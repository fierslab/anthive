#!/usr/bin/env Rscript
# facet_test_limma.R: pseudobulk limma-voom for ant-facet-test.
#
# What it does: reads the long pseudobulk counts and the job/sample design
# tables written by bin/annot/ant-facet-test and, per job (one dataset, one
# cell type, one facet), runs edgeR filterByExpr (design aware), TMM
# normalisation, limma voom, lmFit, eBayes and Benjamini-Hochberg within the
# job.
#
# Inputs (in <work_dir>):
#   counts_long.csv  sample_idx, gene_id, count     (raw summed counts, sparse)
#   genes.csv        gene_id, gene_name, symbol
#   jobs.csv         job_id, facet, stratum, numeric, tau (log2 fold change
#                    threshold of the treat test; per Braak stage or per decade
#                    of age when numeric)
#   samples.csv      job_id, sample_idx, donor, facet_value, cov_sex, cov_age,
#                    cov_pmi, batch   (NA covariate columns are omitted)
# Outputs (in <work_dir>):
#   results.csv      job_id, gene_id, logFC, se, ci_low, ci_high, t, p_value,
#                    q_value, p_treat, q_treat, ave_expr, df_total
#   job_summary.csv  job_id, status, n_genes_input, n_genes_tested, n_samples
#   sample_norm.csv  job_id, sample_idx, lib_size, norm_factor: the library size (after filterByExpr) and TMM
#                    factor of every sample, i.e. the exact scale of the voom input (donor value plots)
#
# Usage: Rscript facet_test_limma.R <work_dir> <seed> [null_permutations] [samples_only]
# With samples_only = 1 only filterByExpr and TMM run: results.csv stays empty, sample_norm.csv is written.
# p_treat / q_treat: limma treat (McCarthy and Smyth 2009), H0 |logFC| <= tau,
# on the same moderated fit; p_treat is a one-sided p in the direction of the
# observed sign (the opposite tail is negligible), BH within the job.
# Model: ~ facet_value [+ cov_sex] [+ cov_age] [+ cov_pmi] [+ cov_path] [+ batch];
# voomWithQualityWeights, lmFit, eBayes(robust = TRUE) and treat(robust = TRUE) since v3b;
# the facet coefficient is reported (log2 CPM scale from voom).

suppressPackageStartupMessages({
  library(limma)
  library(edgeR)
  library(data.table)
})

args <- commandArgs(trailingOnly = TRUE)
work_dir <- args[1]
set.seed(as.integer(args[2]))
# optional 3rd argument: label-permutation null shuffles for units with fewer than null_max_samples samples
null_permutations <- if (length(args) >= 3) as.integer(args[3]) else 0L
null_max_samples <- 40L
samples_only <- length(args) >= 4 && as.integer(args[4]) == 1L

counts_long <- fread(file.path(work_dir, "counts_long.csv"))
jobs <- fread(file.path(work_dir, "jobs.csv"))
samples <- fread(file.path(work_dir, "samples.csv"), colClasses = list(character = c("batch", "donor")))
samples[is.na(batch), batch := ""]

all_results <- list()
all_summaries <- list()
all_norms <- list()

