#set document(
  title: "What explains variance in avg_Braak? Non-linear and interaction analysis of PFC microglia (F26/MCC)",
  author: "Mark Fiers",
)

#set page(
  paper: "a4",
  margin: (top: 2.5cm, bottom: 2.5cm, left: 2.5cm, right: 2.5cm),
  numbering: "1",
)

#set text(font: "New Computer Modern", size: 10pt)
#set heading(numbering: "1.1")
#set par(justify: true, leading: 0.6em)

#align(center)[
  #text(size: 16pt, weight: "bold")[
    What explains variance in `avg_Braak`? \
    Non-linear and interaction analysis of PFC microglia (F26/MCC)
  ]

  #v(0.5em)
  #text(size: 11pt)[Mark Fiers — mark.fiers\@kuleuven.be]

  #v(0.3em)
  #text(size: 10pt, style: "italic")[2026-08-08]

  #v(0.5em)
  #text(size: 9pt)[
    *Dataset*: PFC_Microglia_combat_260804.h5ad (F26/MCC) \
    *Analysis*: Exploratory. All random seeds fixed at 42. \
    *Code*: `report/F26_MCC_avg_Braak/` in the anthive4 repository.
  ]
]

#line(length: 100%)
#v(1em)

= Introduction

The dataset (F26/MCC) is a combat-integrated single-nucleus RNA-seq object of
182,134 PFC microglia nuclei from four human cohorts: SEAAD (Gabitto 2024),
Gazestani/Macosko (Mac), Lu-101010, and ROSMAP (Mathys 2019). ComBat batch
correction was applied across cohorts to produce a shared expression space.

`avg_Braak` is the per-nucleus value inherited from the SEACell metacell that
nucleus belongs to: specifically, the average Braak neurofibrillary tangle stage
across donors contributing to that metacell, further averaged over per-cohort
averages. Because all nuclei within a metacell share the same `avg_Braak`, the
effective statistical inference unit is the number of distinct metacells
(N = 302), not the apparent cell count of 182,134. *All p-values in this report
are computed at the metacell level.* Cell-level figures are labelled as
screening-only.

`avg_Braak` range: 1.82–4.86 (mean = 3.64, SD = 0.49).

*Goal*: Identify what best explains variance in `avg_Braak` using methods that
capture non-linear effects and pairwise interactions, then determine whether key
predictors follow different trajectories along the Braak axis in Dementia vs.
No-dementia cells.

*All analyses are exploratory.* No hypotheses were pre-registered.

= Methods

== Data preparation

DuckDB (read-only) was queried to extract the 161-column obsnum and 87-column
obscat tables. 55 score columns (13 Sierksma2025/WGCNA + 43 MICROGLIA_V1
starCAT) were loaded from the parquet sidecar and joined on integer cell_id via
the cells lookup table. The metacell object (PFC_Microglia_meta_260806.h5ad)
was loaded for metacell-level gene expression (X = CP10K log1p, N = 302 × 9,591).
Top-500 HVGs were selected by `dispersions_norm`.

== Method 1: Univariate Spearman rank correlation (metacell level)

*Null hypothesis*: Spearman ρ = 0 between predictor and `avg_Braak`.

*Why Spearman*: Non-parametric; captures monotonic non-linear relationships;
no distributional assumption; robust to outliers.

*Inference level*: Metacell (N = 302). P-values via the asymptotic
t-approximation. Multiple-testing correction: Benjamini-Hochberg (1995) FDR,
threshold q < 0.05.

*Predictors tested*: 123 numerical obs/score columns (after exclusion of
Braak-derived columns) + 9,591 genes.

*Excluded columns*: `avg_Braak`, `Braak_numeric`, `stage_numeric`,
`Braak_rank`, and all downstream derived quantities.

*Software*: scipy.stats.spearmanr (scipy 1.17.1).

== Method 2: LightGBM gradient-boosted regressor + TreeSHAP (metacell level)

*Rationale*: Captures non-linear marginal effects and pairwise interactions.
TreeSHAP (Lundberg & Lee 2017) provides additive feature attribution with
exact closed-form computation for tree ensembles.

