#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pandas>=2.0",
#   "pyarrow>=14.0",
#   "numpy>=1.24",
#   "scipy>=1.11",
#   "matplotlib>=3.7",
#   "seaborn>=0.12",
#   "anndata>=0.10",
# ]
# ///
"""
Method 01: Univariate Spearman rank correlation panel.

Analysis is exploratory. Two inference levels:
  (A) Cell-level (N=181,938): ranking only — inflated power due to metacell
      pseudoreplication. Used for feature ranking/screening, not for p-values.
  (B) Metacell-level (N=302): correct inference unit. p-values reported here
      with BH multiple-testing correction.

Predictors: all numerical obs columns (excluding Braak-derived cols) +
            55 score columns + all 9591 genes (cell-level ranking only).

Output:
  - spearman_metacell.tsv       -- metacell-level: rho, p_raw, p_adj per feature
  - spearman_cell_obs_scores.tsv -- cell-level ranking (no p-values), obs+scores
  - spearman_cell_genes.tsv     -- cell-level gene ranking (no p-values)
  - figs/01_spearman/           -- waterfall plot, top-40 barplot, scatter examples

Software: scipy 1.17.1, pandas 2.3.3, matplotlib 3.10.9
Random seed: not applicable (Spearman is deterministic).
"""

import logging
import sys
from pathlib import Path

import anndata as ad
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy.stats as stats
import seaborn as sns

matplotlib.use("Agg")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

OUT_DIR = Path("/data/1/users/mark/anthive4/report/F26_MCC_avg_Braak")
FIG_DIR = OUT_DIR / "figs" / "01_spearman"
FIG_DIR.mkdir(parents=True, exist_ok=True)

META_H5AD_PATH = Path("/data/1/users/mark/anthive4/data/h5adstore/Francken2026/PFC_Microglia_meta_260806.h5ad")

# Columns to exclude because they are definitionally collinear with avg_Braak
BRAAK_DERIVED_COLS = {
    "avg_Braak", "Braak_numeric", "stage_numeric", "Braak_rank", "Braak_rank_glob",
    "mean_avg_Braak_CT", "mean_avg_Braak_allCT", "delta_percell", "delta_donorBraak",
    "delta_allcell",
    # Also exclude the per-cohort Braak averages from metacell obs (same quantity)
    "avg_Braak_SEAAD", "avg_Braak_Mac", "avg_Braak_lu", "avg_Braak_ROSMAP",
    "med_Braak_SEAAD", "med_Braak_Mac", "med_Braak_lu", "med_Braak_ROSMAP",
    "med_Braak", "Braak_rank", "Braak_bin", "Braak_bin2",
}

# Technical QC columns — include in analysis but flag separately
TECHNICAL_PREFIXES = (
    "SEAAD_GEX_", "SEAAD_ATAC_", "SEAAD_Multiome_", "Mac_QC_",
    "lu_doublet", "SEAAD_Doublet",
)


def benjamini_hochberg_correction(p_values: np.ndarray) -> np.ndarray:
    """Apply Benjamini-Hochberg FDR correction to an array of p-values.

    Args:
        p_values: Array of raw p-values.

    Returns:
        Array of BH-adjusted p-values.
    """
    n = len(p_values)
    sort_order = np.argsort(p_values)
    sorted_pvals = p_values[sort_order]
    bh_corrected = np.minimum(1.0, sorted_pvals * n / (np.arange(1, n + 1)))
    # Enforce monotonicity (cumulative minimum from right)
    for idx in range(n - 2, -1, -1):
        bh_corrected[idx] = min(bh_corrected[idx], bh_corrected[idx + 1])
    result = np.empty(n)
    result[sort_order] = bh_corrected
    return result


def compute_spearman_series(
    feature_matrix: pd.DataFrame,
    target: pd.Series,
    feature_label: str = "",
) -> pd.DataFrame:
    """Compute Spearman rho and p-value for each column vs. target.

    Args:
        feature_matrix: DataFrame of predictors (columns) x samples (rows).
        target: Target series (avg_Braak), aligned to feature_matrix rows.
        feature_label: Label for logging.

    Returns:
        DataFrame with columns: feature, rho, p_raw, n_pairs.
    """
    results = []
    shared_index = feature_matrix.index.intersection(target.index)
    feature_matrix = feature_matrix.loc[shared_index]
    target_aligned = target.loc[shared_index]

    log.info(f"  Computing Spearman for {feature_matrix.shape[1]} {feature_label} features (N={len(shared_index)})...")

    for col in feature_matrix.columns:
        feature_vals = feature_matrix[col]
        # Drop NaN pairs
        valid_mask = feature_vals.notna() & target_aligned.notna()
        n_valid = valid_mask.sum()
        if n_valid < 10:
            continue
        rho, p_val = stats.spearmanr(feature_vals[valid_mask], target_aligned[valid_mask])
        results.append({"feature": col, "rho": rho, "p_raw": p_val, "n_pairs": n_valid})

    return pd.DataFrame(results)


