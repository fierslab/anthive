# Design: donor-level facet testing (v1)

Status: v1 implemented in `bin/annot/ant-facet-test` (+ `bin/annot/facet_test_limma.R`);
module-level tests (v1.1) in `bin/annot/ant-facet-test-modules`.
All output is exploratory. Date 2026-10-04.

## Why

The existing variability views (`ant-module-variability`, `/facets` signal
strength) compute per-cell statistics. A donor-level facet (sex, disease,
APOE, Braak) is constant within a donor, so those statistics mostly measure
donor identity and the effective sample size is the number of donors, not
cells (facet audit, section 9). v1 replaces that question with a donor-level
test inside each cell type.

## Scope

A dataset is tested when all of these hold (checked from `.md`, `.h5ad.mus`
and the served files, before any expression data is read):

* not `is_secondary: true` (primaries only; reanalyses are skipped),
* `.h5ad.mus` organism is exactly `human` (datasets with a missing organism
  field are skipped, except the five Wachter2024 regional files that are
  listed in `ASSUME_HUMAN_DB_IDS` because their group describes 32 human
  donors),
* `experimental_system` is not xenograft, chimera, in_vitro_2d, organoid or
  bulk_tissue, and the current mapping publishes no `ant_host_sex` facet,
* a served raw-counts layer exists (`counts`, `UMIs`, `raw`, `raw_counts`
  by_gene sidecar) whose recorded `min_nonzero` is 1 (a normalised layer
  that happens to be named `counts`, e.g. Aldinger2021, is rejected),
* at least 6 donors with a non-NULL `ant_donor` in the current mapping.

Bulk datasets are excluded because a (donor, cell type) pseudobulk with a 10
cell minimum is undefined for one sample per donor.

## Facets and contrasts

| Facet | Column | Contrast (reference first) | Notes |
| --- | --- | --- | --- |
| sex | `ant_sex` | male vs female (log fold change = male minus female) | other values dropped |
| disease | `ant_disease` | AD vs control (control is the reference) | everything else (MCI, NCI, PD, AD_DLBD, ...) dropped. **Provisional**: vocabulary decisions L17 to L21 are open; the output column `provisional` is true |
| apoe_carrier | `ant_apoe_carrier` | e4 carrier vs non-carrier | derived from `ant_apoe` (any e4 allele) when the carrier column is absent |
| braak | `ant_braak` | linear trend, log2 fold change per Braak unit | donor value must be a single number |

A donor whose cells carry more than one distinct value for a facet gets NULL
for it and is counted in the manifest (`donor_conflicts`). It is never
resolved silently.

## Method

1. **Current mapping, not stored views.** The `.duckdb` is attached read-only
   in an in-memory connection and TEMP `obscat` / `obsnum` views are rebuilt
   from the dataset's current `.md` `api:` section with
   `anthive_db.build_obs_view_sql`. Mapping fixes committed in the store but
   not yet applied to the stored views are therefore honoured.
2. **Stratifier.** `ant_cell_type` when it varies per cell. When it is a
   constant (a dataset descriptor), two stratifier sets are run and labelled
   in `celltype_source`: the whole dataset under the constant label
   (`constant_all_cells`) and, if the dataset has a per-cell cell type column
   (`ant_author_cell_type`, then an author column named like cell_type /
   class / annotation with 2 to 40 levels), one stratum per level. Both are
   kept because the constant can be wrong for a mixed file (audit L32) and
   per-cell author labels can be expression-defined states. When
   `ant_cell_type` is absent and no cell type column exists, `ant_author_cluster`
   (2 to 40 levels) is used as a last resort (`celltype_source` =
   `ant_author_cluster`; expression-defined groups); with nothing at all, all
   cells are pooled in one stratum (`celltype_source` = `none`; not cell type
   specific). Every test is within one (dataset, cell type) unit.
3. **Pseudobulk.** Raw counts are summed per (donor, cell type) in DuckDB
   from the counts `by_gene` parquet. One sample per donor per cell type;
   donors with several samples or libraries are collapsed by summation.
   (donor, cell type) groups with fewer than 10 cells are dropped (counted in
   the manifest as `n_dropped_lt_min_cells`).