for (job_index in seq_len(nrow(jobs))) {
  current_job <- jobs$job_id[job_index]
  job_samples <- samples[samples$job_id == current_job]
  summary_row <- data.table(job_id = current_job, status = "ok", n_genes_input = 0L,
                            n_genes_tested = 0L, n_samples = nrow(job_samples),
                            null_permutations = NA_integer_, null_lambda_median = NA_real_,
                            null_hits_mean = NA_real_, null_hits_max = NA_real_)
  tryCatch({
    sample_ids <- job_samples$sample_idx
    job_counts <- counts_long[counts_long$sample_idx %in% sample_ids]
    gene_ids <- sort(unique(job_counts$gene_id))
    summary_row$n_genes_input <- length(gene_ids)
    count_matrix <- matrix(0, nrow = length(gene_ids), ncol = length(sample_ids),
                           dimnames = list(as.character(gene_ids), as.character(sample_ids)))
    count_matrix[cbind(match(job_counts$gene_id, gene_ids),
                       match(job_counts$sample_idx, sample_ids))] <- job_counts$count

    design_frame <- data.frame(facet_value = job_samples$facet_value)
    if (any(!is.na(job_samples$cov_sex))) design_frame$cov_sex <- job_samples$cov_sex
    if (any(!is.na(job_samples$cov_age))) design_frame$cov_age <- job_samples$cov_age
    if (any(!is.na(job_samples$cov_pmi))) design_frame$cov_pmi <- job_samples$cov_pmi
    if ("cov_path" %in% names(job_samples) && any(!is.na(job_samples$cov_path))) design_frame$cov_path <- job_samples$cov_path
    if (any(job_samples$batch != "")) design_frame$batch <- factor(job_samples$batch)
    design <- model.matrix(~ ., data = design_frame)
    rownames(design) <- as.character(sample_ids)

    if (qr(design)$rank < ncol(design)) stop("rank deficient design")
    if (nrow(design) - ncol(design) < 2) stop("fewer than 2 residual df")

    dge <- DGEList(counts = count_matrix)
    keep <- filterByExpr(dge, design = design)
    if (sum(keep) < 100) stop(paste("only", sum(keep), "genes pass filterByExpr"))
    dge <- dge[keep, , keep.lib.sizes = FALSE]
    dge <- calcNormFactors(dge, method = "TMM")
    all_norms[[length(all_norms) + 1]] <- data.table(
      job_id = current_job, sample_idx = as.integer(colnames(dge)), lib_size = dge$samples$lib.size,
      norm_factor = dge$samples$norm.factors)
    summary_row$n_genes_tested <- nrow(dge)
    if (!samples_only) {
      # observation weights from voom plus sample quality weights (Liu et al. 2015), robust moderation
      # (Phipson et al. 2016): one outlying donor or gene does not set the prior variance
      voom_object <- voomWithQualityWeights(dge, design, plot = FALSE)
      linear_fit <- lmFit(voom_object, design)
      fit <- eBayes(linear_fit, robust = TRUE)
      tau <- if ("tau" %in% names(jobs)) jobs$tau[job_index] else 0
      treat_fit <- treat(linear_fit, lfc = tau, robust = TRUE)
      p_treat <- treat_fit$p.value[, "facet_value"]
      # strong tier: H0 |logFC| <= tau_strong (log2(1.25) scaled like tau)
      tau_strong <- if ("tau_strong" %in% names(jobs)) jobs$tau_strong[job_index] else tau
      p_treat_strong <- treat(linear_fit, lfc = tau_strong, robust = TRUE)$p.value[, "facet_value"]
      standard_error <- sqrt(fit$s2.post) * fit$stdev.unscaled[, "facet_value"]
      tail_quantile <- qt(0.975, fit$df.total)
      log_fc <- fit$coefficients[, "facet_value"]
      t_statistic <- fit$t[, "facet_value"]
      p_value <- fit$p.value[, "facet_value"]
      all_results[[length(all_results) + 1]] <- data.table(
        job_id = current_job, gene_id = as.integer(rownames(fit)), logFC = log_fc,
        se = standard_error, ci_low = log_fc - tail_quantile * standard_error,
        ci_high = log_fc + tail_quantile * standard_error, t = t_statistic,
        p_value = p_value, q_value = p.adjust(p_value, method = "BH"),
        p_treat = p_treat, q_treat = p.adjust(p_treat, method = "BH"),
        p_treat_strong = p_treat_strong, q_treat_strong = p.adjust(p_treat_strong, method = "BH"),
        ave_expr = fit$Amean, df_total = fit$df.total)
      summary_row$n_genes_tested <- nrow(fit)
      # label-permutation null (small units only): the same pipeline on shuffled facet values, covariates fixed
      if (null_permutations > 0 && nrow(design) < null_max_samples) {
        null_lambdas <- numeric(0); null_hits <- numeric(0)
        for (shuffle in seq_len(null_permutations)) {
          shuffled <- design_frame
          shuffled$facet_value <- sample(shuffled$facet_value)
          null_design <- model.matrix(~ ., data = shuffled)
          if (qr(null_design)$rank < ncol(null_design)) next
          null_fit <- eBayes(lmFit(voomWithQualityWeights(dge, null_design, plot = FALSE), null_design), robust = TRUE)
          null_p <- null_fit$p.value[, "facet_value"]
          null_lambdas <- c(null_lambdas, median(qchisq(pmax(null_p, 1e-300), 1, lower.tail = FALSE)) / 0.4549364)
          null_hits <- c(null_hits, sum(p.adjust(null_p, method = "BH") < 0.05))
        }
        if (length(null_hits)) {
          summary_row$null_permutations <- length(null_hits)
          summary_row$null_lambda_median <- median(null_lambdas)
          summary_row$null_hits_mean <- mean(null_hits)
          summary_row$null_hits_max <- max(null_hits)
        }
      }
    }
  }, error = function(error_condition) {
    summary_row$status <<- conditionMessage(error_condition)
  })
  all_summaries[[length(all_summaries) + 1]] <- summary_row
}

fwrite(if (length(all_results)) rbindlist(all_results) else data.table(job_id = integer()),
       file.path(work_dir, "results.csv"))
fwrite(rbindlist(all_summaries), file.path(work_dir, "job_summary.csv"))
fwrite(if (length(all_norms)) rbindlist(all_norms) else data.table(job_id = integer(), sample_idx = integer(),
                                                                    lib_size = numeric(), norm_factor = numeric()),
       file.path(work_dir, "sample_norm.csv"))
