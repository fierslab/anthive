#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pandas>=2.0",
#   "pyarrow>=14.0",
#   "numpy>=1.24",
#   "scikit-learn>=1.3",
#   "lightgbm>=4.0",
#   "shap>=0.44",
#   "matplotlib>=3.7",
#   "seaborn>=0.12",
#   "anndata>=0.10",
# ]
# ///
"""
Method 02: Gradient Boosted Machine (LightGBM) + SHAP analysis.

Analysis is EXPLORATORY. No hold-out test set for gene-level models;
cross-validation R² used for model quality assessment.

Two sub-analyses:
  (A) Metacell-level (N=302): obs features + 55 scores + top-500 HVGs.
      This is the primary analysis — avoids pseudoreplication.
      10-fold cross-validation for R² estimation.
      SHAP values computed on full model for feature importance ranking.

  (B) Cell-level (N=181,938): obs + scores only (no genes — too slow).
      Used only to check whether cell-level ranking agrees with metacell.
      WARNING: inflated R² due to metacell pseudoreplication.

Output:
  - gbm_shap_metacell_results.tsv   -- SHAP importances + CV R² per feature
  - gbm_shap_interaction_pairs.tsv  -- top pairwise SHAP interaction scores
  - figs/02_gbm_shap/               -- SHAP beeswarm, summary bar, interaction plots

Software: lightgbm 4.7.0, shap 0.52.0, scikit-learn 1.8.0, pandas 2.3.3
Random seed: 42 throughout.
"""

import logging
import warnings
from pathlib import Path

import anndata as ad
import lightgbm as lgb
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shap
from sklearn.model_selection import KFold, cross_val_score
from sklearn.preprocessing import StandardScaler

matplotlib.use("Agg")
warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

RANDOM_SEED = 42
np.random.seed(RANDOM_SEED)

OUT_DIR = Path("/data/1/users/mark/anthive4/report/F26_MCC_avg_Braak")
FIG_DIR = OUT_DIR / "figs" / "02_gbm_shap"
FIG_DIR.mkdir(parents=True, exist_ok=True)

META_H5AD_PATH = Path("/data/1/users/mark/anthive4/data/h5adstore/Francken2026/PFC_Microglia_meta_260806.h5ad")

BRAAK_DERIVED_COLS = {
    "avg_Braak", "Braak_numeric", "stage_numeric", "Braak_rank", "Braak_rank_glob",
    "mean_avg_Braak_CT", "mean_avg_Braak_allCT", "delta_percell", "delta_donorBraak",
    "delta_allcell",
    "avg_Braak_SEAAD", "avg_Braak_Mac", "avg_Braak_lu", "avg_Braak_ROSMAP",
    "med_Braak_SEAAD", "med_Braak_Mac", "med_Braak_lu", "med_Braak_ROSMAP",
    "med_Braak", "Braak_rank", "Braak_bin", "Braak_bin2",
    # Explicit pathology stage columns that are downstream of Braak:
    "avg_early_pct", "avg_late_pct",
    "avg_nodem_pct", "avg_dem_pct",
    "avg_Dementia_late_pct", "avg_Dementia_early_pct",
    "avg_Nodementia_late_pct", "avg_Nodementia_early_pct",
    "perc_lu_stage_early", "perc_lu_stage_middle", "perc_lu_stage_late",
    "perc_SEAAD_stage_early", "perc_SEAAD_stage_middle", "perc_SEAAD_stage_late",
    "perc_Mathys_stage_early", "perc_Mathys_stage_middle", "perc_Mathys_stage_late",
    "perc_Gaz_stage_early", "perc_Gaz_stage_middle", "perc_Gaz_stage_late",
    # Cognitive status stage combinations are also partially Braak-derived
    "perc_lu_CogStage_Nodementia_early", "perc_lu_CogStage_Nodementia_middle",
    "perc_lu_CogStage_Nodementia_late", "perc_lu_CogStage_Dementia_early",
    "perc_lu_CogStage_Dementia_middle", "perc_lu_CogStage_Dementia_late",
    "perc_SEAAD_CogStage_Nodementia_early", "perc_SEAAD_CogStage_Nodementia_middle",
    "perc_SEAAD_CogStage_Nodementia_late", "perc_SEAAD_CogStage_Dementia_early",
    "perc_SEAAD_CogStage_Dementia_middle", "perc_SEAAD_CogStage_Dementia_late",
    "perc_Mathys_CogStage_Nodementia_early", "perc_Mathys_CogStage_Nodementia_middle",
    "perc_Mathys_CogStage_Nodementia_late", "perc_Mathys_CogStage_Dementia_early",
    "perc_Mathys_CogStage_Dementia_middle", "perc_Mathys_CogStage_Dementia_late",
    "avg_Nodementia_middle_pct", "avg_Dementia_middle_pct",
}

