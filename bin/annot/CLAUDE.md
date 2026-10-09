# bin/annot/ — module scoring + obs annotation

Compute new obs columns and add them to a `.duckdb` (and eventually back to
the `.h5ad`).

| Tool | Role |
| --- | --- |
| `ant-score` | Scanpy module scores from the **genesetstore** (`data/genesetstore/`) → one numerical obs column per geneset. Default: writes to duckdb via `AnnotationWriter`. `--rebuild` does the legacy h5ad rewrite. |
| `ant-score-ext` / `ant-ucell` | The module scores the facet tests and the Genesets tab use: scanpy-style `score_genes` computed from the duckdb (`ant-score-ext`, sidecar source `geneset`) and UCell (`ant-ucell`, source `ucell`), both written to `<stem>.scores.parquet` (read-only on the duckdb, no ant-serve coordination). Per-cell-type scope, see below. |
| `ant-score-fleet` | `plan` / `run`: score every facet-eligible dataset (and every dataset that already has a sidecar) with each catalog whose cell type scope contains at least one of its cells, via `ant-score-ext` + `ant-ucell`; removes stale catalogs; nice 19, pinned CPUs, resumable state file, pauses on other users' load. Supersedes the two older `ant-score-*-fleet` scripts for scoring (the microglia one also runs starCAT). |
| `ant-starcat` | Project a dataset onto the `MICROGLIA_V1` starCAT catalog → 43 `starcat__MICROGLIA_V1__*` obs columns. Human-only; needs raw integer counts (not SCT). |
| `ant-annotate` | Load obs columns from a TSV. `status` shows what's pending, `rollback --run-id` reverts a batch. |
| `ant-facet-test` | Replicate-level facet tests (sex, AD vs control, APOE e4, Braak, age per decade, App NL-G-F vs control) by pseudobulk limma-voom per cell type and facet scope (human post-mortem, human xenograft in mouse; pure mouse datasets have no facet layer); exploratory. Output only under `<h5adstore>/_facet_tests/`. See `doc/DESIGN_facet_testing.md`. |
| `ant-facet-test-modules` | Same facets for module scores: donor means of the `.scores.parquet` scores (variant A) and pseudobulk background-matched scores (variant B, needs counts), OLS + 10,000-shuffle permutation p (default), BH per (dataset, stratum, facet, method). |
| `ant-derive-disease` | The common pathology-based `ant_disease` (Braak x CERAD 2x2, `disease_basis: braak_cerad`): writes the stored column `disease_braak_cerad` for SEA-AD, ROSMAP, MADRC WA4 and Prater (table `DERIVATIONS`), edits the `.md` (verified text edit: rename to `ant_disease`, `disease_derivation`, notes, version bump) and sets `disease_basis: author_dx` on the other datasets. `plan` shows counts. After `apply`: `bin/ops/ant-rebuild-obs-views --only <db_id>`, then `ant-sync push`. Needs `ANTHIVE_H5ADSTORE` / `ANTHIVE_CONFIG` when run from a worktree (the view rebuild resolves the crosswalk join through the configured store). |
| `ant-suggest-tops` | Walk all `.md` files and propose `top_genes` / `top_cat` / `top_num` from curated marker panels + the cell's actual obs/var. See the script's docstring for the panels. |

Facet engine v3b (2026-10-06, review): the effect gate is a separate field (at least 2 cohorts with log2FC >= tau), `r_treat` is the second tier, units carry lambda / inflated / notes / covariates_dropped / sex-discordance, `age` and `age_adjusted` run side by side (age floor 60), `app_nlgf` is stratified by timepoint and graft line from the `.md` (`control_basis`, `app_nlgf_design`), limma uses quality weights and robust eBayes, residual df floor 4.