4. **Model** (R, `limma` + `edgeR`, `facet_test_limma.R`): per
   (dataset, cell type, facet), `edgeR::filterByExpr` with the design, TMM
   (`calcNormFactors`), `limma::voom`, `lmFit`, `eBayes` (defaults). Design:
   `~ facet + sex` (sex omitted when the facet is sex), plus age and PMI
   (z-scored) only when the unit has at least 15 donors and the covariate is
   known for at least 90 percent of them (donors without it are then dropped;
   otherwise the covariate is omitted and listed in the manifest), plus batch
   only when a donor-level batch column exists and is not confounded.
   Reported per gene: log2 fold change, posterior standard error, 95 percent
   confidence interval (t quantile on the moderated degrees of freedom),
   moderated t, p value, Benjamini-Hochberg q within the
   (dataset, cell type, facet) triple, average log2 CPM.
5. **Why this method.** The donor is the unit of replication; limma-voom on
   pseudobulk is the standard, well-calibrated choice for small donor counts
   and handles covariates. A simpler per-gene Wilcoxon test would not allow
   covariate adjustment; a cell-level mixed model is far more expensive and
   offers no gain once cells are collapsed within donor.

## Thresholds and flags

| Rule | Value |
| --- | --- |
| Minimum cells per donor x cell type | 10 |
| Minimum donors per dataset | 6 |
| Facet level with fewer donors | dropped; fewer than 2 levels left means not tested |
| `n_lt5` | any level with fewer than 5 donors. Numeric (Braak): fewer than 10 donors (two halves of at least 5; my choice). p, q, t and the confidence interval are set to NULL, the log fold change and SE are kept as descriptive output; `descriptive_only` is true |
| `batch_confounded` | the facet lies in the span of the batch indicator columns (facet is a function of batch); batch is then left out of the model |
| `batch_used` | batch entered the model (donor-level, at least 2 levels, at most donors / 4 levels, not confounded) |
| `composition_p`, `composition_flag` | donor-level fraction of the donor's cells that fall in this stratum, tested against the facet (Mann-Whitney U for two levels, Spearman for Braak). Flag when p < 0.05, uncorrected, exploratory |
| `multi_tissue_donors` | number of donors with cells in more than one `ant_tissue` (collapsing across regions mixes them) |

Batch is detected by name (a visible, non-`ant_` column matching
batch, pool, library, lane, plate, chip, chemistry, sequencing_run, run_id)
that is constant within donor. There is no canonical batch facet, so this is a
heuristic; the manifest records the column used (`batch_column`).

A per-donor composition table (`<file>.facet_composition.parquet`) is written
for every dataset: proportion of each donor's cells per cell type stratum and,
when `ant_author_cluster` exists, per cluster, with the donor's facet values.

## Outputs

Under `<h5adstore>/_facet_tests/<group>/` (underscore folder: not served, not
in git, never read by the catalogue scan):

* `<file>.facet_tests.parquet`: one row per (cell type, facet, gene)
* `<file>.facet_composition.parquet`
* `<file>.facet_run_manifest.json`: dataset sha256 from the `.md`, git commits
  of anthive4 and h5adstore, R / limma / edgeR versions, all parameters, seed,
  timestamp, per-unit status, donors per level, flags and counts.

Nothing is written elsewhere in `data/`; `.h5ad`, `.duckdb` and `.md` files are
only read. R lives in `envs/facet-test-r` (limma 3.62.1, edgeR 4.4.0, R 4.4.3);
the `anthive-data` environment has neither package and was not modified.

## Limitations

* **Composition.** Pseudobulk within a cell type does not remove composition
  effects inside the type (states, subtypes). `composition_flag` is a crude
  warning, not a correction. Per-cell author labels used as strata are
  expression-defined, so a facet effect inside them is conditional on a
  clustering that may itself depend on the facet.
* **Power.** Typical units have 8 to 90 donors. Moderated tests with 5 donors
  per level detect only large effects; absence of a hit is not evidence of no
  effect. `n_lt5` units are descriptive only.
* **Donor overlap.** Datasets are tested independently. The same persons occur
  in several datasets (ROSMAP across M23, GF4, Z20, MY4; SEA-AD across the 14
  Gabitto2024 files and F26; MADRC across SP4 and WA4; Lu across LC5 files).
  Counting a gene as "recurring" across datasets overstates independent
  replication; cross-dataset summaries need the cohort selection table
  (audit section 8.3). No meta-analysis is done in v1.
* **Provisional disease vocabulary.** Only the literal values `AD` and
  `control` of the current `ant_disease` mapping are used. `control` means
  different things across studies (neuropathology-defined, clinical NCI,
  psychiatric controls); AD with Lewy body or vascular co-pathology is
  dropped. Results change when L17 to L21 are decided.
* **Donor identity.** `ant_donor` collisions or identity defects (audit L7)
  cannot be detected here; one dataset's labels are assumed to be one person
  each.
