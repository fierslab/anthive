#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "duckdb>=1.3,<1.6",
#   "pandas>=2.0,<3.0",
#   "pyarrow>=14.0",
#   "numpy>=1.24,<2.5",
#   "anndata>=0.10,<0.13",
# ]
# ///
"""
Prepare merged analysis data from PFC_Microglia_combat_260804.

Outputs:
  - data_cells.parquet       -- all 182k cells: obsnum + obscat + scores (wide pivot)
  - data_metacells.parquet   -- 302 metacells aggregated from the meta h5ad
  - gene_matrix_top500.parquet -- top-500 HVGs by dispersion for metacell-level gene analysis

Run from repo root with:
  micromamba run -p envs/anthive-data python3 report/F26_MCC_avg_Braak/00_prepare_data.py
"""

import logging
import sys
from pathlib import Path

import anndata as ad
import duckdb
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

RANDOM_SEED = 42
H5AD_PATH = Path("/data/1/users/mark/anthive4/data/h5adstore/Francken2026/PFC_Microglia_combat_260804.h5ad")
META_H5AD_PATH = Path("/data/1/users/mark/anthive4/data/h5adstore/Francken2026/PFC_Microglia_meta_260806.h5ad")
DUCKDB_PATH = Path("/data/1/users/mark/anthive4/data/h5adstore/Francken2026/PFC_Microglia_combat_260804.duckdb")
SCORES_PATH = Path("/data/1/users/mark/anthive4/data/h5adstore/Francken2026/PFC_Microglia_combat_260804.scores.parquet")
OUT_DIR = Path("/data/1/users/mark/anthive4/report/F26_MCC_avg_Braak")

# Columns to exclude from predictors because they are derived from or identical to avg_Braak
BRAAK_DERIVED_COLS = {
    "avg_Braak", "Braak_numeric", "stage_numeric", "Braak_rank", "Braak_rank_glob",
    "mean_avg_Braak_CT", "mean_avg_Braak_allCT", "delta_percell", "delta_donorBraak",
    "delta_allcell",
}

# Columns that are purely technical QC (sequencing metrics) — exclude from biological predictors
# but include in a separate "technical confounders" set
TECHNICAL_PREFIX = [
    "SEAAD_GEX_", "SEAAD_ATAC_", "SEAAD_Multiome_", "SEAAD_GEX",
    "Mac_QC_", "lu_doublet",
]


def load_obsnum_obscat_merged(duckdb_path: Path) -> pd.DataFrame:
    """Load obsnum and obscat from DuckDB, merge on cell_name."""
    log.info("Loading obsnum and obscat from DuckDB (read-only)...")
    con = duckdb.connect(str(duckdb_path), read_only=True)
    obsnum = con.execute("SELECT * FROM obsnum").df()
    obscat = con.execute("SELECT * FROM obscat").df()
    con.close()
    log.info(f"  obsnum: {obsnum.shape}, obscat: {obscat.shape}")
    merged = obsnum.merge(obscat, on="cell_name", how="inner")
    log.info(f"  merged: {merged.shape}")
    return merged