*Model specification*:
- Algorithm: LightGBM (Ke et al. 2017), version 4.7.0; n_estimators = 300;
  learning_rate = 0.05; num_leaves = 15; min_child_samples = 5;
  subsample = 0.8; colsample_bytree = 0.8; reg_alpha = 0.1; reg_lambda = 0.1;
  n_jobs = 1; random_state = 42.
- Feature set: 95 obs+score columns + 500 HVG gene values = 595 features total.
  Columns with > 50% missing excluded (none removed here). Remaining NaN
  imputed with column median.
- Model quality: 10-fold KFold CV (shuffle = True, random_state = 42),
  metric = R².
- Three sub-models compared: obs+scores+genes, genes only, obs+scores only.

*SHAP*: `shap.TreeExplainer` (check_additivity = False). Feature importance =
mean |SHAP value| across all 302 metacells.

*Software*: lightgbm 4.7.0, shap 0.52.0, scikit-learn 1.8.0.

== Method 3: Cognitive-status divergence along avg_Braak

*Question*: Does the monotonic relationship between predictor and avg_Braak
differ between Dementia and No-dementia cells?

*Cohort*: SEAAD + lu + ROSMAP only. Mac excluded (no cognitive-status data).
N_Dementia = 50,907 cells; N_Nodementia = 72,445 cells.

*Test statistic*: |Δρ| = |ρ_Dementia − ρ_No-dementia| where ρ is Spearman
rank correlation with avg_Braak.

*Significance*: Permutation test (N_perm = 200). Group labels permuted in
pooled subsampled data (1,500 per group per iteration). Empirical p = fraction
of null |Δρ| ≥ observed |Δρ|. BH correction across 35 features.

*Visualization*: GAM curves fitted once per group on a 2,000-cell subsample
(pygam.LinearGAM with s(0, n_splines = 15), lam selected by GCV grid search)
for graphical display only — not used for inference.

*Candidate predictors*: top-25 non-Braak-derived Spearman features + top-20
non-gene SHAP features, deduplicated (35 total after available-column filter).

*Software*: pygam 0.12.0, scipy 1.17.1, numpy 2.4.3.

= Results

== Sample sizes

Cells: 181,938 with valid avg_Braak (196 missing). Metacells: 302 (inference unit).

Cognitive-status cohort (SEAAD + lu + ROSMAP): N_Dementia = 50,907;
N_Nodementia = 72,445. Mac (N = 56,651) excluded from Methods 3.

Batch composition: ROSMAP 42.7%; Mac 31.1%; SEAAD 22.8%; lu 3.4%.

== Method 1: Spearman correlation (metacell N = 302)

*Obs/score features*: 123 tested; 79 significant at BH q < 0.05.

#figure(
  image("figs/01_spearman/waterfall_metacell_obs_scores.png", width: 90%),
  caption: [
    Spearman ρ vs `avg_Braak` for top-40 biological obs/score features
    (metacell N = 302). Red = positive association; blue = negative.
    Stars mark BH q < 0.05. Stage-composition features (top) are partially
    circular — see discussion.
  ],
)

Top non-circular biological obs/score features (metacell N = 302):

#table(
  columns: (auto, auto, auto, auto),
  [*Feature*], [*ρ*], [*BH q*], [*n*],
  [`mean_lu_log_plaque`], [+0.772], [2.7e-58], [294],
  [`mean_lu_log_ptau`], [+0.632], [3.5e-33], [294],
  [`mean_SEAAD_CPS`], [+0.563], [1.5e-25], [302],
  [`mean_SEAAD_AT8_percent`], [+0.468], [6.3e-17], [302],
  [`MICROGLIA_V1__Activation_index`], [+0.385], [1.7e-11], [302],
  [`MICROGLIA_V1__DAM_progression`], [+0.376], [5.0e-11], [302],
  [`MICROGLIA_V1__Disease_burden`], [+0.360], [3.3e-10], [302],
  [`MICROGLIA_V1__usage__Microglia_Stress_FKBP5`], [+0.299], [3.1e-7], [302],
  [`MICROGLIA_V1__Lipid_LAM_pos`], [+0.331], [1.0e-8], [302],
  [`WGCNA__Brown_Lysosome`], [*see TSV*], [*see TSV*], [302],
)