Donor plots (2026-10-09): both facet tools write per-sample sidecars next to the results (`<stem>.facet_samples.parquet` and
`.facet_sample_cells.parquet`; `.facet_module_samples.parquet` and `.facet_module_sample_scores.parquet`) that `lib/facet_donor_values.py`
turns into the replicate-level values of a result cell. `facet_test_limma.R` writes `sample_norm.csv` (library size after
`filterByExpr`, TMM factor). `run --samples-only` (both tools) backfills the sidecars of existing results without touching them and
refuses results made from another `.md` or with other tested units. Never write a replicate id into a sidecar.

Facet engine v3c (2026-10-07): `dataset_eligibility` also refuses datasets excluded from the facet layer
(`facet_scopes.facet_exclusion_reason`: spatial or mass spectrometry technology, `facet_exclude: true` in the `.md`); `n_tissues` counts
`ant_tissue`, `tissue_uberon` and `ant_region`; `ant_tissue` is a coarse facet (`lib/tissue_groups.yaml`). See the v3c section of
`doc/DESIGN_facet_testing.md`.

Facet engine v3 (2026-10-06, `doc/DESIGN_facet_testing.md`): datasets run in their facet scope (`lib/facet_scopes.py`; manifests
and result rows carry `facet_scope`; a facet is only tested where its registry `facet_scopes` allow it), eligibility is the
statistical minimum (`lib/facet_rules.py`: small cohorts are tested, counted and flagged `small_sample`), `app_nlgf` and `age`
are tested facets, replication uses the ordinary one-sided p values plus an effect gate, and `summarize` writes per scope
(`replicated_<scope>_<facet>_<group>.tsv`). `ant-facet-test-modules summarize` adds `module_replicated.tsv` (partial conjunction on
permutation p, gate |std_effect| >= 0.2). The modules tool has no donor-pooled units.

Facet engine v2 (2026-10-05, `doc/DESIGN_facet_testing.md`): `ant-facet-test` writes limma treat
columns (`p_treat`, `q_treat`, tau log2(1.1), Braak per stage log2(1.1)/3), pools a donor's cells over
the member files of a cohort (`run --pool ID`, cohort table column `pool_members`, output
`_facet_tests/_pooled/`), adds a derived `group_pool` stratum per cell type group, uses the dataset's
`replicate_column` as the pseudobulk unit and records `replicate_unit` / `analysis_level` per unit;
`ant-facet-test summarize --out-dir DIR --genes ...` writes the replication tables.
`ant-facet-test-modules` records the same replicate fields and gates hits on `|std_effect| >= 0.2`.

## Module scores are per cell type (2026-10-08)

`celltypes` is REQUIRED in every genesetstore experiment `index.md` (Cell Ontology labels of the `.h5ad.mus`
`cell_type` vocabulary, or `all`; `lib/geneset.py` refuses an experiment without it and `ant geneset validate`
reports it). A per-geneset `.md` may override `celltypes` and `role`. `role: markers` marks QC marker panels
(`LuChen2025/nbb_celltypes`, `PanglaoDB2019/*_markers`): scored on all cells, never facet-tested.

- `lib/celltype_scope.py` resolves the scope: a label scope matches the cells whose `ant_cell_type` has the same
  `group_of` group (`lib/celltype_groups.yaml`); `all` and markers match every cell. The per-cell label is read through
  the obs views of the current `.md` (as the facet tools do), with the `.h5ad.mus` `cell_type` as fallback. A dataset
  without any cell type information only receives `all` and markers modules; a label outside the group table is
  `unmapped` and does the same.
- Cells outside a module's scope get NO row in the sidecar (NULL, never zero). A catalog with no matching cell is not
  scored and its stale `(source, catalog)` rows are removed (`scores_parquet.delete_scores`).
- scanpy `score_genes` takes its control genes from the expression of the cells it scores, so a restricted module is scored
  with bins rebuilt from the matching cells only (`AnthiveDB.score_module(cell_ids=...)`; `ant-score` scores a copy of
  the matching cells). All-cells modules use the stored `__gene_bins`. UCell ranks genes within a cell, so its scores do not
  depend on which other cells are scored.
- `ant-facet-test-modules` tests a module only in strata of a matching group (all: every stratum, markers: never); the BH
  family is the set of in-scope modules of the stratum. No donor-pooled module units exist.