def make_waterfall_plot(df: pd.DataFrame, title: str, out_path: Path, n_show: int = 40) -> None:
    """Plot top-N positive and negative features by |rho|, waterfall style."""
    df_sorted = df.reindex(df["rho"].abs().sort_values(ascending=False).index)
    top_n = df_sorted.head(n_show).sort_values("rho")

    colors = ["#d73027" if r > 0 else "#4575b4" for r in top_n["rho"]]

    fig, ax = plt.subplots(figsize=(10, max(6, n_show * 0.25)))
    bars = ax.barh(top_n["feature"], top_n["rho"], color=colors)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Spearman ρ vs avg_Braak")
    ax.set_title(title, fontsize=11)
    ax.set_xlim(-1.05, 1.05)

    # Mark significance
    if "p_adj" in top_n.columns:
        for idx, (_, row) in enumerate(top_n.iterrows()):
            if row["p_adj"] < 0.05:
                ax.text(
                    row["rho"] + (0.02 if row["rho"] >= 0 else -0.02),
                    idx,
                    "*",
                    va="center",
                    ha="left" if row["rho"] >= 0 else "right",
                    fontsize=8,
                )
    plt.tight_layout()
    fig.savefig(out_path.with_suffix(".png"), dpi=150)
    fig.savefig(out_path.with_suffix(".svg"))
    plt.close(fig)
    log.info(f"  Saved: {out_path}")


def make_scatter_panel(
    feature_matrix: pd.DataFrame,
    target: pd.Series,
    top_features: list,
    out_path: Path,
    n_cols: int = 4,
) -> None:
    """Scatter plot panel for top features vs avg_Braak."""
    n_features = len(top_features)
    n_rows = (n_features + n_cols - 1) // n_cols
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(n_cols * 4, n_rows * 3.5))
    axes = np.array(axes).flatten()

    shared_index = feature_matrix.index.intersection(target.index)

    for plot_idx, feature_name in enumerate(top_features):
        ax = axes[plot_idx]
        if feature_name not in feature_matrix.columns:
            ax.set_visible(False)
            continue
        feature_vals = feature_matrix.loc[shared_index, feature_name]
        target_vals = target.loc[shared_index]
        valid_mask = feature_vals.notna() & target_vals.notna()

        # Subsample to max 5000 points for scatter clarity
        subsample_size = min(5000, valid_mask.sum())
        if valid_mask.sum() > subsample_size:
            subsample_idx = np.random.RandomState(42).choice(
                np.where(valid_mask)[0], size=subsample_size, replace=False
            )
            valid_x = feature_vals.iloc[subsample_idx]
            valid_y = target_vals.iloc[subsample_idx]
        else:
            valid_x = feature_vals[valid_mask]
            valid_y = target_vals[valid_mask]

        rho, _ = stats.spearmanr(feature_vals[valid_mask], target_vals[valid_mask])

        ax.scatter(valid_x, valid_y, alpha=0.2, s=4, rasterized=True, color="#555555")
        ax.set_xlabel(feature_name[:40], fontsize=8)
        ax.set_ylabel("avg_Braak", fontsize=8)
        ax.set_title(f"ρ = {rho:.3f}", fontsize=9)
        ax.tick_params(labelsize=7)

    for remaining_idx in range(len(top_features), len(axes)):
        axes[remaining_idx].set_visible(False)

    plt.tight_layout()
    fig.savefig(out_path.with_suffix(".png"), dpi=150)
    fig.savefig(out_path.with_suffix(".svg"))
    plt.close(fig)
    log.info(f"  Saved scatter panel: {out_path}")