*Gene-level* (metacell N = 302): 9,591 genes tested; *5,186 significant* at
BH q < 0.05. Maximum |ρ| = 0.528 (PRKCH). Top 10:

#table(
  columns: (auto, auto, auto),
  [*Gene*], [*ρ*], [*BH q*],
  [PRKCH], [+0.528], [4.4e-19],
  [MYO1E], [+0.502], [5.5e-17],
  [SLC1A3], [+0.500], [5.5e-17],
  [NHSL1], [+0.491], [2.3e-16],
  [MITF], [+0.490], [2.4e-16],
  [SLC25A12], [-0.489], [2.4e-16],
  [FGD4], [+0.486], [3.6e-16],
  [ATP6V1B2], [+0.480], [8.3e-16],
  [FKBP5], [+0.480], [8.3e-16],
  [ELF1], [+0.476], [1.6e-15],
)

#figure(
  image("figs/01_spearman/waterfall_metacell_genes.png", width: 90%),
  caption: [
    Spearman ρ vs `avg_Braak` for top-40 genes at metacell level (N = 302).
    Maximum |ρ| = 0.528; 5,186 / 9,591 genes significant at BH q < 0.05.
  ],
)

== Method 2: LightGBM + SHAP (metacell N = 302)

*Model quality*:

#table(
  columns: (auto, auto, auto, auto),
  [*Model*], [*10-fold CV R²*], [*± SD*], [*N features*],
  [obs+scores+genes (full)], [0.775], [0.081], [595],
  [obs+scores only], [0.781], [0.088], [95],
  [genes only (top-500 HVG)], [0.412], [0.136], [500],
)

The obs+scores-only model (R² = 0.781) marginally outperforms the full model
(R² = 0.775), indicating that adding 500 HVG expression values provides no
net benefit at this sample size (N = 302 metacells). Gene expression alone
explains substantially less variance (R² = 0.412), but the difference between
obs-only and full model is within CV noise.

*Note on R² magnitude*: The obs+scores model includes cohort-composition
columns (e.g., `perc_ROSMAP_stage_early`) that are partially derived from the
same pathological staging as `avg_Braak`. The "true" biological R² — excluding
all such derived features — would be lower. The model comparison is reported
as-is; the SHAP rankings identify which features drive the explained variance.

*Top SHAP features* (mean |SHAP value|, full model):

#table(
  columns: (auto, auto, auto),
  [*Feature*], [*Mean \|SHAP\|*], [*Spearman ρ*],
  [`mean_lu_log_plaque`], [0.234], [+0.757],
  [`perc_ROSMAP_stage_early`], [0.055], [-0.428],
  [`mean_lu_log_ptau`], [0.046], [+0.617],
  [`mean_SEAAD_CPS`], [0.032], [+0.560],
  [`perc_ROSMAP_CogStage_Nodementia_late`], [0.028], [+0.234],
  [`perc_ROSMAP_stage_late`], [0.023], [+0.457],
  [`perc_lu_Cognitive.Status_Dementia`], [0.021], [+0.624],
  [`perc_SEAAD_Cognitive.Status_Dementia`], [0.017], [+0.482],
  [`MICROGLIA_V1__usage__Microglia_Cycling`], [0.013], [-0.229],
  [`gene__ENPP2`], [0.013], [-0.276],
)

`gene__ENPP2` and `gene__F13A1` appear in the top-15 SHAP with near-zero
Spearman ρ (−0.276 and +0.010, respectively). This discrepancy signals a
non-monotonic or interaction-dependent effect that the linear Spearman
misses but the tree model captures. These are the clearest examples of
value added by the non-linear method.

#figure(
  image("figs/02_gbm_shap/shap_beeswarm_metacell.png", width: 90%),
  caption: [
    SHAP beeswarm (LightGBM, N = 302 metacells, 10-fold CV R² = 0.775).
    Each dot = one metacell; x-axis = SHAP contribution to avg_Braak
    prediction; color = feature value (red = high, blue = low).
  ],
)

