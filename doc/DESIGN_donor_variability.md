# Donor variability (2026-10-09, API 2.45, exploratory)

How much do donors differ from each other in a cell type, gene by gene and module by module, once the noise of counting
cells is removed, and how much of that is left after what we know about the donors. Tool `bin/annot/ant-donor-variability`,
estimator `lib/donor_variance.py`, reader `lib/donor_variability.py`, endpoints and MCP tool in `doc/API_CHANGELOG.md`
(2.45.0), plain wording for users in the help topic `donor-variability`. The statistics follow the second opinion by Fable
(`fable_variability.md`, 2026-10-05) with the deviations listed below. Phase 1 and 2 only: the web panel and the fleet run are
not part of this change.

## What a replicate is

The dataset's `replicate_unit` (`lib/replicates.py`): the donor for post-mortem tissue, the host animal for xenografts. For a
xenograft "donor variability" is therefore between-animal (host and graft engraftment) variability, and grafts of one iPSC line
carry no genetic donor variance. Scopes are never mixed (`lib/facet_scopes.py`; every row carries `facet_scope` and
`replicate_unit`, every endpoint answers one scope). Datasets outside the facet layer (`facet_exclude`, spatial, proteomics,
mouse only, in vitro, secondary) are skipped by `dataset_eligibility`; the `facet_donor_exclusions` rule of the `.md` is applied
as in the facet tests. A raw counts layer is required (the technical block and TMM need it).

## Estimator (as implemented)

Per (dataset, stratum) with at least 12 replicates of at least 40 cells (`--min-cells 20` is the sensitivity run):

1. **Quarters.** The cells of a replicate are sorted by `splitmix64(cell_id, seed)` and dealt `rank mod 4`: random, balanced to
   one cell, independent of row order and thread count. Counts are summed per (replicate, quarter) in DuckDB. A rerun with the
   same seed is bitwise identical (tested).
2. **Halves.** The three balanced pairings of the quarters `(01|23)`, `(02|13)`, `(03|12)` give three `(x_A, x_B)` pairs per
   replicate. They are three correlated looks at the same cells, not three replicates, so every statistic is averaged over the
   pairings and the bootstrap resamples replicates, never cells or pairings.
3. **Normalisation.** TMM factors (edgeR algorithm, own numpy implementation, reference by upper quartile) are computed once per
   replicate on the full pseudobulk of the gate genes and applied to both halves; library size is the all-gene total of the half.
   Scale: edgeR `cpm(log = TRUE)`, prior count 2 scaled by library size.
4. **Gates.** A gene is reported when its expected count per half reaches 10 in at least 30 percent of the replicates (70th
   percentile of replicate count / 2), a gene is in the module matrix when it reaches 1. The review asked for the median
   replicate; the 70th percentile keeps genes of one sex (UTY, RPS4Y1, DDX3Y, KDM5D) in cohorts with fewer than half males and
   costs nothing elsewhere (the reliability, reported for every gene, is the quality filter).
5. **`V_donor`** is the covariance of `x_A` and `x_B` over replicates (centred, `n - 1`), averaged over the pairings: unbiased for
   the variance of the replicates' true half-library log2 expression without the sampling noise. `reliability` is the
   Spearman-Brown correction `2r / (1 + r)` of the split-half correlation `r` (clipped to [0, 1]); `sd_donor = sqrt(max(V, 0))`
   in log2 units; `fold_range = 2 ** (2 sd)` is what the API shows for biologists. Intervals: 200 bootstrap replicates over
   replicates (fewer for huge matrices, `BOOT_BUDGET`; the manifest records the number used), percentile 2.5 and 97.5.
6. **Unexplained variance.** With `H` the projector on `[1, X]`: `V_unexplained = r_A' r_B / (n - p)` (residual cross-product of
   the halves), `share_design = 1 - V_unexplained / V_donor` (unclipped value can leave [0, 1] by sampling; the file stores the
   clipped value; `V_donor` is recomputed on the replicates of the rung when listwise deletion removed some). `p` is the numeric
   rank including the intercept.
