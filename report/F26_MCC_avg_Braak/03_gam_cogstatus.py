#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pandas>=2.0",
#   "pyarrow>=14.0",
#   "numpy>=1.24",
#   "scipy>=1.11",
#   "pygam>=0.9",
#   "matplotlib>=3.7",
#   "seaborn>=0.12",
#   "statsmodels>=0.14",
# ]
# ///
"""
Method 03: Cognitive-status divergence along the avg_Braak axis.

Primary divergence test: fast linear-rank-interaction test (equivalent to
testing whether the Spearman rho between predictor and avg_Braak differs
between Dementia and No-dementia). This is implemented as a permutation test
on rho_Dem - rho_NoDem. Fast (no GAM fitting in permutation loop).

Visualization: GAM curves fitted once per group on a 2000-cell subsample
(one-time cost) — for publication-quality smooth curve overlay plots.

Permutation: N_PERM=200, subsample=1500 per group (permuting group labels).

Cohort restriction: SEAAD + lu + ROSMAP (Mac excluded — no CogStatus data).

Analysis is EXPLORATORY.

Output:
  - gam_cogstatus_results.tsv
  - figs/03_gam_cogstatus/

Software: pygam 0.12.0, scipy 1.17.1, numpy 2.4.3
Random seed: 42.
"""

import logging
import warnings
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy.stats as stats
from pygam import LinearGAM, s