* **Covariates.** Age and PMI are used as stored (units differ between
  datasets; they are z-scored per unit). Age top-coding (90+) is ignored.
* **Normalisation caveat for nuclei vs cells and ambient RNA** is not modelled.
* **Eligibility.** Datasets without a raw-counts layer cannot be tested; see
  the run report for the list.
* Module-level tests are a separate tool, see "Module-level tests (v1.1)" below.

## Module-level tests (v1.1): `bin/annot/ant-facet-test-modules`

Exploratory, same donors, strata, facets, contrasts, covariate rules and
`n_lt5` rule as the gene tests (the sibling tool imports `plan_job`,
`build_donor_table`, `choose_strata`, `open_dataset` and `composition_test`
from `ant-facet-test`, so the two cannot drift apart on design rules). The
unit of replication is again the donor. Eligibility is the gene-test scope
without the counts requirement (a scores sidecar is required instead); datasets
without a served raw-counts layer run variant A only and carry `no_counts` true.

### Variants (column `method`)

| Variant | `method` | Per-donor module score | Needs |
| --- | --- | --- | --- |
| A | `cell_mean` | mean over the donor's cells in the stratum of the per-cell score in `<stem>.scores.parquet` (source `geneset` = scanpy `score_genes` on the log-normalised default layer; source `ucell` = UCell rank scores. Both are tested by default and each is its own BH family and its own `source` value in the output. The first run with `geneset` only left out the datasets that carry only UCell scores, e.g. L26 and the WA4 regional files) | sidecar only |
| B | `pseudobulk` (`source` = `gene_list`) | per (donor, stratum) summed raw counts, log2(CPM + 1) with library size = all-gene count sum; score = mean over the module genes minus mean over an expression-matched control set | counts layer and a `.grp` gene list in the genesetstore |

The sidecar is keyed `(cell_id, source, catalog, module, score)`; `cell_id`
joins `__cells`, `catalog` is `<Study>/<Experiment>` of the genesetstore and
`module` the geneset name, so the gene list of a module is
`<catalog>/<module>.grp`. Catalogs present in the 118 sidecars on disk
(modules per catalog): GWAS0000/GWAS (15 modules), Habib2020/DAA_states_human (3),
Liddelow2017/Reactive_states_human (3), LuChen2025/cen_wgcna (20), nbb_cellstates
(50), nbb_celltypes (6), nbb_wgcna (16), Mancuso2024/Cell_states (8),
Sadick2022/Astro_subtypes (9), Sierksma2025/WGCNA (13); a UCell copy of 9 of them exists.

Variant B follows scanpy `score_genes` (as `ant-score` does): genes are cut into
25 bins of equal rank size by their mean log2 CPM over the stratum's samples;
for every module gene 50 genes are drawn without replacement from its bin
(module genes excluded); the control set is the union. The control set is drawn
once per (stratum, module) with `default_rng([20261004, crc32("stratum|source|catalog|module")])`
so it does not depend on processing order or on the facet. Genes are matched to the
dataset by exact symbol (then stored gene name); modules with fewer than 3 genes
found are not scored in B (same rule as `ant-score`).

### Model and tests

Per (dataset, stratum, facet, method), one OLS per module on the donor-level scores:
`score ~ facet [+ sex] [+ age] [+ pmi] [+ batch]` (design from `plan_job`, so
age and PMI need at least 15 donors and 90 percent coverage, sex is dropped for
the sex facet, batch only when donor-level and not confounded). Reported:
estimate (score units; per Braak unit for braak; male minus female; AD minus
control; carrier minus non-carrier), SE, t, df = donors minus parameters, 95 percent
CI from the t quantile, `std_effect` (estimate divided by the SD of the donor-level
score, comparable across modules and methods), parametric p.