- `ant-aucell` is not scoped and not part of the defaults (no `aucell` rows exist in the store).

Default build steps of a dataset (decision 2026-10-08): scanpy `score_genes` (`ant-score-ext`, source `geneset`) plus ONE
rank scorer, UCell (`ant-ucell`, source `ucell`), both per cell type scope, automatically after `ant-convert`
(`ant-score-fleet run --only <db_id>`; design in `doc/DESIGN_dataset_ingestion.md`). `ant-facet-test-modules` tests exactly
these two sources (`DEFAULT_SOURCES`), fleet-wide. Why UCell and not AUCell (benchmark 2026-10-08, same modules
`Sierksma2025/WGCNA` + `Mancuso2024/Cell_states`, `--dry-run`, 4 CPUs, nice 19): on G24/MMG (40k cells) UCell took 1:35 wall
and 14 GB, AUCell (pyscenic, single thread) 13:44 and 45 GB (8.7 times slower); on L26/PSY (289k cells, 252k microglia) UCell
took 10:01 and 40 GB, AUCell was not run (it scales to well over 100 GB, 45 GB at 40k cells). `ant-ucell` ranks the cells of a
scope once for all catalogs of the call (ranking dominates the cost), so pass every catalog in one call.

## Write path (the shared contract)

All three write-tools use `lib/anthive_db.AnnotationWriter`:

1. `flock` on `<duckdb>.lock` — serialises writers.
2. `ALTER TABLE __obsnum / __obscat ADD COLUMN <new_col> <type>`.
3. Insert the new values.
4. Record an entry in `__pending_cols` (col_name, target_table, kind,
   added_at, annotator, run_id, catalog, source_hash) — the audit trail
   `ant-merge-h5ad` reads.
5. Refresh the `obsnum` / `obscat` **views** (so `o.*` sees the new column).
6. All-or-nothing per `with` block. Auto-rollback on exception or no-commit.

Categorical cardinality is capped (rejects cell-id-like columns).
Numerical writes go to `__obsnum` (DOUBLE); categorical to `__obscat`
(VARCHAR).

## Coordination

If `ant-serve` is up and serving the same duckdb, AnnotationWriter
auto-handles the release/restore POST cycle when given `db_id` and
`ant_serve_url`. `--no-coordinate` skips this.

## After scoring

The new columns are queryable immediately on the duckdb (so plotting
endpoints work) but are NOT in the `.h5ad` yet. Run `bin/ingest/ant-merge-h5ad`
to push them into the .h5ad before the next `ant-convert` rebuild.

## Heuristics in ant-suggest-tops

See `src/sierksma2025/build_wgcna_modules.py` for an example of a
one-off panel build. `ant-suggest-tops` uses curated marker panels for
~16 cell types + disease modifiers (AD GWAS, PD genes, etc.), intersected
with the genes actually present in the duckdb. Mouse case-fallback via
title-case.

## DE sidecar repair

`ant-de-restore` scans every dataset for `.de.parquet` studies that the
duckdb does not serve (exit 1 on any gap). `--fix` restores them through
`DEWriter` (ant-serve release/restore, no restart); `--embed-metadata`
rewrites legacy sidecars so they carry the full `__de_study` rows. Push
the changed files with `ant-sync push` afterwards.

## Per-cell variability tools: scope

`ant-module-variability` and `ant-gene-variance-precompute` describe
differences between groups of cells and share `lib/cell_level_scope.py`:
bulk datasets (technology containing "bulk", or at most 1.5 observations per
donor) are skipped and listed with the reason in `<aggregate>.skipped.json`
(read by ant-serve: `/module-variability` `skipped_datasets`,
`gene-variance-by-facet` `precompute_skipped`); donor-like columns
(`ant_donor`, `ant_donor_uid`, ...) are always categorical, never quantile
binned. Caution: `ant-module-variability --only` writes an aggregate that
holds only the datasets it ran, so never use it on the fleet parquet.
`ant-gene-variance-precompute --only` keeps the other datasets.