matplotlib.use("Agg")
warnings.filterwarnings("ignore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

RANDOM_SEED = 42
RNG = np.random.RandomState(RANDOM_SEED)

OUT_DIR = Path("/data/1/users/mark/anthive4/report/F26_MCC_avg_Braak")
FIG_DIR = OUT_DIR / "figs" / "03_gam_cogstatus"
FIG_DIR.mkdir(parents=True, exist_ok=True)

N_PERMUTATIONS = 200          # permutations for rho-difference test
SUBSAMPLE_PER_GROUP = 1500    # subsample for permutation loop (speed)
GAM_SUBSAMPLE = 2000          # subsample for GAM curve fitting (visualization)
N_SPLINE_POINTS = 100

COHORTS_WITH_COGSTATUS = {"SEAAD", "lu", "ROSMAP"}

BRAAK_DERIVED_PATTERNS = (
    "avg_Braak", "Braak_numeric", "stage_numeric", "Braak_rank",
    "mean_avg_Braak", "delta_percell", "delta_donorBraak", "delta_allcell",
    "avg_early_pct", "avg_late_pct", "avg_nodem_pct", "avg_dem_pct",
    "avg_Dementia", "avg_Nodementia", "perc_lu_stage", "perc_SEAAD_stage",
    "perc_Mathys_stage", "perc_Gaz_stage", "perc_lu_CogStage",
    "perc_SEAAD_CogStage", "perc_Mathys_CogStage", "perc_lu_Cognitive",
    "perc_SEAAD_Cognitive", "perc_Mathys_Cognitive",
    "perc_ROSMAP_CogStage", "perc_ROSMAP_stage",
)


def load_cogstatus_cells(cells_parquet: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load and split cells by cognitive status (SEAAD/lu/ROSMAP only)."""
    cells_df = pd.read_parquet(cells_parquet)
    log.info(f"All cells loaded: {len(cells_df)}")

    if "batch" in cells_df.columns:
        cogstatus_cells = cells_df[cells_df["batch"].isin(COHORTS_WITH_COGSTATUS)].copy()
    else:
        cogstatus_cells = cells_df.copy()
    log.info(f"SEAAD+lu+ROSMAP cells: {len(cogstatus_cells)}")

    dementia = cogstatus_cells[
        cogstatus_cells["Cognitive.Status"].str.strip() == "Dementia"
    ].dropna(subset=["avg_Braak"]).copy()
    nodementia = cogstatus_cells[
        cogstatus_cells["Cognitive.Status"].str.strip() == "No dementia"
    ].dropna(subset=["avg_Braak"]).copy()

    log.info(f"  Dementia: {len(dementia):,}  |  No-dementia: {len(nodementia):,}")
    return dementia, nodementia


def permutation_test_rho_difference(
    braak_dem: np.ndarray,
    pred_dem: np.ndarray,
    braak_nodem: np.ndarray,
    pred_nodem: np.ndarray,
    n_permutations: int = N_PERMUTATIONS,
    subsample_n: int = SUBSAMPLE_PER_GROUP,
) -> tuple[float, float, float, float, np.ndarray]:
    """Permutation test: does rho_Dem differ from rho_NoDem?

    Uses rank correlation difference as the test statistic.
    Permutes group labels in pooled data.

    Returns:
        rho_dem, rho_nodem, observed_diff, perm_p_value, null_distribution
    """
    valid_dem = ~np.isnan(braak_dem) & ~np.isnan(pred_dem)
    valid_nodem = ~np.isnan(braak_nodem) & ~np.isnan(pred_nodem)

    braak_dem_v = braak_dem[valid_dem]
    pred_dem_v = pred_dem[valid_dem]
    braak_nodem_v = braak_nodem[valid_nodem]
    pred_nodem_v = pred_nodem[valid_nodem]

    if len(braak_dem_v) < 30 or len(braak_nodem_v) < 30:
        return np.nan, np.nan, np.nan, np.nan, np.array([])

    # Observed Spearman rho per group
    rho_dem, _ = stats.spearmanr(braak_dem_v, pred_dem_v)
    rho_nodem, _ = stats.spearmanr(braak_nodem_v, pred_nodem_v)
    observed_diff = abs(rho_dem - rho_nodem)

    # Subsample for permutation loop
    rng_local = np.random.RandomState(RANDOM_SEED)
    n_dem = min(subsample_n, len(braak_dem_v))
    n_nodem = min(subsample_n, len(braak_nodem_v))

    idx_dem = rng_local.choice(len(braak_dem_v), size=n_dem, replace=False)
    idx_nodem = rng_local.choice(len(braak_nodem_v), size=n_nodem, replace=False)

    # Pool subsampled data
    all_braak = np.concatenate([braak_dem_v[idx_dem], braak_nodem_v[idx_nodem]])
    all_pred = np.concatenate([pred_dem_v[idx_dem], pred_nodem_v[idx_nodem]])
    n_dem_sub = len(idx_dem)

    null_diffs = []
    for _ in range(n_permutations):
        perm_idx = rng_local.permutation(len(all_braak))
        perm_braak = all_braak[perm_idx]
        perm_pred = all_pred[perm_idx]

        rho_perm1, _ = stats.spearmanr(perm_braak[:n_dem_sub], perm_pred[:n_dem_sub])
        rho_perm2, _ = stats.spearmanr(perm_braak[n_dem_sub:], perm_pred[n_dem_sub:])
        null_diffs.append(abs(rho_perm1 - rho_perm2))

    null_arr = np.array(null_diffs)
    perm_p = np.mean(null_arr >= observed_diff)
    return rho_dem, rho_nodem, observed_diff, perm_p, null_arr


def fit_gam_for_visualization(
    braak_values: np.ndarray,
    predictor_values: np.ndarray,
    subsample_n: int = GAM_SUBSAMPLE,
) -> LinearGAM | None:
    """Fit GAM on a subsample for visualization purposes only."""
    valid_mask = ~np.isnan(braak_values) & ~np.isnan(predictor_values)
    if valid_mask.sum() < 30:
        return None

    braak_clean = braak_values[valid_mask]
    pred_clean = predictor_values[valid_mask]

    # Subsample
    n_use = min(subsample_n, len(braak_clean))
    idx = RNG.choice(len(braak_clean), size=n_use, replace=False)

    try:
        gam = LinearGAM(s(0, n_splines=15)).gridsearch(
            braak_clean[idx].reshape(-1, 1),
            pred_clean[idx],
            progress=False,
        )
        return gam
    except Exception as exc:
        log.debug(f"  GAM fit failed: {exc}")
        return None


def plot_cogstatus_overlay(
    braak_dem: np.ndarray,
    pred_dem: np.ndarray,
    braak_nodem: np.ndarray,
    pred_nodem: np.ndarray,
    feature_name: str,
    rho_dem: float,
    rho_nodem: float,
    observed_diff: float,
    perm_p: float,
    n_dem: int,
    n_nodem: int,
    out_path: Path,
) -> None:
    """Plot GAM curve overlay (Dem vs NoDem) with difference panel."""
    valid_dem = ~np.isnan(braak_dem) & ~np.isnan(pred_dem)
    valid_nodem = ~np.isnan(braak_nodem) & ~np.isnan(pred_nodem)

    braak_range = np.linspace(
        max(braak_dem[valid_dem].min(), braak_nodem[valid_nodem].min()),
        min(braak_dem[valid_dem].max(), braak_nodem[valid_nodem].max()),
        N_SPLINE_POINTS,
    )

    gam_dem = fit_gam_for_visualization(braak_dem, pred_dem)
    gam_nodem = fit_gam_for_visualization(braak_nodem, pred_nodem)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))

    ax = axes[0]
    plot_n = min(2000, valid_dem.sum())
    idx_plot = RNG.choice(np.where(valid_dem)[0], size=plot_n, replace=False)
    ax.scatter(
        braak_dem[idx_plot], pred_dem[idx_plot],
        alpha=0.12, s=3, color="#d73027",
        label=f"Dementia (N={n_dem:,}, ρ={rho_dem:.3f})", rasterized=True,
    )
    plot_n_nd = min(2000, valid_nodem.sum())
    idx_plot_nd = RNG.choice(np.where(valid_nodem)[0], size=plot_n_nd, replace=False)
    ax.scatter(
        braak_nodem[idx_plot_nd], pred_nodem[idx_plot_nd],
        alpha=0.12, s=3, color="#4575b4",
        label=f"No-dementia (N={n_nodem:,}, ρ={rho_nodem:.3f})", rasterized=True,
    )

    if gam_dem:
        curve_dem = gam_dem.predict(braak_range.reshape(-1, 1))
        ax.plot(braak_range, curve_dem, color="#d73027", linewidth=2.5)
        ci_dem = gam_dem.confidence_intervals(braak_range.reshape(-1, 1))
        ax.fill_between(braak_range, ci_dem[:, 0], ci_dem[:, 1], alpha=0.15, color="#d73027")
    if gam_nodem:
        curve_nodem = gam_nodem.predict(braak_range.reshape(-1, 1))
        ax.plot(braak_range, curve_nodem, color="#4575b4", linewidth=2.5)
        ci_nodem = gam_nodem.confidence_intervals(braak_range.reshape(-1, 1))
        ax.fill_between(braak_range, ci_nodem[:, 0], ci_nodem[:, 1], alpha=0.15, color="#4575b4")

    ax.set_xlabel("avg_Braak")
    ax.set_ylabel(feature_name[:45])
    ax.legend(fontsize=7, loc="best")
    ax.set_title(f"{feature_name[:40]}", fontsize=9)

    ax2 = axes[1]
    if gam_dem and gam_nodem:
        diff_curve = curve_dem - curve_nodem
        ax2.plot(braak_range, diff_curve, color="black", linewidth=2)
        ax2.fill_between(braak_range, 0, diff_curve, where=(diff_curve > 0),
                        alpha=0.3, color="#d73027", label="Dem > NoDem")
        ax2.fill_between(braak_range, 0, diff_curve, where=(diff_curve <= 0),
                        alpha=0.3, color="#4575b4", label="NoDem > Dem")
        ax2.axhline(0, color="gray", linewidth=0.8, linestyle="--")
        ax2.set_xlabel("avg_Braak")
        ax2.set_ylabel("GAM curve diff (Dem − NoDem)")
        p_str = f"perm p={perm_p:.3f}" if not np.isnan(perm_p) else "p=NA"
        ax2.set_title(f"|Δρ|={observed_diff:.3f}, {p_str}", fontsize=9)
        ax2.legend(fontsize=7)

    plt.suptitle(f"Cognitive-status divergence: {feature_name}", fontsize=10)
    plt.tight_layout()
    safe_name = feature_name.replace("/", "_").replace(" ", "_").replace(".", "_")[:55]
    fig.savefig(out_path / f"gam_{safe_name}.png", dpi=150, bbox_inches="tight")
    fig.savefig(out_path / f"gam_{safe_name}.svg", bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    log.info("=== GAM cognitive-status divergence analysis ===")
    log.info(f"N_PERMUTATIONS={N_PERMUTATIONS}, SUBSAMPLE_PER_GROUP={SUBSAMPLE_PER_GROUP}, GAM_SUBSAMPLE={GAM_SUBSAMPLE}")

    dementia_cells, nodementia_cells = load_cogstatus_cells(OUT_DIR / "data_cells.parquet")
    n_dem = len(dementia_cells)
    n_nodem = len(nodementia_cells)

    # Select candidate predictors
    spearman_df = pd.read_csv(OUT_DIR / "spearman_metacell_obs_scores.tsv", sep="\t")

    def is_braak_derived(col_name: str) -> bool:
        return any(col_name.startswith(pat) or pat in col_name
                   for pat in BRAAK_DERIVED_PATTERNS)

    spearman_bio = spearman_df[~spearman_df["feature"].apply(is_braak_derived)].sort_values(
        "rho", key=abs, ascending=False
    )

    # Load SHAP results to supplement
    shap_path = OUT_DIR / "gbm_shap_metacell_results.tsv"
    shap_top_features = []
    if shap_path.exists():
        shap_df = pd.read_csv(shap_path, sep="\t")
        shap_obs = shap_df[~shap_df["feature"].str.startswith("gene__")]
        shap_obs = shap_obs[~shap_obs["feature"].apply(is_braak_derived)]
        shap_top_features = shap_obs.head(20)["feature"].tolist()
        log.info(f"SHAP top obs features loaded: {len(shap_top_features)}")

    spearman_top = spearman_bio["feature"].head(25).tolist()

    # Score columns
    score_cols = [c for c in dementia_cells.columns if "__" in c and any(
        cat in c for cat in ["WGCNA", "MICROGLIA_V1"]
    )]

    # Combine, deduplicate, filter to available
    available_cols = set(dementia_cells.columns) & set(nodementia_cells.columns)
    all_features = list(dict.fromkeys(spearman_top + shap_top_features + score_cols))
    candidate_features = [f for f in all_features if f in available_cols and not is_braak_derived(f)]
    candidate_features = candidate_features[:35]  # cap at 35 for runtime
    log.info(f"Candidate features for divergence test: {len(candidate_features)}")

    # Run divergence tests
    results = []
    for feat_idx, feature_name in enumerate(candidate_features):
        log.info(f"  [{feat_idx+1}/{len(candidate_features)}] {feature_name}")

        braak_dem = dementia_cells["avg_Braak"].values
        pred_dem = dementia_cells[feature_name].values
        braak_nodem = nodementia_cells["avg_Braak"].values
        pred_nodem = nodementia_cells[feature_name].values

        valid_dem_n = (~np.isnan(braak_dem) & ~np.isnan(pred_dem)).sum()
        valid_nodem_n = (~np.isnan(braak_nodem) & ~np.isnan(pred_nodem)).sum()

        if valid_dem_n < 30 or valid_nodem_n < 30:
            log.info(f"    Skipped: n_dem={valid_dem_n}, n_nodem={valid_nodem_n}")
            continue

        rho_dem, rho_nodem, obs_diff, perm_p, null_arr = permutation_test_rho_difference(
            braak_dem, pred_dem, braak_nodem, pred_nodem,
        )

        results.append({
            "feature": feature_name,
            "n_dementia": int(valid_dem_n),
            "n_nodementia": int(valid_nodem_n),
            "rho_dementia": rho_dem,
            "rho_nodementia": rho_nodem,
            "rho_diff_abs": obs_diff,
            "perm_p_value": perm_p,
            "n_permutations": N_PERMUTATIONS,
        })

        # Visualization (GAM fitted once per feature)
        plot_cogstatus_overlay(
            braak_dem, pred_dem, braak_nodem, pred_nodem,
            feature_name=feature_name,
            rho_dem=rho_dem, rho_nodem=rho_nodem,
            observed_diff=obs_diff, perm_p=perm_p,
            n_dem=int(valid_dem_n), n_nodem=int(valid_nodem_n),
            out_path=FIG_DIR,
        )

    results_df = pd.DataFrame(results)

    # BH correction on permutation p-values
    if len(results_df) > 0 and "perm_p_value" in results_df.columns:
        valid_mask = results_df["perm_p_value"].notna()
        p_vals = results_df.loc[valid_mask, "perm_p_value"].values
        n_valid = len(p_vals)
        sort_order = np.argsort(p_vals)
        bh_vals = np.minimum(1.0, p_vals[sort_order] * n_valid / (np.arange(1, n_valid + 1)))
        for idx in range(n_valid - 2, -1, -1):
            bh_vals[idx] = min(bh_vals[idx], bh_vals[idx + 1])
        bh_result = np.empty(n_valid)
        bh_result[sort_order] = bh_vals
        results_df.loc[valid_mask, "perm_p_adj_bh"] = bh_result
        results_df.loc[~valid_mask, "perm_p_adj_bh"] = np.nan

    results_df = results_df.sort_values("rho_diff_abs", ascending=False)
    results_df.to_csv(OUT_DIR / "gam_cogstatus_results.tsv", sep="\t", index=False)
    log.info(f"Saved divergence results: {len(results_df)} features")

    # Summary figures
    if len(results_df) > 0:
        top_div = results_df.dropna(subset=["rho_diff_abs"]).head(30).sort_values("rho_diff_abs")
        colors_bar = [
            "#d73027" if p < 0.05 else "#aaaaaa"
            for p in top_div.get("perm_p_adj_bh", pd.Series([1.0] * len(top_div)))
        ]

        fig, ax = plt.subplots(figsize=(9, max(6, len(top_div) * 0.28)))
        ax.barh(top_div["feature"], top_div["rho_diff_abs"], color=colors_bar)
        ax.set_xlabel("|Δρ| = |ρ_Dementia − ρ_No-dementia|")
        ax.set_title(
            f"Cognitive-status divergence along avg_Braak\n"
            f"(N_Dementia={n_dem:,}, N_Nodementia={n_nodem:,}, SEAAD+lu+ROSMAP)\n"
            f"Red = BH perm-p < 0.05 (N_perm={N_PERMUTATIONS})",
            fontsize=9,
        )
        ax.tick_params(axis="y", labelsize=8)
        plt.tight_layout()
        fig.savefig(FIG_DIR / "divergence_barplot.png", dpi=150)
        fig.savefig(FIG_DIR / "divergence_barplot.svg")
        plt.close(fig)
        log.info("Saved divergence barplot")

        # rho scatter: Dem vs NoDem
        fig, ax = plt.subplots(figsize=(7, 7))
        scatter_c = results_df["rho_diff_abs"].fillna(0)
        sc = ax.scatter(
            results_df["rho_nodementia"],
            results_df["rho_dementia"],
            c=scatter_c, cmap="YlOrRd", s=60, alpha=0.85,
        )
        plt.colorbar(sc, ax=ax, label="|Δρ|")
        ax.plot([-1, 1], [-1, 1], color="gray", linewidth=0.8, linestyle="--")
        ax.axhline(0, color="black", linewidth=0.4)
        ax.axvline(0, color="black", linewidth=0.4)
        ax.set_xlabel("Spearman ρ (No-dementia)")
        ax.set_ylabel("Spearman ρ (Dementia)")

        for _, row in results_df.head(10).iterrows():
            if not np.isnan(row["rho_nodementia"]) and not np.isnan(row["rho_dementia"]):
                ax.annotate(
                    row["feature"][:28],
                    (row["rho_nodementia"], row["rho_dementia"]),
                    fontsize=6, alpha=0.85,
                )

        ax.set_title(
            "Per-group Spearman ρ: off-diagonal = divergent trajectories\n"
            f"(N_perm={N_PERMUTATIONS}, color = |Δρ|)",
            fontsize=9,
        )
        plt.tight_layout()
        fig.savefig(FIG_DIR / "rho_scatter_dem_vs_nodem.png", dpi=150)
        fig.savefig(FIG_DIR / "rho_scatter_dem_vs_nodem.svg")
        plt.close(fig)
        log.info("Saved rho scatter")

    log.info("=== Summary ===")
    log.info(f"N_Dementia (SEAAD+lu+ROSMAP): {n_dem:,}")
    log.info(f"N_Nodementia: {n_nodem:,}")
    log.info(f"Features tested: {len(results_df)}")
    if "perm_p_adj_bh" in results_df.columns:
        sig = results_df["perm_p_adj_bh"].lt(0.05).sum()
        log.info(f"Significant divergence (BH q<0.05): {sig}")
    log.info("Top 10 features by |Δρ|:")
    for _, row in results_df.head(10).iterrows():
        p_str = f"{row.get('perm_p_adj_bh', 'NA'):.3f}" if not pd.isna(row.get("perm_p_adj_bh")) else "NA"
        log.info(
            f"  {row['feature']}: |Δρ|={row['rho_diff_abs']:.3f}, "
            f"ρ_Dem={row['rho_dementia']:.3f}, ρ_NoDem={row['rho_nodementia']:.3f}, "
            f"BH-p={p_str}"
        )
    log.info("Done.")


if __name__ == "__main__":
    main()
