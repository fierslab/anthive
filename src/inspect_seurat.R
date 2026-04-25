#!/usr/bin/env Rscript
# Inspect a Seurat RDS object: print metadata columns, cell type labels,
# embedding names, assay/layer info. Used to plan conversion strategy.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 1) stop("Usage: inspect_seurat.R <file.RDS>")

suppressPackageStartupMessages(library(Seurat))

cat("Loading", args[1], "...\n")
obj <- readRDS(args[1])

cat("\n=== BASIC INFO ===\n")
cat("Cells:", ncol(obj), "\n")
cat("Genes:", nrow(obj), "\n")
cat("Assays:", paste(Assays(obj), collapse=", "), "\n")
cat("Default assay:", DefaultAssay(obj), "\n")

cat("\n=== ASSAY LAYERS ===\n")
for (a in Assays(obj)) {
    cat(a, ":", paste(Layers(obj[[a]]), collapse=", "), "\n")
}

cat("\n=== REDUCTIONS (embeddings) ===\n")
cat(paste(Reductions(obj), collapse=", "), "\n")

cat("\n=== METADATA COLUMNS ===\n")
md <- obj@meta.data
cat(paste(colnames(md), collapse="\n"), "\n")

cat("\n=== CELL TYPE COLUMNS (likely) ===\n")
# Check columns that might contain cell type info
for (col in colnames(md)) {
    vals <- unique(md[[col]])
    if (is.character(vals) || is.factor(vals)) {
        n_unique <- length(unique(vals))
        if (n_unique >= 2 && n_unique <= 300) {
            cat("\n", col, " (", n_unique, " unique values):\n", sep="")
            tbl <- sort(table(md[[col]]), decreasing=TRUE)
            top <- head(tbl, 15)
            for (i in seq_along(top)) {
                cat("  ", names(top)[i], ": ", top[i], "\n", sep="")
            }
            if (length(tbl) > 15) cat("  ... (", length(tbl) - 15, " more)\n", sep="")
        }
    }
}

cat("\n=== IDENTS ===\n")
tbl <- sort(table(Idents(obj)), decreasing=TRUE)
top <- head(tbl, 15)
for (i in seq_along(top)) {
    cat("  ", names(top)[i], ": ", top[i], "\n", sep="")
}
if (length(tbl) > 15) cat("  ... (", length(tbl) - 15, " more)\n", sep="")

cat("\nDone.\n")
