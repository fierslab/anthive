#!/usr/bin/env Rscript
# Convert a Seurat v5 RDS object to h5ad via reticulate + anndata.
#
# Usage: seurat_to_h5ad.R <input.RDS> <output.h5ad>
#
# Transfers: count matrix (from default assay), normalized data,
# all metadata, all reductions (embeddings), and Idents as 'seurat_idents'.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) stop("Usage: seurat_to_h5ad.R <input.RDS> <output.h5ad>")

input_path  <- args[1]
output_path <- args[2]

suppressPackageStartupMessages({
    library(Seurat)
    library(Matrix)
    library(reticulate)
})

# Force reticulate to use the micromamba env Python (has anndata, scipy)
env_python <- file.path(Sys.getenv("CONDA_PREFIX", unset = ""), "bin", "python")
if (file.exists(env_python)) {
    use_python(env_python, required = TRUE)
}

anndata <- import("anndata")
np      <- import("numpy")
scipy_sparse <- import("scipy.sparse")

cat("Loading", input_path, "...\n")
obj <- readRDS(input_path)
cat("Cells:", ncol(obj), "  Genes:", nrow(obj), "\n")

assay_name <- DefaultAssay(obj)
assay_obj  <- obj[[assay_name]]

# ── Extract expression matrices ──────────────────────────────────────────────

cat("Extracting expression data from assay:", assay_name, "\n")

# Get available layers
available_layers <- Layers(assay_obj)
cat("Available layers:", paste(available_layers, collapse=", "), "\n")

# Helper: Seurat layer → scipy CSC matrix (cells x genes)
to_scipy_csc <- function(mat) {
    if (is.null(mat)) return(NULL)
    # Seurat stores genes (rows) x cells (cols) as dgCMatrix (CSC format).
    # Transposing a dgCMatrix gives another dgCMatrix: now cells x genes in CSC.
    if (!inherits(mat, "dgCMatrix")) {
        mat <- as(mat, "dgCMatrix")
    }
    mat_t <- t(mat)  # cells x genes, still dgCMatrix (CSC)
    # dgCMatrix CSC slots: @x (values), @i (row indices), @p (col pointers)
    scipy_sparse$csc_matrix(
        tuple(
            np$array(mat_t@x),
            np$array(as.integer(mat_t@i)),
            np$array(as.integer(mat_t@p))
        ),
        shape = tuple(as.integer(mat_t@Dim[1]), as.integer(mat_t@Dim[2]))
    )
}

# ── Extract layers (handles Seurat v5 split layers like data.1, data.2) ──────

# Seurat v5 can split layers by sample (data.1, data.2, counts.1, counts.2).
# JoinLayers() merges them back into single data/counts matrices.
split_data_layers <- grep("^data\\.", available_layers, value = TRUE)
split_count_layers <- grep("^counts\\.", available_layers, value = TRUE)
if (length(split_data_layers) > 1 || length(split_count_layers) > 1) {
    cat("  Detected split layers, joining...\n")
    obj[[assay_name]] <- JoinLayers(obj[[assay_name]])
    assay_obj <- obj[[assay_name]]
    available_layers <- Layers(assay_obj)
    cat("  Layers after join:", paste(available_layers, collapse=", "), "\n")
}

X_mat <- NULL
layers_dict <- list()

if ("data" %in% available_layers) {
    cat("  Using 'data' layer as X (primary)\n")
    X_mat <- to_scipy_csc(LayerData(assay_obj, layer = "data"))
}

if ("counts" %in% available_layers) {
    counts_mat <- to_scipy_csc(LayerData(assay_obj, layer = "counts"))
    if (is.null(X_mat)) {
        cat("  Using 'counts' layer as X (primary, no 'data' layer)\n")
        X_mat <- counts_mat
    } else {
        cat("  Adding 'counts' as a layer\n")
        layers_dict[["counts"]] <- counts_mat
    }
}

if ("scale.data" %in% available_layers) {
    cat("  Skipping 'scale.data' layer (too large, not needed for browsing)\n")
}

if (is.null(X_mat)) {
    # Fallback: try GetAssayData
    cat("  Fallback: using GetAssayData()\n")
    raw_mat <- GetAssayData(obj, assay = assay_name)
    X_mat <- to_scipy_csc(raw_mat)
}

# ── Metadata ─────────────────────────────────────────────────────────────────

cat("Extracting metadata...\n")
obs_df <- obj@meta.data
# Add Idents as a column
obs_df$seurat_idents <- as.character(Idents(obj))

# Convert factors to characters for clean h5ad storage
for (col in colnames(obs_df)) {
    if (is.factor(obs_df[[col]])) {
        obs_df[[col]] <- as.character(obs_df[[col]])
    }
}

# Gene metadata (var)
var_df <- data.frame(row.names = rownames(assay_obj))
# Seurat v5 Assay5 has meta.data; v4 Assay does not
if (isVirtualClass(class(assay_obj)) || is(assay_obj, "Assay5")) {
    tryCatch({
        amd <- assay_obj@meta.data
        if (!is.null(amd) && nrow(amd) > 0) var_df <- amd
    }, error = function(e) NULL)
}

# ── Embeddings ───────────────────────────────────────────────────────────────

cat("Extracting embeddings...\n")
obsm_dict <- list()
for (red_name in Reductions(obj)) {
    emb <- Embeddings(obj, reduction = red_name)
    # Rename to anndata convention: X_<name>
    key <- paste0("X_", red_name)
    obsm_dict[[key]] <- np$array(emb)
    cat("  ", key, ": ", ncol(emb), " dims\n", sep="")
}

# ── Build AnnData object ─────────────────────────────────────────────────────

cat("Building AnnData object...\n")
adata <- anndata$AnnData(
    X      = X_mat,
    obs    = obs_df,
    var    = var_df
)

# Add layers
for (lname in names(layers_dict)) {
    adata$layers[[lname]] <- layers_dict[[lname]]
}

# Add embeddings
for (ename in names(obsm_dict)) {
    adata$obsm[[ename]] <- obsm_dict[[ename]]
}

cat("AnnData: ", adata$n_obs, " cells x ", adata$n_vars, " genes\n", sep="")
cat("Layers:", paste(names(adata$layers), collapse=", "), "\n")
cat("Obsm:", paste(names(adata$obsm), collapse=", "), "\n")

# ── Write ────────────────────────────────────────────────────────────────────

cat("Writing", output_path, "...\n")
adata$write_h5ad(output_path)
cat("Done. File size:", round(file.info(output_path)$size / 1024 / 1024), "MB\n")