def main() -> None:
    # =========================================================================
    # LEVEL A: Metacell-level analysis (N=302) — correct inference unit
    # =========================================================================
    log.info("=== Level A: Metacell-level Spearman (N=302, proper inference) ===")

    log.info("Loading metacell h5ad for gene expression + obs...")
    meta_adata = ad.read_h5ad(str(META_H5AD_PATH))
    metacell_df = pd.read_parquet(OUT_DIR / "data_metacells.parquet")

    target_metacell = metacell_df["avg_Braak"].dropna()
    log.info(f"  Metacells with valid avg_Braak: {len(target_metacell)}")

    # Numerical obs columns from metacell (exclude Braak-derived)
    meta_num_cols = [
        col for col in metacell_df.select_dtypes(include="number").columns
        if col not in BRAAK_DERIVED_COLS
    ]
    log.info(f"  Metacell numerical predictor columns: {len(meta_num_cols)}")

    # Metacell gene expression (X = CP10K log1p)
    gene_expr_meta = pd.DataFrame(
        meta_adata.X.toarray() if hasattr(meta_adata.X, "toarray") else meta_adata.X,
        index=meta_adata.obs_names,
        columns=meta_adata.var_names,
    )
    log.info(f"  Metacell gene matrix: {gene_expr_meta.shape}")

    # Run Spearman on metacell obs features
    obs_results_meta = compute_spearman_series(
        metacell_df[meta_num_cols].set_index(metacell_df.index),
        target_metacell,
        feature_label="metacell-obs+scores",
    )

    # Apply BH correction
    obs_results_meta["p_adj"] = benjamini_hochberg_correction(obs_results_meta["p_raw"].values)
    obs_results_meta["significant_bh05"] = obs_results_meta["p_adj"] < 0.05
    obs_results_meta["is_technical"] = obs_results_meta["feature"].str.startswith(TECHNICAL_PREFIXES)
    obs_results_meta = obs_results_meta.sort_values("rho", key=abs, ascending=False)

    log.info(f"  Features tested: {len(obs_results_meta)}")
    log.info(f"  Significant (BH q<0.05): {obs_results_meta['significant_bh05'].sum()}")
    log.info(f"  Top 10 by |rho|:")
    for _, row in obs_results_meta.head(10).iterrows():
        log.info(f"    {row['feature']}: rho={row['rho']:.3f}, q={row['p_adj']:.3e}, n={row['n_pairs']}")

    # Run Spearman on metacell genes
    gene_results_meta = compute_spearman_series(
        gene_expr_meta,
        target_metacell,
        feature_label="metacell-genes",
    )
    gene_results_meta["p_adj"] = benjamini_hochberg_correction(gene_results_meta["p_raw"].values)
    gene_results_meta["significant_bh05"] = gene_results_meta["p_adj"] < 0.05
    gene_results_meta = gene_results_meta.sort_values("rho", key=abs, ascending=False)

    log.info(f"  Genes tested: {len(gene_results_meta)}")
    log.info(f"  Significant genes (BH q<0.05): {gene_results_meta['significant_bh05'].sum()}")
    log.info(f"  Top 10 genes by |rho|:")
    for _, row in gene_results_meta.head(10).iterrows():
        log.info(f"    {row['feature']}: rho={row['rho']:.3f}, q={row['p_adj']:.3e}")

    # Save metacell results
    obs_results_meta.to_csv(OUT_DIR / "spearman_metacell_obs_scores.tsv", sep="\t", index=False)
    gene_results_meta.to_csv(OUT_DIR / "spearman_metacell_genes.tsv", sep="\t", index=False)
    log.info("Saved metacell Spearman results")

    # =========================================================================
    # LEVEL B: Cell-level ranking (N=181,938) — feature screening only
    # =========================================================================
    log.info("=== Level B: Cell-level Spearman (N=181,938 — ranking only, no p-values) ===")

    cells_df = pd.read_parquet(OUT_DIR / "data_cells.parquet")
    target_cells = cells_df["avg_Braak"].dropna()
    log.info(f"  Cells with valid avg_Braak: {len(target_cells)}")

    # Obs numerical columns
    num_cols_cells = [
        col for col in cells_df.select_dtypes(include="number").columns
        if col not in BRAAK_DERIVED_COLS and col != "cell_id"
    ]
    log.info(f"  Cell-level numerical predictor columns: {len(num_cols_cells)}")

    cell_num_results = compute_spearman_series(
        cells_df[num_cols_cells].set_index(cells_df.index),
        target_cells.reset_index(drop=True),
        feature_label="cell-obs+scores",
    )
    cell_num_results = cell_num_results.sort_values("rho", key=abs, ascending=False)
    # NOTE: N=181938 is pseudoreplicated (many cells share metacell avg_Braak).
    # p-values are biologically meaningless here. Only rho ranking is valid.
    cell_num_results.drop(columns=["p_raw"], inplace=True)

    cell_num_results.to_csv(OUT_DIR / "spearman_cell_obs_scores.tsv", sep="\t", index=False)
    log.info(f"  Saved cell-level obs+scores ranking ({len(cell_num_results)} features)")

    # =========================================================================
    # FIGURES
    # =========================================================================
    log.info("=== Generating figures ===")

    # 1. Metacell obs+scores waterfall (top 40 by |rho|, biological features only)
    bio_features_meta = obs_results_meta[~obs_results_meta["is_technical"]].copy()
    make_waterfall_plot(
        bio_features_meta,
        title=f"Metacell-level Spearman ρ vs avg_Braak (N=302 metacells)\nBiological features only, BH-corrected (* = q<0.05)",
        out_path=FIG_DIR / "waterfall_metacell_obs_scores",
        n_show=40,
    )

    # 2. Gene waterfall (metacell level, top 40)
    make_waterfall_plot(
        gene_results_meta,
        title=f"Metacell-level Spearman ρ vs avg_Braak (N=302)\nAll {len(gene_results_meta)} genes, top 40 shown",
        out_path=FIG_DIR / "waterfall_metacell_genes",
        n_show=40,
    )

    # 3. Scatter panel: top 12 obs+score features (metacell level)
    top_features_bio = bio_features_meta["feature"].head(12).tolist()
    make_scatter_panel(
        metacell_df.set_index(metacell_df.index)[bio_features_meta["feature"].tolist()],
        target_metacell,
        top_features_bio,
        out_path=FIG_DIR / "scatter_top12_obs_scores_metacell",
        n_cols=4,
    )

    # 4. Scatter panel: top 12 genes (metacell level)
    top_genes = gene_results_meta["feature"].head(12).tolist()
    make_scatter_panel(
        gene_expr_meta,
        target_metacell,
        top_genes,
        out_path=FIG_DIR / "scatter_top12_genes_metacell",
        n_cols=4,
    )

    # 5. Summary comparison barplot: top 20 from each category
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))

    for ax_idx, (df_plot, title_str) in enumerate([
        (bio_features_meta.head(20).sort_values("rho"),
         "Top-20 biological obs/scores (metacell N=302)"),
        (gene_results_meta.head(20).sort_values("rho"),
         "Top-20 genes (metacell N=302)"),
    ]):
        ax = axes[ax_idx]
        colors_plot = ["#d73027" if r > 0 else "#4575b4" for r in df_plot["rho"]]
        ax.barh(df_plot["feature"], df_plot["rho"], color=colors_plot)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.set_xlabel("Spearman ρ vs avg_Braak")
        ax.set_title(title_str, fontsize=10)
        ax.tick_params(axis="y", labelsize=8)
        if "p_adj" in df_plot.columns:
            for plot_row_idx, (_, row) in enumerate(df_plot.iterrows()):
                if row["p_adj"] < 0.05:
                    ax.text(
                        row["rho"] + (0.01 if row["rho"] >= 0 else -0.01),
                        plot_row_idx,
                        "*",
                        va="center",
                        fontsize=9,
                        ha="left" if row["rho"] >= 0 else "right",
                    )

    plt.suptitle("Spearman rank correlation with avg_Braak\n(* = BH q < 0.05; N = 302 metacells for proper inference)", fontsize=11)
    plt.tight_layout()
    fig.savefig(FIG_DIR / "summary_top20_comparison.png", dpi=150)
    fig.savefig(FIG_DIR / "summary_top20_comparison.svg")
    plt.close(fig)

    # =========================================================================
    # SUMMARY STATS
    # =========================================================================
    log.info("=== Summary ===")
    log.info(f"Metacell level (N={len(target_metacell)}):")
    log.info(f"  Total obs+score features tested: {len(obs_results_meta)}")
    log.info(f"  Biological features significant (BH q<0.05): {bio_features_meta['significant_bh05'].sum()}")
    log.info(f"  Top obs/score feature: {bio_features_meta.iloc[0]['feature']} rho={bio_features_meta.iloc[0]['rho']:.3f} q={bio_features_meta.iloc[0]['p_adj']:.3e}")
    log.info(f"  Total genes tested: {len(gene_results_meta)}")
    log.info(f"  Significant genes (BH q<0.05): {gene_results_meta['significant_bh05'].sum()}")
    log.info(f"  Top gene: {gene_results_meta.iloc[0]['feature']} rho={gene_results_meta.iloc[0]['rho']:.3f} q={gene_results_meta.iloc[0]['p_adj']:.3e}")
    log.info("Done.")


if __name__ == "__main__":
    main()