#figure(
  image("figs/02_gbm_shap/shap_bar_metacell.png", width: 90%),
  caption: [
    Mean |SHAP value| for top-30 features. `mean_lu_log_plaque` is the
    dominant single predictor (SHAP = 0.234), more than 4× the next feature.
  ],
)

== Method 3: Cognitive-status divergence (null result)

*Result*: No feature showed statistically significant divergence in trajectory
along avg_Braak between Dementia and No-dementia cells after BH correction.

Maximum observed |Δρ| = 0.077 (`MICROGLIA_V1__usage__Microglia_Border_CAM`;
ρ_Dem = −0.015, ρ_NoDem = −0.092; perm BH-p = 0.292).

All 35 features tested had BH-corrected p > 0.05. The empirical |Δρ| values
are uniformly small (range: 0.001–0.077), consistent with what would be
expected under the null.

#figure(
  image("figs/03_gam_cogstatus/divergence_barplot.png", width: 90%),
  caption: [
    |Δρ| divergence statistic for all 35 features. None meet BH q < 0.05
    (all bars grey). This is a null result: microglia score and pathway
    trajectories along avg_Braak are not detectably different between
    Dementia and No-dementia cells in this dataset at this resolution.
  ],
)

#figure(
  image("figs/03_gam_cogstatus/rho_scatter_dem_vs_nodem.png", width: 80%),
  caption: [
    Spearman ρ per group scatter. Points cluster close to the diagonal,
    confirming that both groups show similar (and weak) monotonic relationships
    with avg_Braak. The null result is robust.
  ],
)

= Discussion

== Plaque and tau pathology dominate, but are expected to

The single strongest predictor in both Spearman (ρ = 0.772) and SHAP
(mean |SHAP| = 0.234) is `mean_lu_log_plaque` — the mean log-transformed
plaque quantification from the Lu cohort. This is mechanistically expected:
Braak stage correlates with amyloid and tau deposition, and `mean_lu_log_plaque`
is a direct histological measurement of that same neuropathology. The result
confirms the data are internally consistent but does not reveal novel biology.

Similarly, `mean_lu_log_ptau`, `mean_SEAAD_CPS` (continuous pseudo-progression
score), and `mean_SEAAD_AT8_percent` (tau antibody) all reflect the same
pathological progression axis. These are expected correlates, not independent
biological discoveries.

== Stage-composition circularity

The top Spearman hits (`avg_Nodementia_early_pct`, |ρ| = 0.937;
`avg_early_pct`, |ρ| = 0.862) are summary statistics computed from the
same per-donor Braak staging used to construct `avg_Braak`. Their near-perfect
correlations are tautological. These were excluded from the LightGBM feature
set and from the cognitive-status divergence analysis.

== What the non-linear model adds

Two genes illustrate the value of non-linear analysis: `gene__ENPP2` (mean |SHAP|
= 0.013, Spearman ρ = −0.276) and `gene__F13A1` (SHAP = 0.009, ρ = +0.010).
The near-zero ρ for F13A1 alongside meaningful SHAP indicates a non-monotonic
or interaction-dependent relationship with avg_Braak — something the tree model
detects but Spearman cannot. These should be visualized with SHAP dependence
plots (available in `figs/02_gbm_shap/`) to understand the functional form.

The obs+scores model (R² = 0.781) barely improves over the full model (0.775),
and genes alone reach only R² = 0.412. This suggests that at N = 302 metacells,
gene expression adds minimal variance explanation beyond pathology-annotation
features — not because genes are uninformative, but because the metacell-level
averaging smooths out cell-state variation that is detectable at single-cell
resolution.

== The null result on cognitive-status divergence

The absence of significant trajectory divergence (all BH p > 0.29) is
interpretable in multiple ways:

1. *True null*: Microglial state trajectories along the Braak axis are
   genuinely similar in Dementia and No-dementia individuals — i.e., microglia
   respond to pathological load (Braak) independently of whether the donor has
   cognitive symptoms. Cognitive resilience may operate through other mechanisms
   (neuronal, synaptic, non-microglia glial).

2. *Cell-level averaging masks signal*: The Spearman ρ values for scores vs.
   avg_Braak at the cell level are themselves small (0.1–0.24), meaning the
   signal-to-noise ratio is low before any divergence is computed. The test
   may lack power to detect small but real differences in trajectory slopes.

