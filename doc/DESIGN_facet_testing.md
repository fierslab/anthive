# Design: donor-level facet testing (v2, 2026-10-05)

Status: v1 implemented in `bin/annot/ant-facet-test` (+ `bin/annot/facet_test_limma.R`);
module-level tests (v1.1) in `bin/annot/ant-facet-test-modules`.
All output is exploratory. Date 2026-10-04.

## v2 (2026-10-05): what changed

Approved after the two reviews of the replication layer (vote counting on per-unit FDR calls,
any-unit-per-cohort, 1,100 donors voting on 3 percent changes, ROSMAP counted through a
substate, Lee2026 and ROSMAP sharing persons, "AD vs control" meaning different things).

1. **Replication = partial conjunction** (Benjamini and Heller 2008) on limma **treat** p values
   (H0 |log2FC| <= tau), **one anchor unit per cohort and cell type group**, r-values BH-adjusted
   over the genes of a facet x group. `k` (cohorts with q_treat < 0.05) is only descriptive. See
   "Replication (v2)".
2. **Donor-pooled units** for multi-region cohorts (MADRC, SEA-AD MTG + PFC) and a derived
   **group-pool stratum** per cell type group (ROSMAP M23/IMM Mic subclusters pooled into
   whole microglia).
3. **Replicate unit** (`replicate_unit`, `replicate_column`): the pseudobulk unit is the
   dataset's replicate (donor, animal, sample), and a unit without enough replicates is
   "cell-level, descriptive only".
4. **Common disease definition** (`disease_basis`): Braak x CERAD 2x2 for datasets with both
   scores; cohorts are only combined for disease when their definition is pathology based.
5. Effect-size gate for modules (`|std_effect| >= 0.2`), SEA-AD microglia without the PVM,
   lymphocyte and monocyte supertypes, independence flag for cohorts never checked.

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

* not `is_secondary: true` (primaries only; reanalyses are skipped; the
  result views also list any secondary unit as not counted),
* `.h5ad.mus` organism is exactly `human` (datasets with a missing organism
  field are skipped, except the five Wachter2024 regional files that are
  listed in `ASSUME_HUMAN_DB_IDS` because their group describes 32 human
  donors),
* `experimental_system` is not xenograft, chimera, in_vitro_2d, organoid or
  bulk_tissue, and the current mapping publishes no `ant_host_sex` facet,
* a served raw-counts layer exists (`counts`, `UMIs`, `raw`, `raw_counts`
  by_gene sidecar) whose recorded `min_nonzero` is 1 (a normalised layer
  that happens to be named `counts`, e.g. Aldinger2021, is rejected),
* at least 6 donors with a non-NULL `ant_donor` in the current mapping (v2: at least 6
  replicates in the dataset's `replicate_column`, see "Replicate unit and analysis level"),
* a `replicate_unit` other than `none`.

Bulk datasets are excluded because a (donor, cell type) pseudobulk with a 10
cell minimum is undefined for one sample per donor.

## Facets and contrasts

| Facet | Column | Contrast (reference first) | Notes |
| --- | --- | --- | --- |
| sex | `ant_sex` | male vs female (log fold change = male minus female) | other values dropped |
| disease | `ant_disease` | AD vs control (control is the reference) | everything else (MCI, NCI, PD, AD_DLBD, ...) dropped. **Provisional**: vocabulary decisions L17 to L21 are open; the output column `provisional` is true |
| apoe4_carrier | `ant_apoe4_carrier` | e4 carrier vs non-carrier | derived from `ant_apoe` (any e4 allele) when the carrier column is absent |
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
   cell_type_high_resolution / class / annotation with 2 to 40 levels), one stratum per level. Both are
   kept because the constant can be wrong for a mixed file (audit L32) and
   per-cell author labels can be expression-defined states. When
   `ant_cell_type` is absent the same author column rule applies (this is how M23/IMM, which
   has only `cell_type_high_resolution`, is stratified; before 2026-10-05 the pattern missed
   that name and the dataset fell back to one pooled stratum). When no cell type column exists, `ant_author_cluster`
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

## Replicate unit and analysis level