7. **Rungs** (nested, each computed only when `n - p >= 20`; a rung with no new term is skipped; replicate counts per rung in
   the manifest):

   | rung | adds | terms |
   | --- | --- | --- |
   | D0 | technical block | `technical`: log cells, log UMI per cell, log genes per cell, sqrt mitochondrial fraction, ambient score (mean log2 CPM of the marker panel of every lineage except the stratum's own: `AMBIENT_PANELS`), long gene index (mean log2 CPM of the 500 longest minus the 500 shortest annotated genes, GENCODE GTF of `gene_classes`, left out without a GTF); `composition`: first 3 PCs of the replicate x author cluster proportion matrix (centred log ratio) when the dataset has author clusters |
   | D1 | covariates | sex (host sex for xenografts), age, PMI, batch (donor level, at most `n / 4` levels) |
   | D2 | disease | `ant_disease` AD vs control (xenografts: App NL-G-F genotype) |
   | D3 | APOE | e4 carrier (human) |
   | D4 | Braak | numeric (human) |
   | F | latent factors | 3 (n < 60), 4 (n < 100) or 5 PCs of the residual replicate matrix (genes scaled), n >= 30; modules reuse the factors of the genes of the same stratum |

   A covariate needs a value for 90 percent of the replicates (the facet tests' rule), two distinct values and, if binary, 3
   replicates per level; replicates without a value leave the rungs that need it (listwise), not the unit. The composition PCs
   are part of D0 because for an eQTL-like question composition is a nuisance; the per-gene correlation with the first PC is
   stored (`r_composition_pc1`) so the goal "donor state" can read the same file at `raw`.
8. **Per-term shares.** At the deepest rung before the factors, when `n - p >= 30`: Shapley (LMG) values of the explained
   fraction `1 - V_unexplained(S) / V_donor` over the terms (`technical`, `composition`, `sex`, `age`, `pmi`, `batch`, `disease`,
   `apoe4`, `braak`; the batch dummies are one term). They add up to the explained fraction of the full model (tested, exactly),
   can be slightly negative by sampling, and are stored unclipped as `lmg_<term>`. Block increments (`share_technical`,
   `share_covariates`, `share_facets`, `share_latent`) are differences of the clipped cumulative shares, floored at 0. Below
   `n - p = 30` only the cumulative shares exist.
9. **Ranking.** `excess_z`: robust local linear (tricube, bisquare) trend of `log V_unexplained` against mean log2 CPM among genes
   with reliability >= 0.3 and V > 0, residual divided by 1.4826 MAD; computed for `raw` (V_donor) and every computed rung, from 20
   replicates. `p_excess` is the share of usable genes with a residual at least as large (the gene itself counted). These are
   ranks inside one unit. There is no `p_donor`: nearly every expressed gene rejects `V_donor = 0` at n >= 20 and a p value would
   say nothing.

## Modules

Primary variant `pseudobulk`: the score of a replicate half is the mean log2 CPM of the module genes (at least 3 in the expressed
matrix) minus the mean over an expression matched control set (`ant-facet-test-modules` bins: 25 bins of mean expression, 50
controls per gene, module genes excluded, seed from the stratum and module). The score is linear in log2 CPM, so a half score is
`x_half @ W` and the whole estimator applies unchanged, with the same TMM, quarters, terms and factors as the genes of the stratum.
Secondary variant `cell_mean`: the weighted mean of the per-cell scores of `<stem>.scores.parquet` (sources `geneset`, `ucell`) over
the cells of the two quarters of a half; modules with an empty half are dropped (counted in the manifest). A module is analysed
only in strata of its cell type scope (`lib/celltype_scope.py`, `ant-facet-test-modules.module_in_stratum`; markers panels never).
The module technical covariates are those of the stratum: the donor mean of a per-cell score tracks depth and genes per cell, so
the block is mandatory. Modules have no mean-variance trend, hence no `excess_*`, and no cross-cohort test (the top list is
descriptive). For a state module most donor variability is the donor's fraction of cells in that state, which is a real donor
trait; `r_composition_pc1` is not computed for modules, read the genes of the state.

## Cross-cohort

Per (scope, cell type group, rung): the counted cohort anchors (`DonorVariability.units`: the dataset is a counted cohort of the
cohort table, not a secondary, mapped group, at least 30 replicates, one unit per cohort and group, the most direct stratifier
first), `p_excess` of each anchor, Fisher combination over the anchors that rank the gene (a rank product, at least two),
BH over the genes (`lib/replication.py` for BH). `k` counts the cohorts in which the gene is in the top 5 percent. The
review proposed the partial conjunction of Benjamini and Heller on rank based p values; it cannot work here: a rank p value is
uniform over genes by construction and never below 1 / (number of genes), the partial conjunction p value of a gene is at least
the second best rank p, and BH over the genes then gives an r-value of about 1 for every gene (seen on the validation units
with Simes). The Fisher rank product tests the relative null "the ranks of a gene in different cohorts are independent", that
is, whether the excess reproduces; it is not a statement about any single cohort being non-null. `concordance` (mean Spearman
correlation of `excess_z` between anchors) summarises the same question over all genes. The statement is exploratory: cohorts
that share donors or cell states are not independent (the facet tests' overlap caveats apply; Fisher is liberal then), and
technical differences between replicates are only removed from rung D0 on.

## Outputs

`<h5adstore>/_facet_tests/<group>/<stem>.donor_variability.parquet` (genes: unit keys, `gene`, `ave_expr`, `half_count_q70`,
`v_donor` + CI, `sd_donor`, `var_half`, `r_half`, `reliability` + CI, per rung `v_unexplained_<R>` and `share_design_<R>`,
`share_design_lo/hi` and `v_unexplained_lo/hi` (deepest rung before factors), block shares, `lmg_<term>`, `excess_z_<R>`,
`p_excess_<R>`, `r_composition_pc1`; float32, sorted by gene, row groups of 20000 rows so one gene is a row-group lookup),
`.donor_variability_modules.parquet` (the same columns for modules plus `method`, `source`, `catalog`, `module`,
`n_genes_found`, `n_control_genes`) and `.donor_variability_manifest.json` (tool version, seed, `md_sha256`, the gates and
rungs in `parameters`, and per unit: replicates, terms and the reason for omitted ones, technical covariates used, composition
PCs, `rungs` with n, p, residual df, computed and reason, factors, genes passing the gate, module counts, runtime). No replicate
id is written anywhere. `ant-sync` publishes the files with the rest of `_facet_tests/`.

## Not built (and why)

* **Library-split `V_donor`** (cell split versus library split, to measure the technical component directly) needs the library
  of every cell, which the obs tables of most datasets do not carry; the technical block and the factors are the substitute.
* **variancePartition on 10 strata** as an external check: not run here (no R package in the engine environments); the planted
  truths of the simulation suite and the positive controls below stand in. It remains a sensible check before the fleet run.
* **Precision weights** (voom style) for the OLS residualisation: unweighted; the moment estimator stays unbiased, the shares are
  less efficient.
* **Bimodality / zero-class flag** for deletion polymorphisms: not computed; `reliability` high with `share_design` near 0 and a
  high `excess_z` is the current signature (GSTM1 is below the expression gate in microglia).
* **Cross-region covariance** for multi-region datasets: not computed; units are per file.
* **Donor-pooled (`pool:`) units**: none, as for the module facet tests.

## Simulation (tests/test_donor_variability.py, src/donor_variability_simulation_grid.py)

Gamma-Poisson counts, known donor variance (SD 0.2 log2), a planted facet effect on a third of the genes, 6 datasets per cell,
300 genes, 100 bootstrap replicates; truth is the realised variance of the true log2 proportion across the simulated
replicates (`ratio_v` = mean estimate / mean truth; coverage of the 95 percent interval; `bias` of the reliability; share
columns for D2 with a random technical covariate and the facet, replicates >= 50):

| n | cells | ratio_v | cov_v | bias reliability | cov reliability | bias share | cov share |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 12 | 40 | 0.973 | 0.863 | -0.054 | 0.935 | | |
| 12 | 80 | 0.989 | 0.918 | -0.025 | 0.976 | | |
| 12 | 113 | 0.996 | 0.929 | -0.018 | 0.976 | | |
| 20 | 40 | 0.962 | 0.914 | -0.037 | 0.966 | | |
| 20 | 80 | 0.975 | 0.942 | -0.019 | 0.980 | | |
| 20 | 113 | 0.979 | 0.952 | -0.012 | 0.986 | | |
| 50 | 40 | 0.958 | 0.939 | -0.021 | 0.975 | 0.010 | 0.996 |
| 50 | 80 | 0.980 | 0.974 | -0.008 | 0.994 | 0.003 | 0.995 |
| 50 | 113 | 0.982 | 0.978 | -0.006 | 0.991 | 0.002 | 0.998 |
| 200 | 40 | 0.963 | 0.947 | -0.012 | 0.976 | -0.001 | 0.995 |
| 200 | 80 | 0.982 | 0.976 | -0.005 | 0.993 | -0.001 | 0.996 |
| 200 | 113 | 0.985 | 0.982 | -0.003 | 0.989 | 0.000 | 0.998 |

`V_donor` is about 2 to 4 percent low (the log transform at low counts and the TMM scale); its interval covers the truth in 86
percent of the genes at n = 12 and 95 percent or more from n = 20 with 113 cells. Reliability is slightly low and its interval
conservative; the share interval covers 99.5 percent because most simulated shares sit at the boundary 0 or 1, where the clipped
interval is trivially wide, so the share coverage says little. Planted truths in the suite: unbiased `V_donor` across n, the null
(no donor variance, the interval reaches 0), a planted technical axis absorbed by the technical block (share 0.8 to 0.94), a
planted facet share recovered by `share_design` and by the Shapley split (0.5 planted, 0.35 to 0.65 accepted), a latent axis
absorbed by the factors, gating and rung bookkeeping, `excess_z` finding planted variable genes, TMM neutrality and shift, the
weighted bootstrap equal to explicit resampling, deterministic quarters.

## Validation on real data (2026-10-09, `--seed 20261009`, 200 bootstrap replicates, 4 CPUs niced)

Units: G24/MMG (SEA-AD MTG microglia, 87 donors), M23/IMM (ROSMAP microglia, 369), L26/PSY (PsychAD, microglia 1,125 and CNS
macrophages 242) and B25/CNT (Borrie 2025 xenograft, microglia, 46 host animals). Output files are in
`_facet_tests/<group>/` (not in git; they are synced with the rest of `_facet_tests/`).

| unit | replicates | genes reported | median reliability | median donor SD (log2) | deepest rung | runtime | peak memory |
| --- | --- | --- | --- | --- | --- | --- | --- |
| G24/MMG microglia | 87 | 12,064 | 0.68 | 0.35 (fold range 1.6) | D4 (+F) | 59 s with 366 module rows | 1.0 GB |
| M23/IMM microglia | 369 | 6,237 | 0.49 | 0.35 | D4 (+F) | 89 s | 2.1 GB |
| L26/PSY CNS macrophage | 242 | 4,783 | 0.53 | 0.30 | D4 (+F) | 56 s of 308 s | 6.2 GB (whole run) |
| L26/PSY microglia | 1,125 | 7,687 | 0.58 | 0.33 | D1 (+F) | 244 s | 6.2 GB |
| B25/CNT xenograft | 46 | 11,459 | 0.77 | 0.23 | D2 (+F) | 29 s | 0.7 GB |

**Positive controls.** Sex genes: `share_design` at D1 (sex is a D1 term) is 0.97 to 1.00 for XIST, RPS4Y1, UTY, DDX3Y, KDM5D and
USP9Y in G24 (LMG share of sex 0.94 to 0.99), 0.95 to 0.96 in M23 (XIST, UTY, USP9Y), 0.92 to 0.97 in L26 microglia (the Shapley
split gives sex 0.83 to 0.86 there because sex is correlated with the technical block); before D1 (D0) their share is 0.03 to 0.11. Polymorphic genes
stay unexplained after the deepest rung (unexplained fraction, excess z): G24 HLA-DRB5 0.99 (3.6), HLA-DQB1 1.00 (3.2), HLA-DQA1
0.98 (2.6), ARL17B 1.00 (4.1), ARL17A 1.00 (2.3), LRRC37A 0.81 (0.7), LRRC37A2 0.62 (1.1), ERAP2 0.74 (0.9, reliability 0.75);
M23 HLA-DRB1 0.98 (3.8), HLA-DQB1 0.99 (3.5), ARL17B 0.96 (3.1), ERAP2 reliability 0.37 only; L26 microglia HLA-DRB5 0.87
(2.3), ARL17B 0.96 (2.9); B25 HLA-DRB5, HLA-DQA1, HLA-DQB1 and HLA-DRB1 are 93 to 97 percent explained by D1, i.e. by the batch (see the open questions). The strongest `excess_z` genes
of G24 and M23 (D4) include HLA, ARL17B, CD163, F13A1, SERPINE1, HSPA1A/B (stress), TMEM163, IFI44L, PDK4, RGS1. GSTM1 and GSTT1 are
below the expression gate in microglia and are not reported; LRRC37A, LRRC37A2 and ERAP2 are only moderately
reliable and moderately ranked, so "high unexplained" holds for the HLA and ARL17 genes but not clearly for
the LRRC37A family and ERAP2 in these cohorts.

**Negative control (ambient RNA).** 30 neuronal, oligodendrocyte and astrocyte marker genes that are NOT in the ambient
panel of the technical block (NRGN, CAMK2A, SYN1, CNP, MAG, CLDN11, SLC1A3, ...), compared with expression matched genes
(median over genes; unexplained SD in log2 units and median `excess_z`): G24 SD 0.62 raw, 0.28 after D0 (matched
genes 0.35 and 0.23), excess 0.89 raw and 0.08 after D0 (matched -0.10 and -0.08); L26 microglia 0.62 to 0.28, excess 0.82 to
-0.24; L26 CNS macrophages 0.56 to 0.33, excess 0.66 to 0.09; M23 only partly: SD 0.76 to 0.43, excess 1.55 to 0.55 (matched genes
-0.28); B25 (xenograft; 5 genes, human reads) does not collapse (excess 0.74 raw, 1.3 to 1.9 after D0 and D1, because the matched genes lose
most of their variance to batch at D1). So the technical block takes the ambient genes to the level of expression matched genes in
G24 and L26 and about half of the way in M23; the latent factors do not remove more of them (excess 0.1 to 1.0 after F).

**Technical block and shares.** The median `share_design` of all genes is 0.51 (D0), 0.52 (D4) and 0.69 (F) in G24, 0.34 and
0.66 in M23: about half of the donor variance of a typical microglia gene is explained by the technical block and composition
alone, sex, age, PMI, APOE and Braak add 1 percentage point or less at the median, and the latent factors add 17 (G24) to 32 (M23) points.
In B25 the batch (7 levels for 46 animals) explains 0.9 at the median and is confounded with the genotype (rung D2 adds no rank).

**Cross-cohort (rung D1).** The three human microglia anchors (SEA-AD 87 donors, ROSMAP 369, PsychAD 1,125) share 4,752 to 5,775
genes pairwise; the mean Spearman correlation of `excess_z` between cohorts is 0.81 (0.78 to 0.83). 6,374 genes are ranked in at
least two cohorts, 106 have a rank product r-value below 0.05 (35 below 0.01), 84 of those in all three cohorts. The top of the list is HSPA1A, TMEM163,
SERPINE1, PDK4, CD163, RGS1, CCDC26, ARL17B, F13A1, IFI44L, SMIM35, ZBTB16: heat shock and stress genes (HSPA1A, HSPA1B), DAM and
perivascular macrophage markers (CD163, F13A1) and interferon genes are state and handling axes of a donor, ARL17B is the 17q21.31
inversion signal; HLA-DQB1 r 0.007, HLA-DRB1 0.03, HLA-DRB5 0.04 (two cohorts), ARL17A 0.16, XIST 0.29, LRRC37A2 0.52, UTY 0.71, ERAP2 1.0.
(With the partial conjunction proposed in the review every r-value was 0.9998 on these data, which is why it was dropped.) The
first modules of the descriptive top list are `Sierksma2025/WGCNA` Tan cytokine activation, Greenyellow migration and Magenta
antigen presentation (donor SD about 0.45 to 0.49 log2 units, reliability 0.91 to 0.99).

**Modules.** The primary `pseudobulk` variant gives median reliability 0.91 to 0.97 (donor SD 0.14 to 0.21), the secondary
`cell_mean` variant 0.89 to 0.99 (SD 0.01 to 0.04, UCell bounded in [0, 1], different units). Agreement of A with B over the
modules of a unit (Spearman of the reliabilities): 0.46 to 0.87 (G24 0.60 geneset / 0.46 ucell, M23 0.72 / 0.74, L26 microglia 0.81 /
0.70, B25 0.87 / 0.84).

**Runtime model** (all strata, 6 cores pinned and niced, load average about 20 on the host): about 25 s per dataset plus 15 s per
stratum plus 0.18 s per replicate; peak memory grows with replicates times genes (6.2 GB at 1,125 replicates and 7,700 genes,
dominated by the bootstrap matrices and the Shapley subsets; under 1 GB below 100 replicates). From the facet manifests 84 datasets, 271 strata and 18,467
(stratum, replicate) pairs reach 12 replicates: projected fleet run 2.6 hours (plan 3 to 4 hours with the large multi-region sets;
Castonguay2026 has 16 strata and 1.0 million cells), run as datasets in sequence under `nice -n 19 ionice -c3 taskset`. The
files are 1.5 to 3 MB per stratum (float32, zstd): about 0.6 GB for the fleet.

## Open questions

* In xenograft datasets the batch (injection group, graft line, sequencing pool) can be the identity of the grafted iPSC line.
  Then D1 removes exactly the genetic donor variance one wants to see (in B25 the HLA genes are 93 to 97 percent explained by
  the batch). The xenograft `raw` and `D0` rungs are the ones to read for line-to-line differences; whether batch should
  stay in D1 for xenografts is a decision for the `.md` (`facet_batch_column`) of each dataset.

* Which rung do the web panel and the top list show by default? D1 (technical block, sex, age, PMI, batch) is the deepest rung
  almost every human cohort reaches; D4 is reached by few.
* Should composition PCs stay in D0? They remove the donor's fraction of cells in a sub-state, which is the largest donor level
  axis in microglia but is also biology; a D0b rung without them would answer "donor state" questions.
* Cohort anchors reuse the facet test cohort table and its overlap flags; no donor overlap correction is applied to the rank
  product (as for the facet tests).