def load_scores_wide(scores_path: Path) -> pd.DataFrame:
    """Load scores parquet and pivot to wide format (one row per cell_id)."""
    log.info("Loading and pivoting scores parquet...")
    scores_long = pd.read_parquet(scores_path)
    log.info(f"  raw scores: {scores_long.shape}")

    # Create a module label that combines catalog and module
    scores_long["feature_name"] = scores_long["catalog"].str.split("/").str[-1] + "__" + scores_long["module"]

    scores_wide = scores_long.pivot_table(
        index="cell_id",
        columns="feature_name",
        values="score",
        aggfunc="first",
    )
    scores_wide.index.name = "cell_id"
    scores_wide.columns.name = None
    scores_wide = scores_wide.reset_index()
    log.info(f"  scores wide: {scores_wide.shape}")
    return scores_wide


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- Load cell-level data ---
    obs_df = load_obsnum_obscat_merged(DUCKDB_PATH)

    # Load scores and merge
    scores_wide = load_scores_wide(SCORES_PATH)

    # DuckDB cell_name is a string like "AAACCTGAGAAACGCC-1-SEAAD-..."
    # scores_wide cell_id is integer (the row index in obsnum)
    # Need to get an integer cell_id from obsnum to join with scores
    # The obsnum row position IS the cell_id in the scores file
    log.info("Building cell_id integer index for scores join via cells table...")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    cells_id_map = con.execute("SELECT cell_id, cell_name FROM cells").df()
    con.close()

    log.info(f"  cells table: {len(cells_id_map)} rows, cell_id range: {cells_id_map['cell_id'].min()}–{cells_id_map['cell_id'].max()}")
    log.info(f"  scores cell_id sample: {scores_wide['cell_id'].iloc[:3].tolist()}")

    # Merge obs_df with cell_id map (both have cell_name), then join scores on cell_id
    obs_with_id = obs_df.merge(cells_id_map, on="cell_name", how="left")
    cells_with_scores = obs_with_id.merge(scores_wide, on="cell_id", how="left")
    log.info(f"  cells with scores: {cells_with_scores.shape}")

    # Report how many cells have score data
    score_cols = [c for c in cells_with_scores.columns if "__" in c and any(
        cat in c for cat in ["WGCNA", "MICROGLIA_V1"]
    )]
    log.info(f"  score columns found: {len(score_cols)}")
    n_cells_with_scores = cells_with_scores[score_cols[0]].notna().sum() if score_cols else 0
    log.info(f"  cells with score data: {n_cells_with_scores} / {len(cells_with_scores)}")

    # --- Filter to cells with valid avg_Braak ---
    cells_valid = cells_with_scores.dropna(subset=["avg_Braak"])
    log.info(f"  cells with valid avg_Braak: {len(cells_valid)}")

    # --- Save cell-level data ---
    cell_out = OUT_DIR / "data_cells.parquet"
    cells_valid.to_parquet(cell_out, index=False)
    log.info(f"Saved cell-level data: {cell_out}")

    # --- Load metacell data from meta h5ad ---
    log.info("Loading metacell h5ad...")
    meta_adata = ad.read_h5ad(str(META_H5AD_PATH))
    log.info(f"  meta h5ad: {meta_adata.shape} (metacells x genes)")
    log.info(f"  obs columns: {list(meta_adata.obs.columns[:20])}")

    # Build metacell dataframe
    metacell_obs = meta_adata.obs.copy()
    metacell_obs.index.name = "SEACell_id"

    # Add gene expression (X is CP10K log1p normalized per meta h5ad)
    gene_expr = pd.DataFrame(
        meta_adata.X.toarray() if hasattr(meta_adata.X, "toarray") else meta_adata.X,
        index=meta_adata.obs_names,
        columns=meta_adata.var_names,
    )

    # Select top-500 HVGs by dispersions_norm (from var)
    if "dispersions_norm" in meta_adata.var.columns:
        top500_genes = (
            meta_adata.var["dispersions_norm"]
            .nlargest(500)
            .index.tolist()
        )
        log.info(f"  top-500 HVGs by dispersions_norm selected")
    elif "highly_variable" in meta_adata.var.columns:
        hvgs = meta_adata.var[meta_adata.var["highly_variable"]].index.tolist()
        top500_genes = hvgs[:500]
        log.info(f"  top-500 from {len(hvgs)} HVGs selected")
    else:
        # Fall back to all genes if no HVG info
        top500_genes = meta_adata.var_names[:500].tolist()
        log.info("  WARNING: no HVG info found, using first 500 genes")

    gene_matrix_top500 = gene_expr[top500_genes]
    gene_matrix_top500.index.name = "SEACell_id"
    gene_matrix_out = OUT_DIR / "gene_matrix_top500.parquet"
    gene_matrix_top500.to_parquet(gene_matrix_out)
    log.info(f"Saved gene matrix: {gene_matrix_out} shape={gene_matrix_top500.shape}")

    # Add scores to metacells -- aggregate from cell-level
    # Match on SEACell column in combat obs
    if "SEACell" in cells_valid.columns and score_cols:
        log.info("Aggregating scores to metacell level...")
        seacell_scores = (
            cells_valid.groupby("SEACell")[score_cols].mean()
        )
        metacell_obs_with_scores = metacell_obs.join(seacell_scores, how="left")
        log.info(f"  metacell obs with scores: {metacell_obs_with_scores.shape}")
    else:
        metacell_obs_with_scores = metacell_obs
        log.warning("Could not aggregate scores to metacell level")

    metacell_out = OUT_DIR / "data_metacells.parquet"
    metacell_obs_with_scores.to_parquet(metacell_out)
    log.info(f"Saved metacell data: {metacell_out}")

    # --- Summary ---
    log.info("=== Data preparation summary ===")
    log.info(f"  Cell-level rows: {len(cells_valid)}")
    log.info(f"  Metacell rows: {len(metacell_obs_with_scores)}")
    log.info(f"  avg_Braak range: {cells_valid['avg_Braak'].min():.2f} -- {cells_valid['avg_Braak'].max():.2f}")
    log.info(f"  Cognitive.Status distribution:")
    if "Cognitive.Status" in cells_valid.columns:
        for status, count in cells_valid["Cognitive.Status"].value_counts().items():
            log.info(f"    {status}: {count}")
    log.info(f"  Score columns: {len(score_cols)}")
    log.info("Done.")


if __name__ == "__main__":
    main()