A dataset's `.md` declares what an independent replicate is: `replicate_unit` (`donor`, `animal`,
`sample`, `none`) and `replicate_column` (the obs column identifying it; `ant_donor` for the
human cohorts, `hash.ID` / `Mouse_ID` / `mouse.id` for xenografts). Without the keys it is
inferred (`lib/replicates.py`: human with `ant_donor` is a donor; mouse or xenograft with an animal
id column is an animal; in a xenograft `ant_donor` is the human donor line and never the animal;
otherwise `none`). `bin/meta/ant-set-replicate` populated all 168 datasets (99 donor, 24
animal, 21 sample, 24 none; the `none` ones and the reasons are in `doc/DESIGN_canonical_columns.md`).
`ant-md-check` warns when the key is missing and errors when `replicate_column` is not an obs column.

The shared rule (`replicates.analysis_level`), per dataset x stratum x facet: a unit is analysed at
the **replicate level** (pseudobulk by `replicate_column`, the tool's former donor) when
`replicate_unit` is not `none` and there are at least 3 replicates in every level of a two-group
facet, or at least 6 replicates spanning at least two distinct values of a numeric facet (Braak).
Otherwise the unit is **cell-level, descriptive only**: no test, with the reason. The existing
`n_lt5` rule (fewer than 5 per level: no p, q, CI) stays on top of it. Both tools record
`replicate_unit`, `replicate_column`, `replicate_basis` in the run manifest, and
`analysis_level`, `analysis_level_label`, `analysis_level_reason` on every unit; the API serves
them per unit (`/databases/{id}/facet-tests`). Facet tests of xenograft or mouse datasets are not run
today because the four facets (sex, disease, APOE, Braak) do not exist there; the plumbing is in
place for treatment or genotype facets.

The per-cell tools (`ant-module-variability`, `ant-gene-variance-precompute`) write a
`replicate_unit` column into their aggregate; the API marks their numbers
`per-cell (descriptive)` (`basis`), whether a replicate-level analysis exists or is impossible.

## Effect-size gate and error control (limma treat)

`facet_test_limma.R` also runs `limma::treat` on the same linear fit (H0: |log2FC| <= tau) and
writes `p_treat` and `q_treat` (BH within the job) next to the point-null `p_value` / `q_value`.
tau is log2(1.1) for two-group contrasts (a 10 percent change; the lower end of Smyth's range,
McCarthy and Smyth 2009) and log2(1.1) / 3 per stage for Braak (10 percent over three stages;
a per-stage slope has to be scaled, otherwise the gate is 3 times too strict); both are command
line options (`--tau-two-group`, `--tau-braak-per-stage`, log2 units) and are recorded as
`tau_log2` on every row and in the manifest. `p_treat` is the one-sided p in the direction of
the observed sign (the opposite tail of treat's p is negligible), so a gene whose effect is
below tau has p_treat above 0.5. For modules a hit also needs `|std_effect| >= 0.2`
(column `passes_effect_gate`; `ant-facet-test-modules summarize --min-std-effect`).

## Donor-pooled units and group-pool strata

**Pooled units.** A cohort that is delivered as one file per region (MADRC: WA4 myeloid and
SP4 astrocytes, five regions from about 30 donors; SEA-AD: one MTG and one PFC file per cell
class) would contribute five correlated "cohorts" or a region-matched anchor of arbitrary
choice. `ant-facet-test run --pool ID` (or `run --all`, pools after datasets) pools a donor's
cells over the member files into **one pseudobulk per (donor, cell type stratum)**: the counts
of each member are summed in DuckDB, gene ids are mapped to symbols (counts of ids sharing a
symbol are added) and summed across files; the donor key is `ant_donor_uid` where it exists.
Region is not a covariate (ignored, recorded in the manifest: `pool.region_handling`,
`multi_tissue_donors`); the batch column is not used for pools. A pool is a row of the cohort
table whose `db_id` is `pool:<id>` and whose `pool_members` lists the member `db_id`s; output goes
to `<results>/_pooled/<id>.facet_*` with `db_id` `pool:<id>`. Members keep their own runs as
supplementary units. Pools defined on 2026-10-05: `madrc_myeloid`, `madrc_astro`, and seven
SEA-AD classes (`seaad_microglia`, `_astro`, `_endothelial`, `_inhibitory`, `_opc`, `_oligo`, `_vlmc`).
The module tool has no pooled units; its recurring-module summary still uses the member files.

**Group-pool strata.** When a stratifier source has two or more labels in one cell type group
(ROSMAP M23/IMM: Mic P2RY12, Mic TPT1, Mic MKI67, Mic DUSP1 are all microglia; Schirmer2019
EN-L2-3-A, EN-L4, ...), `choose_strata` adds a derived stratum with the group name as label and
source `group_pool` containing all those cells. Priority for the anchor: dataset constant, then
`group_pool`, then per-cell `ant_cell_type`, author cell type columns, author clusters. This
replaces the Bonferroni-over-substrata rule for such datasets (a whole-microglia unit exists, so a
substate with a lucky q cannot speak for the cohort).

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

### Dataset definition notes (`facet_notes`)

A dataset `.md` can state how one of its facets was defined, as a front-matter
mapping `facet_notes: {disease: "..."}` (names with or without `ant_`). Both
tools copy it into the run manifest as `facet_notes`, and `lib/facet_results.py`
appends it to the "Facet term" line of the model description as "Dataset
definition: ...". Used for the ROSMAP datasets, whose `ant_disease` is a
pathologic AD proxy (Braak III or higher and CERAD definite or probable) and
not a clinical diagnosis. Manifests written before the key existed carry no
note until the dataset is re-run.

### Donor exclusions (`facet_donor_exclusions`, shared persons)

A dataset that contains persons of another counted cohort must not be counted
as an independent cohort on those persons. The `.md` front matter declares the
rule (machine readable, `lib/donor_exclusions.py`):

```yaml
facet_donor_exclusions:
  reason: Rush ROSMAP brain bank donors, counted through the ROSMAP cohort
  obs:                    # donors with at least one cell in these values
    Source: [R]
  cohorts: [rosmap]       # donors whose ant_donor_uid starts with rosmap:
```

Both tools drop those donors' cells right after the per-cell table is read,
before donors, strata, composition proportions or the limma fit exist, so every
number of the counted run refers to the remaining donors. The manifest block
`donor_exclusion` records the variant, the rule, donor and cell counts before
and after, and the number of donors each criterion excludes (counts only, no
identifiers). A malformed rule or a missing obs column is an error, never a
silent skip.

* The counted run is the default (`run --db-id L26/PSY`) and writes the normal
  files, so the API serves it.
* `run --all-donors` ignores the rule and tests every donor. It writes the same
  file names under `<group>/supplementary_all_donors/`, which no glob of the
  summaries and no API path reads, and its manifest has `donor_exclusion.counted`
  false: a supplementary, non-counted unit. Datasets without a rule are skipped.
* `--exclude-donors-from COHORT` and `--exclude-donors-where COLUMN=V1|V2` add
  ad hoc exclusions on top of the rule; they need an explicit `--out-root`.
* The cohort table has an optional column `donor_exclusion` (free text). A row
  with a value there counts only when the dataset's gene run manifest records
  `donor_exclusion.counted` true; otherwise the unit is listed with
  `not_counted_reason` "...does not record the donor exclusion". So a stale run
  from before the rule cannot be counted by accident.

Used for Lee2026 (2026-10-05): PsychAD brain bank `R` (152 of 1,470 donors) and
FreshMG `RS` (41 of 137) are Rush ROSMAP participants (Methods of Lee 2026);
ROSMAP is already counted through M23/IMM. The ids of both datasets are
anonymised (`Donor_n`, `FM_nnn`), so the overlap was established three ways:

1. Cohort label: the `Source` counts (152, 41) equal the ROSMAP counts in the
   Methods; no donor of another bank matches a ROSMAP person.
2. Attribute matching without ids (PsychAD): Table S4 ages are decimal years on
   the same day grid as `ROSMAP_clinical.csv` (`age_death`). Sex, age at death
   (1e-6 years) and PMI (hours, 1e-6) identify 30 donors one to one; Braak, CERAD
   (PsychAD scale 1 to 4 is 5 minus ROSMAP `ceradsc`) and dementia
   (cogdx 1 to 0, 2 and 3 to 0.5, 4 to 6 to 1) agree in 30 of 30 pairs (chance 0.04
   per pair for all three). The same key at +-1..120 day lags gives 0.017 matches
   per lag; age-only exact matches exceed the 30 by 7, as many as chance predicts
   (5.9 +- 2.2 per lag). `donor_crosswalk.tsv` carries these as alias type
   `psychad_id`. 8 of the 30 are persons in the ROSMAP datasets served here
   (6 in M23/IMM, 3 in GF4/MIC). Donors with top-coded age (89+, 54 of the 152)
   and FreshMG (integer ages, PMI in minutes) cannot be resolved one by one.
3. Expression: donor pseudobulk of microglia correlated across a 300-gene panel
   chosen on M23 vs GF4 same-person pairs (held-out AUC 0.98); 2 of the 4
   identified donors present in M23 rank first of 381 (p about 4e-5 for two
   first ranks), the other two do not (ranks 170, 206). Underpowered, consistent.

All 152 and 41 are excluded, not only the 30 identified donors, because the other
donors are ROSMAP participants whose ids cannot be recovered locally. The
alternative (excluding only the 8 persons found in the ROSMAP datasets served here)
was not run.

### Age floor and declared batch column (`facet_min_age`, `facet_batch_column`)

Two more `.md` keys, both read by both tools (`lib/donor_exclusions.py`):

```yaml
facet_min_age:             # per facet; donors younger than this (years, ant_age) leave out that facet only
  disease: 60
  braak: 60
facet_batch_column: Source # obs column used as the batch term instead of the name based guess
```

Donors under a floor stay in the other facets (sex keeps every donor); donors with
an unknown age are kept and counted in the manifest block `facet_min_age`
(`n_donors_below_floor`, `n_age_unknown_kept`). The declared batch column must be
an obs column (an unknown name is an error); a donor with more than one value has no
batch, and `plan_job` still drops the term when fewer than two levels remain among the
analysed donors or when it is confounded with the facet (`batch_used`,
`batch_confounded` in the unit record). Used for Lee2026: disease and Braak units
leave out donors under 60 years, brain bank (`Source`) is the batch term.

Nothing is written elsewhere in `data/`; `.h5ad`, `.duckdb` and `.md` files are
only read. R lives in `envs/facet-test-r` (limma 3.62.1, edgeR 4.4.0, R 4.4.3);
the `anthive-data` environment has neither package and was not modified.

## Reproducibility and determinism

Both tools are bitwise reproducible for a given input (store state, mapping,
tool commit, seed), independent of the DuckDB thread count and of machine load
(`tests/test_facet_determinism.py`).

* Gene tool: the pseudobulk is an integer sum of raw counts (exact, so the
  order of addition cannot matter), the SQL result is sorted before it reaches
  R, R runs with a fixed seed and fixed BLAS thread settings.
* Module tool: variant A needs the donor mean of per-cell double scores. A
  parallel DuckDB `AVG` adds the values in a scheduling dependent order, so the
  mean differed in the last bits between runs (2026-10-05, seen as 1e-17 to 1e-14
  differences in estimates, t, p_param and q_param between two reruns of unchanged
  datasets). The mean is now an ordered aggregate (`AVG(score ORDER BY cell_id)`),
  summed in a fixed order. Permutation shuffles come from seeded generators and
  were never affected. Variant B uses exact integer count sums.
* Summaries from runs before the fix differ from a rerun by at most ~1e-12 in
  the parametric statistics; counts of hits did not change in the module tests.
* `run --all` without `--force` skips a dataset only when its manifest records
  the sha256 of the current `.md` file (`md_sha256`). `dataset_sha256` is the h5ad
  content hash and does not change when the mapping changes, so before this
  check a mapping fix left the old output in place. Changes outside the `.md`
  (`.h5ad.mus`, scores sidecar, genesetstore) are not tracked: use `--force`.
  2026-10-05c to 2026-10-05d: the hit count changes were all D26/ALL, whose 05c
  output predates the ant_age top-coding fix (it reproduces exactly from the
  pre-fix mapping).

## Common disease definition

`disease_basis: braak_cerad` (`lib/disease_rule.py`, `bin/annot/ant-derive-disease`): AD = Braak III
or higher and CERAD probable or definite; control = Braak II or lower and CERAD none or sparse;
everything else NULL (PART-like donors with tangles but no plaques, amyloid-only donors, missing
values, SEA-AD reference donors). This is a binary pathologic-AD facet, not ADNC (Thal is missing
for ROSMAP) and not a clinical diagnosis. Applied to SEA-AD (14 files), ROSMAP (M23 x3, GF4, MY4,
Z20; RADC `ceradsc` 1 definite, 2 probable, 3 possible = sparse, 4 none; the native `niareagansc` is
not in this distribution, so the 2x2 rule is used), MADRC WA4 (6 files), Prater (P23) and, by the
Lee2026 job, PsychAD / FreshMG (authors' pure AD or control AND the same Braak and CERAD limits).
The stored column is `disease_braak_cerad`; the `.md` records the rule as `disease_derivation`.
All other datasets that publish `ant_disease` carry `disease_basis: author_dx`.

## SEA-AD microglia stratum

G24/MMG and G24/PMG derive `ant_cell_type` from the `Supertype` column: microglia = Micro-PVM_2,
_2_1, _2_3, _3 and _4 (SEAAD); Micro-PVM_1 (putative perivascular macrophages, about 4 percent of
the nuclei; inferred from the supertype size and the Allen MTG taxonomy, moderate to high
confidence), Lymphocyte and Monocyte are separate strata. They are tested when at least 10 cells
per donor exist in enough donors (MTG: PVM 66 donors, lymphocyte 22, monocyte 4), and
`lymphocyte` / `monocyte` are unmapped cell type groups (never counted).

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
  in several datasets (ROSMAP across M23, GF4, Z20, MY4 and, 152 + 41 donors,
  Lee2026; SEA-AD across the 14 Gabitto2024 files and F26; MADRC across SP4 and
  WA4; Lu across LC5 files). Lee2026 is handled by a donor exclusion rule (above);
  the other overlaps are handled by the cohort table roles only.
  Counting a gene as "recurring" across datasets overstates independent
  replication; cross-dataset summaries need the cohort selection table
  (audit section 8.3). No meta-analysis is done in v1.
* **Disease definition (2026-10-05).** Datasets with both Braak and CERAD use the common
  definition (below); the others keep the author's labels (`disease_basis: author_dx`) and do not
  enter the disease replication. The paragraph that follows describes the older state and still
  holds for those datasets. Only the literal values `AD` and
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

Permutation p: 10,000 shuffles of the facet labels (default of `--permutations`; the first module run used 1,000) across donors with covariates
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
(exploratory). **Resolution limit:** with B = 10,000 the smallest permutation p is
1/10,001, so a q_perm below 0.05 needs at least m / 500 modules at that floor (m =
modules in the family, about 128 to 235 here, i.e. a single module at the floor can pass
q_perm). With the earlier B = 1,000 the floor was 1/1001 and at least m / 50 modules (3 to 5)
had to sit at it, so a single very strong module could not pass; results from 1,000-shuffle
runs should be read with `q_param` and `p_perm` together. `--permutations` changes B.
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

## Serving the results (API, MCP, web)

The server reads the files above directly (`lib/facet_results.py`); there is
no separate publish step. The folder is the config key `facet_results_dir`
(default `<h5adstore>/_facet_tests`), so a site can point it at a copied or
consolidated folder later without code changes. Reads are defensive: a
missing or partly written file is "not available".

* REST: `/databases/{id}/facet-tests` (units with a `model` object built from
  the manifests), `/genes`, `/modules`, `/volcano.png`;
  `/facets/{name}/tests` (units across datasets), `/facets/{name}/effects?gene=`
  (result card with the replication badge), `/facets/{name}/effects/top`
  (recurring genes per cell type), `/genes/{gene}/facet-effects`,
  `/facets/cohorts`. See `doc/API_CHANGELOG.md`.
* MCP: `get_facet_tests`, `facet_effects`, `facet_top_genes`, `facet_units`.
* Web: effects under each facet of the dataset Facets card, "Effects across
  datasets" on the facet page, a forest panel on the gene page, and the help
  section "Facets and facet effects".

Replication (v2) is defined in the next section; the counting of cohorts and cell type groups
follows. The old badge ("replicates in k of m independent cohorts", q < 0.05 with the same sign)
is kept for one release as `legacy_badge` and the old count fields.

Donors behind a badge (2026-10-05). Each replication entry, each recurring
gene row and the chosen cell type group of `/facets/{name}/effects/top` carry
`n_donors_sum` (donors analysed, added over the counted units; a person tested
in two datasets counts twice) and `n_donors_distinct` (each person once).
Distinct counting keys a donor on `ant_donor_uid` where the dataset publishes
one and on `<group_id>:<ant_donor>` otherwise (`lib/donor_keys.py`). Both test
tools record per tested unit the digests of the analysed donors' keys
(`donor_key_digests`, the first 16 hex digits of SHA-256; no identifiers in the
manifests) and per run `donor_keys` (`n_donors`, `n_donors_uid`,
`n_uid_conflicts`). The fallback cannot see one person in two collections, so
`n_donors_distinct` is an upper bound where uids are missing; it is null when a
counted unit comes from a run made before the digests existed (results from
before 2026-10-05 need a re-run).

### Cell type groups (replication counting)

Strata carry their dataset's label: a Cell Ontology name ("microglial
cell"), a dataset constant, or an author label ("Mic P2RY12", "MG", "Astro",
"Z20/ALL/cluster_Micro"). Since 2026-10-05 replication and the cross-dataset
views count by the **group** of a label, from `lib/celltype_groups.yaml`
(resolved by `lib/celltype_groups.py`: exact label, then case folded, then
the longest matching prefix; anything else is `unmapped`). The raw label
stays on every unit (`cell_type`) next to `celltype_group`.

| Group | CL id | Labels (2026-10-05 results) |
| --- | --- | --- |
| microglia | CL:0000129 | microglial cell, Microglia, micro, MG, Mic P2RY12 / TPT1 / MKI67 / DUSP1, Z20/ALL/cluster_Micro |
| perivascular macrophage | CL:0000881 | PVM, perivascular macrophage, CAMs (medium) |
| macrophage (other) | CL:0000235 | macrophage, meningeal macrophage |
| myeloid (mixed) | CL:0000763 | myeloid cell (Lee2026 constant over MG + PVM), Phagocytes |
| immune (mixed) | none | Immune |
| T cell | CL:0000084 | T cell, T cells |
| astrocyte | CL:0000127 | astrocyte, Astrocytes, Astro, astro, Bergmann glial cell, Z20 cluster_Astro |
| oligodendrocyte | CL:0000128 | oligodendrocyte, Oligo, oligo, OL-A, OL-C, Olig_*, Z20 cluster_Oli0/1 |
| oligodendrocyte precursor cell | CL:0002453 | OPC, opc, OPC_*, oligodendrocyte precursor cell, Z20 cluster_OPC |
| glia (mixed) | CL:0000125 | glial cell, Glia, Glia-MIX, MicroOligo |
| excitatory neuron | CL:0000679 | glutamatergic neuron, excitatory neuron, Excit, exc, EN-*, Z20 cluster_Ex0/1, granule cell, unipolar brush cell |
| inhibitory neuron | CL:0000617 | GABAergic neuron / interneuron, inhibitory neuron / interneuron, interneuron, Inhib, inh, IN-*, Z20 cluster_In, Purkinje cell, cerebellar Golgi cell, molecular layer interneuron |
| dopaminergic neuron | CL:0000700 | dopaminergic neuron, CALB1_*, SOX6_* |
| neuron (mixed) | CL:0000540 | neuron, brainstem motor neuron |
| neural progenitor | none | cerebellar granule cell precursor |
| endothelial cell | CL:0000115 | endothelial cell and its artery / capillary / vein / vascular tree forms, Endo cells, endo, Endo_* (Kamath2022, medium), Z20 cluster_Endo |
| mural cell | CL:0008034 | pericyte, smooth muscle cell |
| vascular (mixed) | none | EndoMural, perivascular cell, Endo_DCN_ABCC9 |
| VLMC / fibroblast | CL:4023051 | vascular leptomeningeal cell, fibroblast, Stromal cells (medium) |
| ependymal cell | CL:0000065 | ependymal cell |
| choroid plexus epithelial cell | CL:0000706 | choroid plexus epithelial cell |
| unmapped | none | all (pooled), unknown, LuChen2025 state clusters (cluster_Alz / Res / ND) |

Mixed populations have their own groups and never count as evidence for a
pure class. Neuron groups are coarse classes across regions (cerebellar
Purkinje cells and cortical interneurons are both "inhibitory neuron"), so a
group-level count can combine regions; read the raw labels before drawing a
biological conclusion. Rows marked medium in the YAML are judgement calls.

## Replication (v2)

Per gene and per (facet, cell type group):

1. **Anchors.** A unit is eligible when its cohort counts (role not duplicate, supplement, "do
   not", never, alternative B, T20 only), it is not descriptive only, not a secondary dataset
   (`exclude_secondary`), its group is not `unmapped`, and (disease only) its `disease_basis` is
   pathology based (`nia_reagan`, `braak_cerad`, `adnc`). Each cohort then has **exactly one anchor
   dataset per group**: a donor-pooled unit first, then a role starting with `anchor`, then the most
   direct cell type source, then the most donors. Only the anchor's units with the most direct
   source count; several equal strata of that dataset share the cohort's single vote (their p
   values are multiplied by the number of strata, Bonferroni). Everything else stays on the card
   with `counted: false` and a `not_counted_reason` ("supplementary: pool:madrc_myeloid is the
   anchor of this cohort for this cell type group"). ROSMAP counts through the whole-microglia
   `group_pool` stratum of M23/IMM; MY4/MRM (all donors in M23) and Y22/BBB (ROSMAP participants)
   are supplements; the two Lu cohorts are one cohort.
2. **Cohort p values.** The anchor's `p_treat` (one-sided in the observed direction, tau as above).
   For the tested direction d the cohort p is `p_treat` when the observed effect has sign d and 1
   otherwise.
3. **Partial conjunction (u = 2).** For direction d, the m - 1 largest of the m cohort p values are
   combined by **Simes'** method (`min_i (n p_(i) / i)` over the n = m - 1 selected values; valid
   under positive dependence, which cohorts have: they share pathology, cell-state shifts and, for
   Lee2026 and ROSMAP, persons). Fisher's method (`-2 sum ln p` against chi-square with 2(m - 1) df;
   independent cohorts) is implemented (`facet_results.PC_METHOD`) and was the first choice, but on the
   2026-10-05 results it calls 29 Braak microglia genes replicated against 2 for Simes, and 23 of the
   29 have at most one cohort individually at q_treat < 0.05: it accumulates modest p values of
   correlated cohorts. Disease microglia: 2 and 2; sex microglia: 39 and 30. The method is served
   (`replication_method`). With m = 2 both give the larger of the two p values. The gene's p is the
   better direction times 2.
4. **r-value.** Benjamini-Hochberg over all genes tested in at least 2 cohorts of the (facet,
   group). `replicated` = r < 0.05; the direction is the better one. `r_value`, `p_pc`, `k`, `m`
   and `anchors` are served; `k` = cohorts whose anchor has q_treat < 0.05 in that direction
   (descriptive), `m` = cohorts with a treat p value. Runs from before the treat columns have
   `m = 0` and no r-value until they are re-run.
5. **Never twice.** Disease and Braak are separate facets and nothing combines them: the same
   pathology read twice is not two replications.

Cohorts whose donor independence was never checked (`independence: unknown` in the cohort table:
Gerrits2021, Grubman2019, Jakel2019, Schirmer2019, Smajic2022, Zelic2025, Olah2020, Gazestani/NPH,
Prater2023) count but are flagged (`overlap_unknown`, `cohorts_overlap_unknown`, in the badge) so a UI
can show "overlap unknown". The disease facet does not combine definitions of different kinds:
clinical or author diagnoses (`author_dx`, `clinical_dx`) are listed and not counted.

Recurring genes (`/facets/{name}/effects/top`) list the replicated genes (r < 0.05) ranked by
`r_value`, then `k`, `agreement` (fraction of the m tested cohorts whose effect has the replicated
sign, significant or not), then `median_abs_logFC`; the response gives `n_replicated` and
`n_genes_in_family` for the group.

Sex positive control (dataset card, sex units): passed when XIST is lower and
RPS4Y1 higher in males, both at q < 0.05; the number of Y-linked genes
(RPS4Y1, DDX3Y, UTY, KDM5D, USP9Y, EIF1AY, ZFY, NLGN4Y, TXLNGY) higher in
males at q < 0.01 is shown alongside.

## Reproducibility

Tool version `ant-facet-test/2.0`. Seed 20261004 is passed to R (`set.seed`; limma-voom itself is
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

`tests/test_facet_engine_v2.py` (v2): BH, partial conjunction (Fisher and Simes against closed forms,
NaN rows, `u`), replicability direction; replicate unit inference and the analysis level rule;
`replicate_unit` / `disease_basis` lint; treat columns and tau in the tool (R needed; `p_treat`
never below the one-sided plain p, tau configurable, manifest fields), a non-donor replicate
column, `replicate_unit: none` skipped; group-pool strata; the pooled pseudobulk equals the sum
over member files and an end-to-end pooled run (planted effect found with 20 donors, not 40
samples); the pooled unit wins the anchor in `facet_results`; per-cell aggregates carry
`replicate_unit`. `tests/test_facet_core.py` and `tests/test_facet_results.py`: anchors (pooled, role,
donors), group-pool over sibling strata, pathology-only disease, r-value ranking.
`tests/test_disease_rule.py`: the Braak x CERAD boundaries. `tests/test_md_edit.py`: verified text
edits of the `.md` front matter.