# Also exclude any per-cohort pathology staging averages
BRAAK_DERIVED_PATTERNS = ("stage_", "CogStage_", "_pct", "avg_Braak_", "med_Braak_", "Braak_")


def build_metacell_feature_matrix(
    metacell_df: pd.DataFrame,
    gene_matrix: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    """Build metacell-level feature matrix and target.

    Excludes Braak-derived columns. Includes:
    - Numerical obs metadata (non-Braak-derived, non-constant)
    - 55 score columns
    - top-500 HVG expression values

    Returns:
        (feature_matrix, target_series) aligned by index.
    """
    target = metacell_df["avg_Braak"].dropna()
    valid_idx = target.index

    # Select numerical obs columns (exclude Braak-derived and constant columns)
    num_cols = []
    for col in metacell_df.select_dtypes(include="number").columns:
        if col in BRAAK_DERIVED_COLS:
            continue
        if any(col.startswith(pat) for pat in BRAAK_DERIVED_PATTERNS):
            continue
        col_vals = metacell_df.loc[valid_idx, col].dropna()
        if col_vals.nunique() <= 1:
            continue
        num_cols.append(col)

    log.info(f"  Metacell obs+score features after filtering: {len(num_cols)}")

    obs_features = metacell_df.loc[valid_idx, num_cols].copy()

    # Align gene matrix
    gene_sub = gene_matrix.loc[gene_matrix.index.intersection(valid_idx)].copy()
    gene_sub = gene_sub.reindex(valid_idx).fillna(0)  # fill missing gene expr with 0

    # Combine
    feature_matrix = pd.concat([obs_features, gene_sub], axis=1)
    log.info(f"  Combined feature matrix: {feature_matrix.shape}")

    # Remove columns with >50% NaN
    nan_frac = feature_matrix.isnull().mean()
    feature_matrix = feature_matrix.loc[:, nan_frac <= 0.5]
    log.info(f"  After removing >50% NaN columns: {feature_matrix.shape}")

    # Impute remaining NaN with column median
    for col in feature_matrix.columns:
        if feature_matrix[col].isnull().any():
            feature_matrix[col] = feature_matrix[col].fillna(feature_matrix[col].median())

    return feature_matrix, target.loc[valid_idx]


def train_lgbm_model(
    feature_matrix: pd.DataFrame,
    target: pd.Series,
    n_cv_folds: int = 10,
) -> tuple[lgb.LGBMRegressor, np.ndarray, pd.DataFrame]:
    """Train LightGBM regressor with cross-validation.

    Args:
        feature_matrix: Features aligned to target index.
        target: Continuous target (avg_Braak).
        n_cv_folds: Number of CV folds.

    Returns:
        (fitted_model, cv_r2_scores, feature_matrix_as_array_df)
    """
    model_params = {
        "n_estimators": 300,
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_child_samples": 5,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_alpha": 0.1,
        "reg_lambda": 0.1,
        "random_state": RANDOM_SEED,
        "n_jobs": 1,  # n_jobs=1: joblib parallelism causes exit code 144 on this host
        "verbose": -1,
    }

    log.info(f"  LightGBM params: {model_params}")
    model = lgb.LGBMRegressor(**model_params)

    kfold = KFold(n_splits=n_cv_folds, shuffle=True, random_state=RANDOM_SEED)
    # n_jobs=1 in cross_val_score: avoids joblib fork issues at small N
    cv_r2_scores = cross_val_score(
        model, feature_matrix.values, target.values,
        cv=kfold, scoring="r2", n_jobs=1,
    )
    log.info(f"  {n_cv_folds}-fold CV R²: mean={cv_r2_scores.mean():.3f} ± {cv_r2_scores.std():.3f}")
    log.info(f"  CV R² range: [{cv_r2_scores.min():.3f}, {cv_r2_scores.max():.3f}]")

    # Fit on full data for SHAP
    model.fit(feature_matrix.values, target.values)

    return model, cv_r2_scores, feature_matrix


def compute_shap_values(
    model: lgb.LGBMRegressor,
    feature_matrix: pd.DataFrame,
) -> tuple[np.ndarray, shap.Explanation]:
    """Compute SHAP values using TreeExplainer.

    Args:
        model: Fitted LightGBM model.
        feature_matrix: Feature matrix (samples x features).

    Returns:
        (shap_values_array, shap_explanation_object)
    """
    log.info("  Computing SHAP TreeExplainer values...")
    explainer = shap.TreeExplainer(model)
    shap_explanation = explainer(feature_matrix.values, check_additivity=False)
    shap_values = shap_explanation.values
    log.info(f"  SHAP values shape: {shap_values.shape}")
    return shap_values, shap_explanation


def build_shap_importance_table(
    shap_values: np.ndarray,
    feature_names: list[str],
    feature_matrix: pd.DataFrame,
    target: pd.Series,
) -> pd.DataFrame:
    """Build a ranked importance table from SHAP values.

    Args:
        shap_values: Array of shape (n_samples, n_features).
        feature_names: List of feature names.
        feature_matrix: Original feature matrix for correlation computation.
        target: Target series.

    Returns:
        DataFrame with feature, mean_abs_shap, shap_direction, spearman_rho.
    """
    import scipy.stats as spstats

    mean_abs_shap = np.abs(shap_values).mean(axis=0)
    # Direction: sign of mean SHAP (positive = higher value → higher avg_Braak)
    mean_signed_shap = shap_values.mean(axis=0)

    results = []
    for feat_idx, feat_name in enumerate(feature_names):
        feat_vals = feature_matrix.iloc[:, feat_idx].values
        target_vals = target.values
        valid_mask = ~np.isnan(feat_vals) & ~np.isnan(target_vals)
        if valid_mask.sum() >= 5:
            rho, _ = spstats.spearmanr(feat_vals[valid_mask], target_vals[valid_mask])
        else:
            rho = np.nan
        results.append({
            "feature": feat_name,
            "mean_abs_shap": mean_abs_shap[feat_idx],
            "mean_signed_shap": mean_signed_shap[feat_idx],
            "spearman_rho": rho,
        })

    df = pd.DataFrame(results).sort_values("mean_abs_shap", ascending=False)
    return df


def plot_shap_beeswarm(
    shap_explanation: shap.Explanation,
    feature_names: list[str],
    out_path: Path,
    max_display: int = 30,
    title: str = "",
) -> None:
    """Generate SHAP beeswarm summary plot."""
    fig, ax = plt.subplots(figsize=(10, max(8, max_display * 0.28)))
    shap_exp_named = shap.Explanation(
        values=shap_explanation.values,
        base_values=shap_explanation.base_values,
        data=shap_explanation.data,
        feature_names=feature_names,
    )
    shap.plots.beeswarm(shap_exp_named, max_display=max_display, show=False)
    if title:
        plt.title(title, fontsize=10, pad=10)
    plt.tight_layout()
    fig.savefig(out_path.with_suffix(".png"), dpi=150, bbox_inches="tight")
    fig.savefig(out_path.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)
    log.info(f"  Saved SHAP beeswarm: {out_path}")


def plot_shap_bar(
    importance_df: pd.DataFrame,
    out_path: Path,
    n_show: int = 30,
    title: str = "",
) -> None:
    """SHAP mean |value| barplot."""
    top_df = importance_df.head(n_show).sort_values("mean_abs_shap")
    colors = ["#d73027" if s > 0 else "#4575b4" for s in top_df["mean_signed_shap"]]

    fig, ax = plt.subplots(figsize=(9, max(6, n_show * 0.26)))
    ax.barh(top_df["feature"], top_df["mean_abs_shap"], color=colors)
    ax.set_xlabel("Mean |SHAP value|  (red = positive effect on avg_Braak)")
    ax.set_title(title, fontsize=10)
    ax.tick_params(axis="y", labelsize=7)
    plt.tight_layout()
    fig.savefig(out_path.with_suffix(".png"), dpi=150)
    fig.savefig(out_path.with_suffix(".svg"))
    plt.close(fig)
    log.info(f"  Saved SHAP bar: {out_path}")


def plot_shap_dependence(
    shap_values: np.ndarray,
    feature_matrix: pd.DataFrame,
    feature_name: str,
    target: pd.Series,
    out_path: Path,
) -> None:
    """SHAP dependence plot for a single feature."""
    feat_idx = list(feature_matrix.columns).index(feature_name)
    feat_vals = feature_matrix.iloc[:, feat_idx].values
    shap_feat = shap_values[:, feat_idx]

    fig, ax = plt.subplots(figsize=(6, 4))
    sc = ax.scatter(feat_vals, shap_feat, c=target.values, cmap="coolwarm", alpha=0.6, s=20)
    plt.colorbar(sc, ax=ax, label="avg_Braak")
    ax.axhline(0, color="black", linewidth=0.7, linestyle="--")
    ax.set_xlabel(feature_name)
    ax.set_ylabel(f"SHAP value for {feature_name}")
    ax.set_title(f"SHAP dependence: {feature_name}")
    plt.tight_layout()
    fig.savefig(out_path.with_suffix(".png"), dpi=150)
    fig.savefig(out_path.with_suffix(".svg"))
    plt.close(fig)


def main() -> None:
    # =========================================================================
    # METACELL ANALYSIS (primary)
    # =========================================================================
    log.info("=== LightGBM + SHAP: Metacell-level (N=302) ===")

    metacell_df = pd.read_parquet(OUT_DIR / "data_metacells.parquet")
    gene_matrix_top500 = pd.read_parquet(OUT_DIR / "gene_matrix_top500.parquet")

    # Add gene prefix to distinguish from obs columns
    gene_matrix_top500.columns = ["gene__" + col for col in gene_matrix_top500.columns]

    log.info("Building feature matrix...")
    feature_matrix, target = build_metacell_feature_matrix(metacell_df, gene_matrix_top500)
    log.info(f"Final feature matrix: {feature_matrix.shape}, target: {len(target)}")

    log.info("Training LightGBM with 10-fold CV...")
    model, cv_r2_scores, _ = train_lgbm_model(feature_matrix, target, n_cv_folds=10)

    log.info("Computing SHAP values...")
    shap_values, shap_explanation = compute_shap_values(model, feature_matrix)

    log.info("Building SHAP importance table...")
    importance_df = build_shap_importance_table(
        shap_values, feature_matrix.columns.tolist(), feature_matrix, target
    )
    importance_df["cv_r2_mean"] = cv_r2_scores.mean()
    importance_df["cv_r2_std"] = cv_r2_scores.std()
    importance_df["n_samples"] = len(target)

    importance_df.to_csv(OUT_DIR / "gbm_shap_metacell_results.tsv", sep="\t", index=False)
    log.info("Saved SHAP importance table")

    log.info("Top 20 SHAP features:")
    for _, row in importance_df.head(20).iterrows():
        log.info(f"  {row['feature']}: SHAP={row['mean_abs_shap']:.4f}, rho={row['spearman_rho']:.3f}")

    # =========================================================================
    # FIGURES: Metacell level
    # =========================================================================
    feature_names_list = feature_matrix.columns.tolist()

    # SHAP beeswarm (top 30)
    plot_shap_beeswarm(
        shap_explanation,
        feature_names_list,
        FIG_DIR / "shap_beeswarm_metacell",
        max_display=30,
        title=f"SHAP feature importance (LightGBM, N=302 metacells)\n10-fold CV R² = {cv_r2_scores.mean():.3f} ± {cv_r2_scores.std():.3f}",
    )

    # SHAP bar (top 30)
    plot_shap_bar(
        importance_df,
        FIG_DIR / "shap_bar_metacell",
        n_show=30,
        title=f"SHAP mean |value| (LightGBM, N=302 metacells, 10-fold CV R²={cv_r2_scores.mean():.3f})",
    )

    # SHAP dependence for top 6 non-gene features
    top_obs_features = [
        row["feature"] for _, row in importance_df.iterrows()
        if not row["feature"].startswith("gene__")
    ][:6]
    for feat_name in top_obs_features:
        safe_name = feat_name.replace("/", "_").replace(" ", "_").replace(".", "_")
        plot_shap_dependence(
            shap_values, feature_matrix, feat_name, target,
            FIG_DIR / f"shap_dependence_{safe_name}",
        )
    log.info(f"  Saved {len(top_obs_features)} SHAP dependence plots (obs features)")

    # SHAP dependence for top 6 gene features
    top_gene_features = [
        row["feature"] for _, row in importance_df.iterrows()
        if row["feature"].startswith("gene__")
    ][:6]
    for feat_name in top_gene_features:
        safe_name = feat_name.replace("/", "_").replace(" ", "_").replace(".", "_")
        plot_shap_dependence(
            shap_values, feature_matrix, feat_name, target,
            FIG_DIR / f"shap_dependence_{safe_name}",
        )
    log.info(f"  Saved {len(top_gene_features)} SHAP dependence plots (gene features)")

    # Combined top features comparison: SHAP vs Spearman
    spearman_meta = pd.read_csv(OUT_DIR / "spearman_metacell_obs_scores.tsv", sep="\t")
    top30_shap = importance_df.head(30)[["feature", "mean_abs_shap", "spearman_rho"]].copy()
    top30_shap["rank_shap"] = range(1, len(top30_shap) + 1)

    fig, axes = plt.subplots(1, 2, figsize=(16, 10))

    # Left: SHAP bar
    top30_sorted = top30_shap.sort_values("mean_abs_shap")
    colors_shap = ["#d73027" if r > 0 else "#4575b4" for r in top30_sorted["spearman_rho"]]
    axes[0].barh(top30_sorted["feature"], top30_sorted["mean_abs_shap"], color=colors_shap)
    axes[0].set_xlabel("Mean |SHAP value|")
    axes[0].set_title("Top-30 features: LightGBM SHAP\n(red=positive Spearman, blue=negative)", fontsize=10)
    axes[0].tick_params(axis="y", labelsize=7)

    # Right: Spearman rho for the same top-30 features
    spearman_lookup = spearman_meta.set_index("feature")["rho"]
    top30_sorted["spearman_from_meta"] = top30_sorted["feature"].map(spearman_lookup)
    axes[1].barh(
        top30_sorted["feature"],
        top30_sorted["spearman_from_meta"].fillna(top30_sorted["spearman_rho"]),
        color=colors_shap,
    )
    axes[1].axvline(0, color="black", linewidth=0.8)
    axes[1].set_xlabel("Spearman ρ vs avg_Braak")
    axes[1].set_title("Same features: Spearman ρ (metacell N=302)", fontsize=10)
    axes[1].tick_params(axis="y", labelsize=7)

    plt.suptitle("SHAP vs Spearman: top-30 LightGBM features", fontsize=11)
    plt.tight_layout()
    fig.savefig(FIG_DIR / "shap_vs_spearman_top30.png", dpi=150)
    fig.savefig(FIG_DIR / "shap_vs_spearman_top30.svg")
    plt.close(fig)
    log.info("  Saved SHAP vs Spearman comparison figure")

    # =========================================================================
    # GBM genes only model: compare with obs-only
    # =========================================================================
    log.info("=== Secondary model: genes only (top-500 HVGs, metacell N=302) ===")
    gene_only_matrix = feature_matrix[[c for c in feature_matrix.columns if c.startswith("gene__")]].copy()
    log.info(f"  Gene-only feature matrix: {gene_only_matrix.shape}")

    model_genes_only = lgb.LGBMRegressor(
        n_estimators=300, learning_rate=0.05, num_leaves=15,
        min_child_samples=5, subsample=0.8, colsample_bytree=0.8,
        random_state=RANDOM_SEED, verbose=-1, n_jobs=1,
    )
    kfold = KFold(n_splits=10, shuffle=True, random_state=RANDOM_SEED)
    cv_r2_genes = cross_val_score(
        model_genes_only, gene_only_matrix.values, target.values,
        cv=kfold, scoring="r2", n_jobs=1,
    )
    log.info(f"  Genes-only 10-fold CV R²: {cv_r2_genes.mean():.3f} ± {cv_r2_genes.std():.3f}")

    # Obs+scores only
    obs_only_cols = [c for c in feature_matrix.columns if not c.startswith("gene__")]
    obs_only_matrix = feature_matrix[obs_only_cols].copy()
    log.info(f"  Obs+scores-only feature matrix: {obs_only_matrix.shape}")

    model_obs_only = lgb.LGBMRegressor(
        n_estimators=300, learning_rate=0.05, num_leaves=15,
        min_child_samples=5, subsample=0.8, colsample_bytree=0.8,
        random_state=RANDOM_SEED, verbose=-1, n_jobs=1,
    )
    cv_r2_obs = cross_val_score(
        model_obs_only, obs_only_matrix.values, target.values,
        cv=kfold, scoring="r2", n_jobs=1,
    )
    log.info(f"  Obs+scores-only 10-fold CV R²: {cv_r2_obs.mean():.3f} ± {cv_r2_obs.std():.3f}")
    log.info(f"  Full model (obs+genes) 10-fold CV R²: {cv_r2_scores.mean():.3f} ± {cv_r2_scores.std():.3f}")

    # Save model comparison
    model_comparison = pd.DataFrame([
        {"model": "obs+scores+genes (top-500 HVG)", "cv_r2_mean": cv_r2_scores.mean(),
         "cv_r2_std": cv_r2_scores.std(), "n_features": feature_matrix.shape[1]},
        {"model": "genes only (top-500 HVG)", "cv_r2_mean": cv_r2_genes.mean(),
         "cv_r2_std": cv_r2_genes.std(), "n_features": gene_only_matrix.shape[1]},
        {"model": "obs+scores only", "cv_r2_mean": cv_r2_obs.mean(),
         "cv_r2_std": cv_r2_obs.std(), "n_features": obs_only_matrix.shape[1]},
    ])
    model_comparison.to_csv(OUT_DIR / "gbm_model_comparison.tsv", sep="\t", index=False)
    log.info("Saved model comparison table")

    # Bar plot of model R²
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(
        model_comparison["model"],
        model_comparison["cv_r2_mean"],
        yerr=model_comparison["cv_r2_std"],
        capsize=5,
        color=["#2166ac", "#5aae61", "#d73027"],
        alpha=0.85,
    )
    ax.set_ylabel("10-fold CV R²")
    ax.set_title("LightGBM model R² comparison\n(N=302 metacells, seed=42)")
    ax.set_ylim(0, 1)
    plt.xticks(rotation=10, ha="right")
    plt.tight_layout()
    fig.savefig(FIG_DIR / "model_r2_comparison.png", dpi=150)
    fig.savefig(FIG_DIR / "model_r2_comparison.svg")
    plt.close(fig)

    # =========================================================================
    # SUMMARY
    # =========================================================================
    log.info("=== Final Summary ===")
    log.info(f"LightGBM 10-fold CV R² (full model, N=302 metacells): {cv_r2_scores.mean():.3f} ± {cv_r2_scores.std():.3f}")
    log.info(f"Top 10 SHAP features:")
    for _, row in importance_df.head(10).iterrows():
        log.info(f"  {row['feature']}: SHAP={row['mean_abs_shap']:.4f}")
    log.info("Done.")


if __name__ == "__main__":
    main()