Permutation p: 1,000 shuffles of the facet labels across donors with covariates
held fixed; test statistic is the t of the facet coefficient in the full model;
two-sided, p = (1 + #{|t_perm| >= |t_obs|}) / (1 + B). The shuffles are shared
by all modules of a unit and by both methods (so A and B are compared under the
same null); they are generated by `default_rng([20261004, crc32("db_id|stratum|facet")])`.
Shuffles whose permuted facet is collinear with the covariates are discarded
(`n_perm_effective`). Plain label shuffling with fixed covariates is what was
approved; it is exact only when the facet is exchangeable given the covariates,
and the studentised statistic makes it close to correct otherwise. A
residual-permutation scheme (Freedman-Lane) would be the more conservative
choice if facet and covariates are strongly associated.

Multiple testing: Benjamini-Hochberg across the modules of one (dataset, stratum,
facet, method), applied separately to the parametric p (`q_param`) and the
permutation p (`q_perm`). Facets and strata are not corrected against each other
(exploratory). **Resolution limit:** with B = 1,000 the smallest permutation p is
1/1001, so a q_perm below 0.05 needs at least m / 50 modules at that floor (m =
modules in the family, about 128 to 235 here, i.e. at least 3 to 5 modules at the floor).
A single very strong module cannot pass `q_perm`; read `q_param` and `p_perm`
together. `--permutations` raises B (one flag, small cost).
Units with `n_lt5` are descriptive only (estimates and SE kept, t / p / q / CI NULL).

Calibration check: per unit, Pearson and Spearman correlation of the A and B
effect estimates (and of `std_effect` and t) across the modules scored by both,
plus the fraction with the same sign, are written to the manifest
(`units[].calibration`, one record per A source, paired by (catalog, module)). A and B are not expected to agree in scale (scanpy score
units versus log2 CPM units). Disagreement is expected where per-cell scores are
driven by dropout structure that pseudobulk averages away, and where the
background-matched control set differs (scanpy draws controls over the whole
dataset, variant B over the stratum's pseudobulk).

Negative control on real data: `run --null-control-seed N --out-root DIR` shuffles
every facet column across donors (age, PMI, batch stay) before planning and testing;
every hit is then a false positive. It refuses to run without an explicit `--out-root`.

### Outputs

`<h5adstore>/_facet_tests/<group>/<file>.facet_module_tests.parquet`: one row per
(stratum, facet, method, source, catalog, module) with db_id, dataset, cell_type,
celltype_source, contrast, level_ref / level_test, n_donors(_ref/_test), estimate, se,
ci_low, ci_high, t, df, p_param, q_param, p_perm, q_perm, std_effect, score_sd,
n_perm_effective, n_genes_requested, n_genes_found, n_control_genes (B),
min_score_coverage (A), n_lt5, descriptive_only, batch flags, covariates,
composition_p / composition_flag, provisional, no_counts, multi_tissue_donors.
`<file>.facet_module_run_manifest.json` records the dataset sha256, git commits
(anthive4, h5adstore, genesetstore), package versions, seed and all parameters,
per-unit status, counts of modules, hits at q < 0.05, calibration, catalog counts.
`ant-facet-test-modules summarize --out-dir DIR` writes the cross-dataset tables
(units, per-facet hit counts, recurring modules, calibration). A recurring module
is counted only over datasets that the DRAFT cohort selection table marks as
independent (anchor / primary roles), with the same donor-overlap caveat as the
gene tests: ROSMAP, SEA-AD, MADRC and Lu files share persons.

### Limitations specific to modules

* The per-cell scores were computed once per dataset on all cells; the donor mean
  within a stratum therefore inherits dataset-wide control sets (A) and is
  sensitive to cell-type composition within the stratum.
* Modules in one catalog are correlated (WGCNA modules, cNMF states, GWAS
  lists overlap); BH assumes positive dependence and is applied per family
  without accounting for catalog structure. Modules of different catalogs share
  genes, so hits are not independent findings.
* Control sets contain co-regulated genes in real data; a strong module can
  push the scores of unrelated modules whose controls overlap it (seen in the
  synthetic test when null modules shared bins with the planted genes).
* Strata from per-cell author labels are expression defined, as for the gene tests.
* `ucell` scores are rank based in [0, 1]; effect sizes of the two A sources are not
  comparable in scale (use `std_effect`).

## Reproducibility

Seed 20261004 is passed to R (`set.seed`; limma-voom itself is
deterministic). Parameters are constants at the top of `ant-facet-test` and
are copied into every manifest.

## Tests

`tests/test_facet_test_modules.py`: BH, OLS against an independent lstsq /
linregress computation, permutation p uniform under the null (Gaussian,
covariates, heavy-tailed noise), powered under a planted effect, seed
reproducibility, control genes bin matched with module genes excluded, and an
end-to-end synthetic dataset (planted module effect recovered by both
variants, null modules not rejected, calibration record, no-counts dataset,
n_lt5, exclusion rules).

`tests/test_facet_test.py` (synthetic dataset): planted sex effect recovered,
`n_lt5` suppression, exclusion of xenograft / in vitro / bulk / mouse /
secondary / host-sex datasets, collapse of multi-sample donors, level and
batch rules, use of the current `.md` mapping. The end-to-end tests need
limma + edgeR (`ANT_FACET_RSCRIPT`), otherwise they are skipped.