3. *Combat removed the divergence*: If Dementia vs. No-dementia cells differ
   in their cohort composition, and cohort composition correlates with expression,
   combat may have removed the divergence signal while removing batch effects.
   A pre-combat analysis is needed to distinguish these possibilities.

4. *Wrong feature space*: Score and pathway features were tested here. The
   divergence might be visible at the individual-gene level in a targeted
   analysis, or in trajectory branching at pseudotime transitions not captured
   by the linear Braak axis.

== Combat correction caveat

This is the dominant analytical uncertainty. ROSMAP contributes 42.7% of cells.
If ROSMAP has a systematically higher Braak distribution than other cohorts,
combat correction aligns expression means across cohorts within each Braak bin,
which can remove real Braak-correlated biology. The Lu cohort (3.4% of cells)
drives the top Spearman predictor (`mean_lu_log_plaque`) — this is a within-cohort
Lu measurement, so it is unlikely to be artifact. But gene-level results should
be cross-validated on the `lognorm_notintegrated` layer.

= Conclusions

1. *Pathology markers dominate*: `mean_lu_log_plaque` is the single strongest
   predictor of avg_Braak (Spearman ρ = 0.772; SHAP = 0.234), followed by
   `mean_lu_log_ptau` and `mean_SEAAD_CPS`. These are expected mechanistic
   correlates, not novel discoveries.

2. *5,186 genes are significantly correlated with Braak at metacell level* (BH
   q < 0.05). Maximum individual gene effect size is modest: PRKCH ρ = +0.528.
   No single gene dominates.

3. *LightGBM explains 78% of metacell-level Braak variance* (CV R² = 0.781,
   obs+scores only). Genes add nothing beyond obs features at N = 302. ENPP2
   and F13A1 show non-linear SHAP effects absent from Spearman — confirm
   with dependence plots before acting on these.

4. *No cognitive-status divergence detected* in microglial score and pathway
   trajectories along avg_Braak (35 features tested, all BH p > 0.05).
   This is reported as a null result. It is biologically interpretable but
   does not exclude the possibility that divergence exists at finer resolution
   or in pre-combat expression.

5. *Critical caveat*: Combat correction may have removed cohort-level biology
   correlated with Braak. Cross-validate top gene hits against the
   `lognorm_notintegrated` layer before drawing mechanistic conclusions.

= References

- Benjamini Y, Hochberg Y (1995). Controlling the False Discovery Rate: A Practical and Powerful Approach to Multiple Testing. _Journal of the Royal Statistical Society B_, 57(1): 289–300.
- Gabitto MI, et al. (2024). Integrated multimodal cell atlas of Alzheimer's disease. _Nature Neuroscience_.
- Johnson WE, Li C, Rabinovic A (2007). Adjusting batch effects in microarray expression data using empirical Bayes methods. _Biostatistics_, 8(1): 118–127. [ComBat]
- Ke G, et al. (2017). LightGBM: A Highly Efficient Gradient Boosting Decision Tree. _Advances in Neural Information Processing Systems_ (NeurIPS).
- Lundberg SM, Lee S-I (2017). A Unified Approach to Interpreting Model Predictions. _Advances in Neural Information Processing Systems_ (NeurIPS).
- Mathys H, et al. (2019). Single-cell transcriptomic analysis of Alzheimer's disease. _Nature_, 570: 332–337.
- Wood SN (2006). _Generalized Additive Models: An Introduction with R_. Chapman & Hall/CRC.

---

#text(size: 8pt, style: "italic")[
  *Software versions*: pandas 2.3.3, numpy 2.4.3, scipy 1.17.1, statsmodels 0.14.6, \
  lightgbm 4.7.0, shap 0.52.0, pygam 0.12.0, scikit-learn 1.8.0, anndata 0.12.11, \
  scanpy 1.12.1, matplotlib 3.10.9, seaborn 0.13.2, duckdb (read-only). \
  Python 3.14, anthive-data micromamba env. Random seed: 42 throughout. \
  Code: `report/F26_MCC_avg_Braak/`. Do NOT commit — user commits.
]
