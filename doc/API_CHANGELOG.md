# Anthive REST API — Changelog

Versioned record of contract changes to `ant-serve`. The single source of
truth for "is my client still up-to-date?" is the **`API_VERSION`** string
in `bin/api/ant-serve` (currently surfaced at `/api/health.api_version`,
`/api/version.version`, and as `info.version` in `/api/openapi.json`).

### Versioning rules

* `patch` (2.2.x) — bug fix only, no contract change. Clients don't need to update.
* `minor` (2.x.0) — **additive**. New endpoints, new optional params with safe defaults, new optional response fields. Existing callers keep working unchanged.
* `major` (x.0.0) — removes / renames endpoints, changes response shape, tightens an existing param's constraints. Old clients break.

### How to check if your client is up-to-date

```sh
curl -s http://<host>/api/version
```

Returns a compact JSON with the version stamp, recommended antclient pin,
the canonical OpenAPI URL, the supported plot geoms, and the list of
deprecated endpoints. Cache the `version` field client-side; if it changes,
re-read `/api/openapi.json` to regenerate bindings (or read this file to
understand the diff).

The OpenAPI spec itself (`/api/openapi.json`) is auto-generated from
FastAPI route signatures, so it's **always** current — there is no risk of
the spec drifting away from the running code.

---

## 2.45.0 (unreleased)

Release summary. The data behind the new gene, gene set and facet views: forest rows with limma intervals and the
calls a view draws, module (gene set) replication across cohorts with the member-gene counts next to it, an
uncapped and paged top list, a one-call facet overview, the low-detection pill and the modality (nuclei or cells) of
every dataset. The facet statistics are unchanged.

**Breaking changes in this release** (marked BREAKING below; see "Security and access" and "Renames and
error codes"). By the versioning rules above several of them would call for a major version; they ship in 2.45.0 because
they close security gaps or remove unreleased or announced names. Clients that only read public data as a signed-in
user need no change.

Security and access (2026-10-13):

- BREAKING: **`access: internal` datasets need an authenticated caller.** A dataset whose resolved annotation is not
  `access: public` (internal, or no `access` value) is hidden from callers without an identity: it is missing from
  every listing (`/databases`, `/groups/{group_id}`, `scored_in` of the gene set routes, `/genesets/.../rank`,
  facet results and cohorts, donor variability, `/module-variability`, the facet inventory's signal strength, gene
  detection, donor-pooled units with an internal member, the dataset links and `n_datasets` of papers) and every
  route that names it (`/databases/{id}/...`, a `db_id` / `db_ids` / `dataset` / `datasets` / `db_x` / `db_y`
  query parameter, the dataset inside a `/notebook/{b64}` link) answers `404 {"detail": "Database '<id>' not
  found"}`, the answer for a dataset the site does not have. An identity is a non-empty `Remote-User` set by the
  proxy (ignored on `auth.mode: none`), a local maintenance tool (loopback, no `X-Forwarded-For`, no user) or the
  internal secret without a user. A request with `X-Anthive-Anonymous` is anonymous whatever else it carries;
  ant-browse and ant-mcp send it when they act for a visitor who is not signed in, and ant-browse now forwards the
  visitor's `Remote-User` on every server-side call. Site setting `internal_requires_auth` (default true, env
  `ANTHIVE_INTERNAL_REQUIRES_AUTH`, `site.yaml` `auth.internal_requires_auth`); `lib/dataset_access.py`.
  Sites with a login see no change.
- BREAKING: **`X-Remote-User` and `X-Remote-Groups` are no identity.** ant-serve used them as a fallback when
  `Remote-User` / `Remote-Groups` were empty, which let a client claim `X-Remote-Groups: admins` behind a proxy that
  did not strip them. Only `Remote-User` / `Remote-Groups` count; every Caddy auth snippet and the nginx examples drop
  the `X-Remote-*` headers. A forged `X-Remote-Groups: admins` gets 403, a chat or `/me/mcp` call with only
  `X-Remote-User` gets 401.
- BREAKING: **Server-control routes need an admin, a host-local tool or the internal secret** (the
  `_require_admin_or_local` rule of `/admin/rescan`): `POST /admin/api-config/{id}`, `GET /admin/plot-cache/stats`,
  `POST /admin/plot-cache/clear`, `GET /admin/gene-stats-cache/stats`, `POST /admin/gene-stats-cache/clear`,
  `POST /admin/gene-stats-cache/gc`, `GET /admin/gene-stats-index/stats`, `POST /admin/gene-stats-index/reload`,
  `POST /admin/gene-stats-index/rebuild`, `GET /admin/gene-variance-index/stats`,
  `POST /admin/gene-variance-index/reload`, `POST /admin/gene-variance-index/rebuild`,
  `GET /admin/gene-variance-cache/stats`, `POST /genesets/rescan`, `POST /module-variability/rescan`,
  `POST /catalogs/rescan`. They relied on the proxy alone (the last three not even that); other signed-in users now
  get 403. pyvark `rescan_genesets`, `rescan_module_variability`, `rescan_catalogs`, `plot_cache_stats` and
  `plot_cache_clear` need an admin account; `ant sync` sends the internal secret and is unaffected.

Renames and error codes (2026-10-13):

- **One 404 for an unknown dataset.** Every route answers `404 {"detail": "Database '<id>' not found"}` (was:
  `Database not found: <id>`, `Dataset '<id>' not found`, `db_id '<id>' not found` or the message with embedded
  quotes). The DE routes (`/databases/{id}/de`, `/de/{study}`, `/de/{study}/summary`, `/de/by-gene/{gene}`,
  `/de/{study}/terms/...`) answered 500 for an unknown dataset; they answer this 404 now.
- `GET /databases/{id}/module_ranks` (new in this release) is `GET /databases/{id}/module-ranks` (kebab case like
  the other new paths); no alias, it was never released.
- `GET /donor-variability/top` answers `celltype_group` (was `cell_type_group`, the only route with that spelling).
- The effect gate rule in a response is `effect_gate_rule` (was `effect_gate`, the same name as the boolean
  `effect_gate` query parameter of the same routes): `{std_effect, wording}` on `/facets/{facet}/modules/top` and on
  the module card of `/genesets/{study}/{experiment}/{name}/facet-effects` (was a bare number there),
  `{std_effect, per_facet}` on the gene set card. The query parameter keeps its name.
- `GET /facets/{facet}/donor-plot.{fmt}` with a format other than png, svg or pdf: 400 (was 404).
- `GET /databases/{id}/donor-variability` checks `rung`, `sort` and `feature_type` up front: a bad value is a 400 also
  without `feature_type` (it was ignored with a 200). `cell_type` takes the cell type group too (`microglia`), like
  the other donor and facet routes: the exact stratum label wins, else the unit of that group with the most replicates.
- BREAKING: **The `apoe_carrier` alias is gone**, as announced in 2.43.0: `/facets/ant_apoe_carrier`,
  `/facets/apoe_carrier/...` and `facet=apoe_carrier` are unknown facets (404 or 400 like any other), the
  `facets_visible` entry `ant_apoe_carrier` matches nothing, and views built from now on have no
  `ant_apoe_carrier` column (views built earlier keep it until they are rebuilt; the facet listings hide it). Facet
  test results written under the old name are still read under the new one.
- `/version`: `released` is null until the release is cut (it said 2026-10-07, the 2.44.0 date); new
  `compatible_pyvark` (`>=0.9.0`), the oldest pyvark that works with this API. `compatible_antclient` carries the same
  pin and is deprecated (antclient became pyvark); it is removed in 2.46.

Changed behaviour (2026-10-08 to 2026-10-10):

- **Xenograft sex facets.** New tested facets `graft_sex` (sex of the donor line of the grafted cells, the test of
  `ant_sex` in xenografts; `facet_column` is `ant_sex`) and `host_sex` (`ant_host_sex`, the mouse) on every facet route.
  `sex` is human post-mortem only now: a `facet=sex` call with `scope=xenograft_human_in_mouse` no longer finds
  results (use `graft_sex`). Villages (several donor lines per animal) are refused as mixed.
- **Gene sets.** Every experiment in the gene set store must declare `celltypes` (labels or `all`); experiments
  without a valid value are dropped from the registry (and from `/genesets`, the cards and the module views). The gene
  set detail and card add `role` (`markers` for QC panels); a set's own `.md` may override `celltypes` and `role`. Module
  scores and the module facet tests cover only the cells of a set's cell type scope.
- **Score index from the sidecars.** Gene sets scored only in a `.scores.parquet` sidecar (not as an obs column) are
  in the score index now, so `scored_in`, `/databases/{id}/module_scores` and the gene set listings can name more
  datasets than in 2.44.
- Module variants (gene set card) add `family_rank` and `family_size`: the rank by `p_pc` among the modules of the
  same family ("ranks 2nd of 122"). For the `sex` facet the member-gene counts leave out genes on chrX and chrY
  (`members.n_sex_chromosome_excluded`). Gene top rows (`/facets/{facet}/effects/top`) add `min_logFC` / `max_logFC`,
  module top rows `min_effect` / `max_effect` (smallest and largest per-cohort effect). The gene and module top lists add
  `n_cohorts_tested` and `not_tested` (why a cell type group has no family, null when it has one).

New endpoints:

- `GET /genesets/{study}/{experiment}/{name}/facet-effects` (`facet` comma list, `scope`, `cell_type`,
  `exclude_secondary`, `include_units`, `forest`): the module result of one gene set per facet and scope next to the member genes
  that replicate for the same facet. Shape: `{geneset_id, catalog, module, display_name, description, organism, n_genes,
  celltypes, role, direction, quality: {mt_fraction, ribosomal_fraction, mt_dominated, ribosomal_dominated,
  quality_sensitive}, directionless, directionless_reason, directionless_note, member_match, scored_in: [{db_id, title,
  facet_scope, cohort_label, cell_types, groups, variants, facets, has_counts, counted, modality, n_donors}], scope,
  facets: {facet: {facet, scope, module, members}}, by_scope (only without `scope`), effect_gate_rule: {std_effect: 0.2,
  per_facet: {facet: tau}}, help_topics}`. `module` is `{catalog, module, module_key, facet, scope, cell_type, directionless,
  effect_gate_rule: {std_effect, wording}, replication: [{celltype_group,
  cl_id, labels, primary: {method, source, label}, variants: [variant], ab: {disagree, reason} | null}], n_units, units}`;
  a `variant` is `{method, source, label, basis, forest, r_value, p_pc, k, m, r_direction, replicated, call, nt_label,
  gate, effect_gate_passed, effect_gate_cohorts, n_cohorts_ge_gate, median_std_effect, median_estimate, agreement,
  r_cohorts, n_small, small_cohorts, k_over_m, cohorts_label, family_rank, family_size, strict: null}` (plus the
  fields of the module statistics fixes below). `members` is `{facet, scope, scopes,
  n_members, n_sex_chromosome_excluded, groups: {celltype_group: {n_members, n_in_data, n_tested, n_replicated, n_up, n_down, n_effect_gate,
  n_strict, genes_up, genes_down, genes_gate, genes: [{gene, r_value, direction, median_logFC, k, m,
  effect_gate_passed, strict}]}}}`, never truncated; null when the geneset store has no such set. Module
  replication uses the same partial conjunction as genes (Simes, u = 2) on the permutation p values of the donor-level
  scores, BH over the modules of one (scope, facet, cell type group, method, source) family; the effect filter is a
  signed standardised effect of at least 0.2 in 2 cohorts; there is no strict tier (`strict` null). The view variant
  is the pseudobulk of counts (B) where the module has one, else the mean of per-cell scores (A: UCell, then
  score_genes); `ab.disagree` is true only for opposite directions with one variant replicated or both effects at
  least 0.2. `include_units` (default false) adds the flat unit list. A variant `forest` row has the cohort row fields of a gene forest
  with `estimate`, `ci_low`, `ci_high`, `std_effect`, `p_param`, `q_param`, `p_perm`, `q_perm` and `has_counts` in place of the
  log fold change fields; `forest` (`full` | `anchors` | `none`, default `anchors` here) sets the detail as for genes.
- `GET /facets/{facet}/modules/top` (`cell_type`, `n`, `offset`, `effect_gate`, `include_not_replicated`, `catalog`,
  `exclude_secondary`, `scope`): modules ranked by r-value, then k, agreement and the standardised effect. Shape
  `{facet, scope, scopes, cell_type, celltype_group, labels, cohorts_in_cell_type, n_cohorts_tested, not_tested,
  n_modules_in_family, n_replicated, n_effect_gate, offset, n_matching, n_returned, has_more, cell_types: [{cell_type,
  celltype_group, cl_id, labels, n_cohorts, n_units, n_modules_in_family, n_replicated, n_effect_gate}], rows,
  effect_gate_rule: {std_effect, wording}, exclude_secondary, help_topics}`. A row is `{geneset_id, catalog, module,
  method, source, variant_label, r_value, p_pc, k, m, r_direction, replicated, r_significant (same as replicated), call,
  nt_label, gate, effect_gate_passed, effect_gate_tau, effect_gate_unit, effect_gate_cohorts, n_cohorts_ge_gate,
  median_std_effect, median_estimate, min_effect, max_effect, agreement, r_cohorts, n_small, small_cohorts, k_over_m,
  cohorts_label, n_cohorts_tested, p_check_cohorts, p_check_flag, per_region_only_cohorts, module_scope_note,
  other_variants: [{method, source, label, call, r_value, median_std_effect, r_direction, cohorts_label}], ab,
  quality_sensitive, directionless, n_genes}`; the gene tiers `strict`, `r_strong`, `replicated_strong`, `r_treat` and
  `replicated_effect` are always null for modules. A row has no `label`, `basis` or `forest` (those are on the card
  variants).
- `GET /facets/{facet}/effects/overview` (`n`, `scope`, `exclude_secondary`): the simplified facet view in one call.
  `{facet, scope, scopes, headline: {celltype_group, cohorts, n_genes_in_family, n_replicated, n_effect_gate, n_strict,
  strict_genes, effect_gate_unit}, cell_types: [{celltype_group, cl_id, n_cohorts, n_genes_in_family, n_replicated,
  n_effect_gate, n_strict, n_assigned, genes: [{gene, r_value, direction, median_logFC, k, m, effect_gate_passed,
  strict, also_in, detection, low_detection}]}], quiet_groups: [{celltype_group, n_cohorts, n_genes_in_family,
  n_replicated, n_effect_gate, n_strict, low_power}], n_genes_unique}` (the counts of a quiet group are flat fields, as
  on `cell_types`). A gene replicated in several groups is listed once, in the group where it is strongest.
- `GET /genes/{gene}/detection`: `{gene, low_detection_pct: 5, min_cells: 50, n_candidates, n_missing, partial,
  summary: [{scope, celltype_group, n_datasets, pct_median, pct_min, pct_max, n_cells, low_detection}], datasets:
  [{db_id, title, scope, celltype_group, cell_type, n_cells, pct, modality, modality_note, technology, mean, median_nonzero, q10_nonzero, q90_nonzero, rank_mean}]}` from the
  precomputed gene statistics and the per-gene cache (never a live scan).
- `GET /facets/{facet}/donor-values` (`cell_type` required, exactly one of `gene` or `module` = `Study/Experiment/name`,
  `method` and `source` for a module variant, `scope`, `adjusted`, `include_uncounted`, `exclude_secondary`): the replicate-level
  values behind one result cell. Shape `{facet, scope, subject: {gene} | {module, method, source}, cell_type, facet_kind
  (two_group | numeric), levels (two-group: [reference, tested]), x_label, scale, adjusted, n_units, n_counted, note,
  anonymisation, units: [{db_id, title, cohort, cohort_label, cohort_display, cell_type, celltype_source, counted,
  not_counted_reason, pooled, descriptive_only, replicate_unit, available, reason, covariates, n_donors, scale, facet_kind,
  model: {estimate, ci_low, ci_high, p_value, q_value, unit}, line: {slope, intercept} | null (numeric facets), donors:
  [{label, facet_value, group (two-group), value, n_cells}]}], help_topics}`. One unit per counted anchor (the units that
  carry a cohort's vote; `include_uncounted` adds the rest with the reason). `label` is a sequential `D01`, `D02` ... per
  unit, ordered by facet value; no donor id, crosswalk id or per-donor covariate value is ever returned (`covariates` names
  what the model adjusts for). Gene `value` is the pseudobulk on the exact scale the test fits: `log2((count + 0.5) /
  (library size * TMM factor + 1) * 1e6)`, library size after `filterByExpr` (the voom input; its mean over the units samples is
  the stored `ave_expr`); module `value` is the replicate score the module test fitted (`scale` names it). `facet_value` is
  the 0/1 group (with `group` naming it), the Braak stage, or age in years. `adjusted=true` removes the covariate part by
  ordinary least squares on the same scale (the test itself also uses voom precision weights, so a drawn trend can differ
  a little from `model.estimate`; `line` has the reported slope through the centre of the dots, per year for age). A unit
  whose results predate the per-replicate sidecars answers `available: false` with a `reason` until it is rerun or
  backfilled (`ant-facet-test run --samples-only`, `ant-facet-test-modules run --samples-only`; seconds to minutes per
  dataset). Errors: 400 (both or neither of `gene` and `module`, bad `scope`, malformed `module`), 404 (not tested).
- `GET /facets/{facet}/donor-plot.{png|svg|pdf}` (same parameters, plus `width`, `height` per panel, `dpi`): small multiples,
  one panel per counted cohort on one shared y axis. Two-group facets: dots, the box of the quartiles and a bar at the group
  mean per group. Numeric facets: scatter with the straight line of the reported slope. The image is clean: panel titles are
  only the cohort and `n =`; the effect, interval and q of every cohort, the scale and the covariates of the model are in the
  `X-Plot-Caption` header and the image metadata, never drawn. 404 with the reasons when no unit has stored values.
- `GET /databases/{db_id}/donor-variability` (`feature_type` gene | module, `cell_type`, `celltype_source`, `feature`, `method`,
  `sort` excess | sd | reliability | feature, `rung`, `min_reliability`, `limit` up to 2000): donor (replicate) variability from
  `ant-donor-variability`, per cell type stratum with at least 12 replicates of at least 40 cells; a replicate is the dataset's
  `replicate_unit` (donor; animal for xenografts, where it is host-to-host variability). Without `feature_type` the summary
  `{db_id, status (analysed | not_analysed | not_run), reason, facet_scope, replicate_unit, replicate_column, files: {genes,
  modules}, run: {tool, timestamp_utc, seed, versions, md_sha256, anthive4_git, h5adstore_git, counts_layer, batch_column,
  n_replicates_all, n_strata_analysed, runtime_seconds}, parameters (gates, rungs, normalisation), units: [{cell_type,
  celltype_source, celltype_group, n_replicates, status, rung_deepest, rungs: [{name, terms, n, p, residual_df, computed,
  reason}], terms, terms_omitted, technical_covariates, composition_pcs, n_factors, median_cells_per_replicate, n_genes_gate,
  median_reliability, median_sd_donor, lmg_rung, n_genes, n_modules, counted_in_cross_cohort}], help_topics}`. With
  `feature_type` and `cell_type` it adds `{unit, rung, n_matching, n_returned, rows}`. A gene row is `{gene, n_replicates,
  reliability, reliability_ci, r_half, v_donor, v_donor_ci, sd_donor (log2 units), fold_range_donor (2 ** (2 sd)), var_half,
  rungs: {raw | D0 | D1 | D2 | D3 | D4 | F: {n, p, terms, v, sd, fold_range, share_design, excess_z, p_excess} | null}, deepest_rung,
  share_design, share_design_ci (deepest rung before factors), share_blocks: {technical, covariates, facets, latent}, lmg:
  {technical, composition, sex, age, pmi, batch, disease, apoe4, braak} | null (only at n - p >= 30), lmg_rung,
  r_composition_pc1, ave_expr, half_count_q70}`; `v` / `sd` of a rung are the variance and SD left unexplained after that rung's
  design (the raw rung: all replicate differences), `share_design` the share of the replicate variance the design explains, `excess_z`
  / `p_excess` a within-unit ranking of the unexplained variance against the mean-variance trend (a rank, not a test; null below 20
  replicates and for genes with reliability under 0.3). A module row adds `module` (`Study/Experiment/name`), `method`
  (`pseudobulk` primary, `cell_mean` secondary), `source` (`gene_list`, `geneset`, `ucell`), `n_genes_found`; modules have no
  `excess_z`. Rungs: D0 technical block (cells, UMI per cell, genes per cell, mitochondrial fraction, ambient score, long gene
  index, composition PCs), D1 + sex, age, PMI, batch, D2 + disease (host genotype for xenografts), D3 + APOE e4, D4 + Braak, F +
  3 to 5 latent factors; a rung a cohort cannot support (n - p < 20, no new term) is `null`. `cell_type` is the stratum
  label or its cell type group (`microglia`; the exact label wins, else the unit of that group with the most
  replicates). Errors: 400 (bad `rung`, `sort` or `feature_type`, also without `feature_type`; `feature_type` without
  `cell_type`), 404 (unknown dataset or unit).
- `GET /genes/{gene}/donor-variability` (`scope`, `cell_type`, `rung` default D1, `exclude_secondary`): one gene in every analysed
  unit. `{feature, feature_type, scope, rung, rung_label, units: [{db_id, title, cohort, cohort_label, cell_type, celltype_source,
  celltype_group, n_replicates, replicate_unit, rung_deepest, counted, not_counted_reason, values: <gene row above>}], by_group:
  [{celltype_group, m (counted cohorts that reach the rung), k (cohorts in which the gene is in the top 5 percent of the excess
  ranking), m_with_gene, p_rank_product, r_value, rung}], help_topics}`. A unit is `counted` when it is the anchor of a counted cohort for the cell type
  group (one per cohort and group: most direct stratifier, then most replicates) with at least 30 replicates. `p_rank_product` is
  Fisher's combination (a rank product) of the cohorts' `p_excess` over the cohorts that ranked the gene (at least 2), `r_value`
  its Benjamini-Hochberg adjustment over the genes of the (scope, group, rung) family. A rank p value is uniform over genes and
  cannot be below 1 / (number of genes), so the null is that a gene's ranks in different cohorts are independent: the pair
  measures whether the excess reproduces. A partial conjunction as in the facet tests could never call a gene on rank p values.
  Relative and exploratory, not a test. Scopes are never mixed (default `human_postmortem`).
  404 when no analysed unit has the gene in the scope.
- `GET /genesets/{study}/{experiment}/{name}/rank` (`db_id` optional, one dataset of `scored_in`): where the mean score of a gene set
  ranks among ALL gene sets scored in the same dataset (a mean module score is not comparable between datasets, a rank within a dataset
  is). Shape: `{geneset_id, primary_source: 'geneset', secondary_source: 'ucell', min_cells: 20, min_peers: 10, datasets: [{db_id,
  geneset: R, ucell: R}], not_available: [{db_id, reason}], help_topics}` with `R = {available, reason, rank, n_modules_compared,
  n_cells, mean, std, cell_types: [{cell_type, n_cells, mean, std, rank, n_modules_compared}]}`. `std` is the standard deviation of the score over the scored
  cells of the module (spread between cells, not donors; per cell type group, and pooled over the groups for the dataset value; null
  for a single cell; caches written before this field are rebuilt on the next request). `rank` runs from 0 (lowest mean) to 1 (highest):
  the fraction of the other modules of the same source, any catalog, with a lower mean (ties half), taken per cell type group on the
  cells of that group (modules are scored on the cells of their cell type scope, `all` modules on every cell; '' = all cells of a
  dataset without cell types) and averaged weighted by the cells of the module for the dataset value. At least 10 peers and 20 cells
  per module and stratum are needed, else `rank` is null with a `reason`. `geneset` (scanpy `score_genes`, matched control set) is
  the primary ranking; `ucell` depends on the size of the gene set and is secondary. Within one dataset only: never compare ranks or
  means between datasets. Computed from the `.scores.parquet` sidecar joined with `ant_cell_type`, cached per dataset under
  `<tmp_dir>/module_rank/` until the sidecar or the duckdb changes (first request of a dataset reads its whole sidecar: seconds to
  about a minute; `scores_index.parquet` has no cell type column and can lag the sidecars, so it is not used).
- `GET /databases/{db_id}/module-ranks`: the dataset view of the rank route. `{db_id, available, reason, min_cells,
  min_peers, modules, help_topics}`; one row per gene set scored in the dataset (`geneset` source): `{geneset_id, study,
  experiment, name, display_name, column_name, available, reason, rank, n_modules_compared, n_cells, mean, std}` (`std`:
  standard deviation of the score over the scored cells, not donors); `available: false` with a `reason` for a dataset
  without per-cell scores. Same cached table and rules as `/rank`; within one dataset only.
- `GET /genesets/{study}/{experiment}/{name}/donor-variability` (`scope`, `cell_type`, `rung`, `method`, `source`,
  `exclude_secondary`): the same card for a gene set (the `pseudobulk` variant unless `method` is given); `k`, `p_rank_product` and `r_value` are
  null (no ranking test for modules).
- `GET /donor-variability/top` (`cell_type` default microglia, `scope`, `feature_type` gene | module, `rung`, `limit`, `min_cohorts`
  default 2, `exclude_secondary`): features that vary between replicates in several cohorts at one rung. `{scope, celltype_group,
  feature_type, rung, rung_label, cohorts: [{db_id, cohort, title, cell_type, celltype_source, n_replicates, replicate_unit,
  rung_deepest}], m_cohorts, concordance: {mean_spearman, n_pairs, pairs: [{cohorts, n_genes, spearman}]} | null (mean
  Spearman correlation of `excess_z` between the anchors over the genes both rank), min_replicates_per_cohort: 30, high_tail: 0.05,
  not_enough_cohorts, rows, note, help_topics}`. Gene
  row `{gene, m, k, p_rank_product, r_value, median_excess_z, median_reliability, median_sd, fold_range}` sorted by `r_value`, then
  `median_excess_z`; module row `{module, method, source, m, k (cohorts with reliability >= 0.3), median_reliability, median_sd,
  median_share_design, fold_range}` sorted by `median_sd`. Fewer cohorts than `min_cohorts` (counted, 30 or more replicates, rung
  reached): `not_enough_cohorts` true and no rows.

Changed (additive):

- `GET /databases/{id}/facet-tests`: each unit's `genes` adds `n_up` and `n_down`, the genes at q < 0.05 with a
  positive or negative logFC (up or down in the test level, or with the trend). Counted from the gene results file;
  null when only the run manifest has the counts. Used by the dataset page Facet > Gene matrix.
- `GET /databases/{id}/facet-tests`: each entry of a unit's `modules` adds `n_q05`, `n_up` and `n_down`: the gene sets
  significant by the rule the module replication uses (q_param when the residual df is at least 30, else q_perm, below
  0.05), split by the sign of `std_effect`. Null when only the run manifest has the counts. Used by the dataset page
  Facet > GS matrix.
- `GET /facets/{facet}/donor-values` and `/facets/{facet}/donor-plot.{fmt}` take `db_id`: only the units of that dataset,
  counted or not, and only the exact `cell_type` stratum when one matches (404 when the dataset has no unit). The dataset
  page uses it for the per donor plot of one gene or gene set in one cell type.

New endpoint:

- `GET /databases/{id}/de/{study_id}/summary` (`padj_max`, default 0.05): the study metadata of `/de/{study_id}` plus
  `subset_filter`, `cluster_obs_column` (null on schema v1 duckdbs), `padj_max` and `contrasts: [{term, contrast,
  n_genes, n_sig, n_up, n_down, efftype}]`: significant at padj < padj_max, split by the sign of `effsize`. One query
  per study; used by the dataset page "Other DE" matrices.
- Every gene card replication entry (`/facets/{facet}/effects`, `/genes/{gene}/facet-effects`) and every module card
  replication entry and variant (`/genesets/{study}/{experiment}/{name}/facet-effects`) adds `links`: `{donor_plot,
  donor_plot_svg, donor_plot_adjusted, donor_values, donor_values_available (all counted anchors have stored values),
  donor_plot_label, anchors}`.
  The URLs are relative to the API root and carry the gene or module, `scope` and `cell_type` of the cell (a module link also
  `method` and `source`). `anchors` (genes only) is, per counted anchor dataset, `{db_id, cohort, cell_type, url, label:
  "per-cell, descriptive"}` with the existing `/databases/{db_id}/plot/violin` (two-group facets) or `/plot/scatter` with a
  regression line (numeric facets) of the gene by the facet within that cell type; omitted for datasets without the facet column
  and for derived strata.
- Gene cards (`/facets/{facet}/effects`, `/genes/{gene}/facet-effects`): every replication entry adds `forest`
  (one row per cohort: `{cohort, cohort_label, cohort_display, counted, in_m, small, anchor, more_units, n_more_units}`;
  `anchor` is a slim row `{cohort_label, db_id, title, cell_type, celltype_source, pooled, anchor_counted, n_donors,
  n_donors_ref, n_donors_test, small_sample, logFC, ci_low, ci_high (limma, 5 decimals), p_value, q_value, lambda_gc,
  inflated, analysis_level, replicate_unit, modality, modality_note, not_counted_reason}` and `more_units` holds folded
  units as `{db_id, title, cell_type, celltype_source, anchor_counted, n_donors, logFC, ci_low, ci_high, q_value,
  modality, not_counted_reason}`; counted cohorts first, the rest (secondary datasets, datasets outside the cohort table,
  shown as `unassigned`) after; the counted rows are the cohorts behind `m`), the calls `call` (`replicated` |
  `not_replicated` | `not_tested`), `nt_label` (`1 cohort` or `not tested`), `gate`, `strict`, `k_over_m`, `cohorts_label`
  (`3/4 cohorts, 1 small`), `small_cohorts`, `detection` (`{pct_median, n_datasets, low_detection}` or null) with
  `low_detection`, and `gene_flags` (the card's flags plus `foreign_lineage_marker` where the gene is a marker of a
  non-microglial panel and the group is myeloid; the card's own `gene_flags` add `no_mouse_ortholog` for a xenograft
  scope). Both context flags are informational. `foreign_lineage_marker` needs more than panel membership: the gene must be detected in under 10 percent of microglia (median over the scope's microglia datasets) and at least 5 times more often in a non-myeloid cell type of that scope (`gene_classes.foreign_by_detection`); without detection data it is not raised. New `forest` query parameter (`full` | `anchors` | `none`): `/facets/{facet}/effects`
  defaults to `full`, `/genes/{gene}/facet-effects` (the all-facets payload) to `anchors` (folded units left out,
  `n_more_units` kept). `anchors` rows keep the 2.44 fields and add `ci_low`, `ci_high`, `lambda_gc`, `inflated`,
  `small_sample`, `modality`. Unit cards add `anchor_counted`, `cohort_display`, `modality`, `modality_note`,
  `small_sample`. The word "small" is not a call.
- `GET /facets/{facet}/effects/top`: `n` has no practical cap (was 500; up to 100000), new `offset`, `effect_gate`
  and `strict` filters (rows also carry `gene_flags` with the context flags); the response adds `headline` (`n_replicated`, `n_effect_gate`, `n_strict`, `strict_genes`,
  `n_genes_in_family`, `cohorts`, `effect_gate_unit`), `offset`, `n_matching`, `n_returned`, `has_more`; rows add
  `n_small`, `small_cohorts`, `detection`, `low_detection`; every entry of `cell_types` adds `n_genes_in_family`,
  `n_replicated`, `n_effect_gate`, `n_strict`.
- `GET /facets/{facet}/tests` rows add `modality`, `modality_note`, `cohort_display`; `GET /gene_stats_all` entries add
  `modality`, `modality_note`, `suspension_type`, `technology`, `facet_scope`, `cell_type`.
- Lambda per unit (`lambda_gc`, `inflated` above 1.3) was already served on unit cards; it is now also on every forest
  row and anchor.
- MCP: new tool `facet_donor_values` (one number per donor per counted cohort, the model line and the plot link).
- Help: new topic `donor-plots` (child of `facet-effects`).
- MCP: new tool `donor_variability` (one gene or gene set across cohorts, or the cross-cohort top list; exploratory).
- Help: new topic `donor-variability` (its own section of the help page).
- MCP: `facet_effects` prints the call, the forest with intervals, inflation, modality and the low-detection warning;
  `facet_top_genes` takes `offset`, `effect_gate`, `strict` and has no row cap; new tools `module_facet_effects` (one
  gene set across facets, with member-gene counts) and `facet_top_modules`.
- MCP (sync with 2.45, 2026-10-13): new tool `facet_overview` (`/facets/{facet}/effects/overview`); new tools for the
  DE stored with a dataset, labelled author or custom DE and never replication: `list_de_studies` (one dataset or every
  dataset), `de_study` (`/de/{study}/summary`, or the rows of one contrast) and `gene_de` (`/de/by-gene/{gene}`); each
  study carries its analysis level from the method name (the web UI's rule), and cell-level studies are marked
  descriptive and pseudoreplicated. `donor_variability` takes `db_id` (`/databases/{id}/donor-variability`),
  `facet_donor_values` takes `db_id` and prints an absolute donor plot link (`mcp.public_url` + `/api`), `get_facet_tests`
  prints the gene `n_up` / `n_down` and the module `n_q05` / `n_up` / `n_down`, `get_geneset` takes `db_id` for the
  rank among gene sets (`/genesets/.../rank`), and `list_module_scores` takes `with_ranks` (`/databases/{id}/module-ranks`;
  it also lists scores held only in the scores sidecar, which `/module_scores` misses). `plot` calls the per-geom routes
  `/databases/{id}/plot/{geom}` instead of the deprecated `/plot?geom=` (same figures). Tool texts, the server
  instructions and `anthive://guide` say when to call `explain()` (facets, replication, q vs adj. p vs r, per cell vs
  per donor).
- Help: new topics `module-replication`, `facet-low-detection`, `facet-modality` (children of `facet-counting`), glossary
  terms `forest-plot`, `low-detection`, `modality`; `facet-replication`, `facet-effect-size` and `facet-technical-genes`
  describe the forest rows, the marks and the pill.
- Caches: module tables and families are built single flight like the gene tables (`FAMILY_CACHE_ENTRIES` bound) and
  warmed at start together with the detection table.

Module statistics fixes (2026-10-09, additive fields; module replication values change on the next module run):

- `/facets/{facet}/modules/top` and the module cards of `/genesets/{study}/{experiment}/{name}/facet-effects`: the
  effect gate is per facet (0.2 SD per contrast, 0.2/3 per Braak stage, 0.2 per decade, 0.1 per e4 allele) and is
  read on the lower 95 percent confidence bound of the standardised effect; `effect_gate_tau` and `effect_gate_unit`
  follow the facet, `effect_gate_rule: {std_effect, wording}` on the top list and the module card,
  `effect_gate_rule.per_facet` on the gene set card (named `effect_gate` before 2026-10-13, see "Renames").
- Cohort p values for module replication are the parametric p when the unit has at least 30 residual df, else the
  permutation p. New on every forest point: `df_resid`, `p_used`, `p_basis` (`parametric` | `permutation`),
  `p_disagree`, `p_check_flag`, `p_check_fraction`, `n_ref`, `n_test`, `per_region_only`; `small_sample` is true when
  the smaller level of a two-group facet has fewer than 10 replicates (trends: fewer than 20).
- New on every module entry (variant, top row): `n_cohorts_tested`, `p_check_cohorts`, `p_check_flag`,
  `per_region_only_cohorts`, `module_scope_note`; `agreement` is the share of tested cohorts pointing the replicated
  way (null without a direction).
- Directionless sets (GWAS, MAGMA, bidirectional) are left out of the module families, replicated and top lists; their
  cards carry `directionless: true` and variants with `call: not_tested`, `nt_label: no expected direction`.
- Web: `/facets/{bare name}` (for example `/facets/braak`) redirects to `/facets/ant_braak`.
- Clients: pyvark, vark and the skills reach none of the new routes yet; pass-through clients keep working with
  the new optional fields. pyvark's `get_facet_recurring_genes` documents `n` up to 500 and has no `offset`,
  `effect_gate`, `strict` or `forest`. The breaking changes above (internal datasets for anonymous callers, admin-only
  rescans, the `apoe_carrier` alias) are the ones a client may notice.

## 2.44.0 (2026-10-07)

Release summary. Facet engine v3: facet scopes, ordinary-p partial-conjunction replication with an effect filter
and a strong tier, robust limma, floors, the facets `age`, `age_adjusted`, `app_nlgf`, `braak_threshold` and
`apoe4_dose`, donor exclusions, `genotype` / `host_genotype`, a tissue umbrella and per-cell-type author clusters.
A plain-language help API (`/api/help`, the MCP `explain` tool) and much faster facet endpoints. Operations:
`data.extra_mounts`, hidden unresolved paper links, library fixes, a cron `PATH` example, `ant sync` also syncs
`_facet_tests`, and the new `ant-restore-obs`. The web UI gains a dataset Facets tab, collapsible docs and a
glossary. Details below.

Minor bump. Additive (new `scope` parameter, new fields, two new facets), with these changes to existing
answers:

- **Replication uses the ordinary one-sided limma p value and an effect gate** (facet engine v3, 2026-10-06; the gate became a separate field in v3b, below).
  `replicated` is now `r_value < 0.05` AND `effect_gate_passed`; `r_value` and `p_pc` are computed from the
  ordinary p values (the treat p values are still served as `p_treat` / `q_treat`). On the 2026-10-05 results
  Braak microglia goes from 2 replicated genes to 34 (all pass the gate). `k` now counts cohorts with
  `q_value < 0.05` in the replicated direction; `k_treat` keeps the treat count. Clients that read `replicated`
  or `r_value` get different numbers; field names are unchanged.
- **Facets are tested, counted and replicated per facet scope** (`human_postmortem`, `mouse_in_vivo`,
  `xenograft_human_in_mouse`, `in_vitro`): human and mouse (and xenograft) never mix. The default answer of every
  single-answer endpoint is `human_postmortem` (for `app_nlgf`, which is not allowed there, `mouse_in_vivo`), so
  existing clients see what they saw.
- **Small cohorts are tested and counted.** Units with a level below 5 replicates (numeric facets below 10) are no
  longer `descriptive_only`: they carry `flags.small_sample` / `n_lt5` and take part in replication. Eligibility is
  3 replicates per level (4 replicates with 3 distinct values for Braak and age); datasets need at least 4
  replicates (was 6).

New:

- `scope` query parameter (`human_postmortem` | `mouse_in_vivo` | `xenograft_human_in_mouse` | `in_vitro`; an unknown
  name is a 400) on `GET /facets/{facet}/tests` (also `all`), `GET /facets/{facet}/effects`,
  `GET /facets/{facet}/effects/top`, `GET /genes/{gene}/facet-effects`, `GET /facets/{facet}` (datasets, cell
  counts, value distribution, per-cell module statistics and non-carriers of that scope) and
  `GET /module-variability` (rows of the datasets of that scope). Responses carry `scope` and `scopes` (the scopes
  with results; `GET /facets/{facet}` lists `{scope, n_datasets, n_cells}`; `GET /facets` rows carry `scopes`).
  `GET /genes/{gene}/facet-effects` without `scope` adds `by_scope` (`{scope: {facet: card}}`) so a gene page shows
  scopes separately; `facets` keeps one card per facet in its default scope. A gene symbol is never matched across scopes.
- Dataset metadata (`/databases`, info) and `/databases/{id}/facet-tests` carry `facet_scope`; every unit and gene
  or module row does too. The metadata cache version is 6.
- Tested facets `age` (human post-mortem; log2 fold change per decade of age, model `~ age + sex + pmi [+ batch]`,
  age effects include disease prevalence) and `app_nlgf` (`nlgf` vs `control`; mouse in vivo and xenograft hosts;
  replicate = the animal). `GET /facets/age|app_nlgf/...` work like the other facets; `facets_visible` defaults to
  include `ant_age` and `ant_app_nlgf`. `app_nlgf` is a registered facet (`controlled_values` nlgf, control).
- Replication entries (`/facets/{facet}/effects`, `/genes/{gene}/facet-effects`) and rows of `/effects/top` add
  `effect_gate_passed`, `median_logFC`, `effect_gate_tau`, `r_significant` (r < 0.05 alone), `k_treat`,
  `replication_p` (`p_value`); the badge states the median effect and the gate. `/effects/top` adds
  `n_r_significant` (genes with r < 0.05 before the gate) and lists only genes that pass the gate. Anchors add
  `p_value`, `q_value`; unit cards add `p_value` and `facet_scope`.
- Gene rows add `small_sample` and `facet_scope`; `flags` of a unit add `small_sample`.
- Contrasts: `age_per_decade` (age), `nlgf_vs_control` (app_nlgf).
- MCP: `facet_effects`, `facet_top_genes` and `facet_units` take `scope`; the tool texts describe the effect gate, the
  scopes and the new facets; `facet_effects` without `facet` asks all six.
- Registry: `facet_scopes` per facet in `lib/obscol_tags_vocab.yaml` (`ant md-check` error `facet_scope_mismatch`).

Facet engine v3b (same unreleased version, 2026-10-06, after the second-opinion review):

- **The effect gate is no longer part of `replicated`.** `replicated` is r < 0.05 (direction-consistent effect in at least 2
  cohorts). `effect_gate_passed` now means at least 2 cohorts each with a signed log2FC of at least tau in the replicated
  direction (not a median); new `effect_gate_cohorts`, `n_cohorts_ge_tau`, `r_cohorts` (the two cohorts that carry the
  r-value), `r_treat` and `replicated_effect` (second tier: the partial conjunction on the treat p values, replicated with at
  least a 10 percent change), `effect_gate_unit` ("10%", "10% over three stages", "10% per decade"). `/effects/top` adds
  `n_effect_gate`, `n_replicated_effect`, `r_cohorts`, `r_treat`, `replicated_effect`, `effect_gate_cohorts`, `gene_flags`
  per row. The badge reads "direction replicates (r = ...); effect below 10% in fewer than 2 cohorts" when the gate fails.
- New tested facet `age_adjusted` (age with the pathology term, Braak else the 2x2 disease); `age` is labelled "not adjusted
  for pathology" and its gene rows carry `persists_after_pathology_adjustment`. The age facets exclude donors under 60 (a
  dataset's `facet_min_age.age` wins). `FACET_ORDER` and `/genes/{g}/facet-effects` have 7 facets; `strongest_facet` names the
  facet a gene page opens on.
- Units add `lambda_gc`, `inflated` (lambda > 1.3), `df_total_median`, `covariates_dropped`, `n_sex_checked`,
  `n_sex_discordant`, `age_top_coded`, `control_basis`, `design_subset`, `notes`. Cards add `gene_flags`. `app_nlgf` units are
  stratified by timepoint and graft line (the `celltype_source` carries `@timepoint=...|line=...`), adjust for declared
  covariates and are refused when confounded. Dataset metadata adds `control_basis` (metadata cache version 7).
- limma: `voomWithQualityWeights` and `eBayes(robust = TRUE)`; the residual-df floor for covariates is 4. Tool version
  `ant-facet-test/3.1`.

Facet engine v3c (same unreleased version, 2026-10-07):

- **`mouse_in_vivo` is retired.** The scopes are `human_postmortem`, `xenograft_human_in_mouse`, `in_vitro`; pure mouse
  datasets have no facet layer. `scope=mouse_in_vivo` is a 400. `app_nlgf` is a xenograft facet only (its default scope is
  `xenograft_human_in_mouse`).
- Units add `analysis_level` `below_floor` (descriptive) next to `replicate` and `cell`; rows and units add `n_small`;
  `r_strong` / `replicated_strong` (tau 25 percent) next to `r_treat`; unit QC `lambda_gc`, `inflated`, `df_total_median`,
  `notes`; new tested facets `braak_threshold`, `apoe4_dose` (registered under `braak` and `apoe4_carrier`).
- Registry: `tissue` is a coarse facet with controlled values (21 group terms, `human_postmortem` only); `region`,
  `author_cell_type` and the generic `author_cluster` are deprecated, replaced by `mg_author_cluster`, `astro_author_cluster`,
  `oligo_author_cluster`, `opc_author_cluster`, `exn_author_cluster`, `inn_author_cluster`, `endo_author_cluster` (dataset-scoped,
  `dataset_scoped` in the registry entry, no gene tests); `host_genotype` is new; `lesion`, `ms_type`, `cause_of_death`, `ethnicity`,
  `source_institution`, `technology`, `group` are deprecated entries. `ant_tissue` values change (umbrella terms); the
  specific term is the plain column `tissue_uberon` (+ `tissue_uberon_id`). `ant_cell_type` is NULL for cells the authors left
  unassigned or called doublets.
- Datasets excluded from the facet layer (spatial, proteomics, metacell sets, Francken2026) have no results and no cohort rows.
- The metadata cache version is 7 (8 with the facet API fixes below).

Facet API fixes (same unreleased version, 2026-10-07, found by the vark client on the live API):

- **Performance.** `GET /genes/{gene}/facet-effects` without `scope` took over 170 s and nine parallel per-facet calls stalled the
  server: the caches (4 gene tables, 8 replication families) were smaller than the facets x scopes of 2.44, so every call
  recomputed them. Now: the gene tables of all facets and scopes are built in one pass over the result files and kept together;
  one replication family per (facet, scope, exclude_secondary) is kept (54 slots); a gene card is cached per (facet, scope, gene)
  until a result file changes. Builds are single flight (concurrent callers share one build, no global lock is held while it
  runs) and, while a rebuild after a file change runs, other callers get the previous tables. ant-serve builds the tables and
  families at start in a low priority thread (`ANTHIVE_FACET_WARMUP=0` turns that off). On a loaded 8 core slice: first call after
  a start without warm-up 22 s (was 38 to 88 s), all scopes of a gene 41 s from cold (was over 170 s), 0.6 s warm for a repeated
  gene and 1.0 to 1.6 s for a new gene, nine parallel per-facet calls on a new gene 4 s with other endpoints answering in under
  2 s (was a stall of over 100 s).
- `GET /genes/{gene}/facet-effects` adds `facet` (comma separated facets to answer; default all; an untested facet is a 404) and
  `include_units` (default true; false keeps `n_units`, `replication`, `cell_types` and `notes` of every card but empties
  `units`: 0.5 MB instead of 5 MB for PTPRG). `facets` is taken from `by_scope` instead of being computed twice.
- `facet_exclude` (bool) and `facet_exclude_reason` are served on `/databases`, `/databases/{id}/info` and
  `/databases/{id}/facet-tests`. They follow `lib/facet_scopes.facet_exclusion_reason`: the `.md` key `facet_exclude: true` with
  its reason, a spatial or proteomics technology, `experimental_system: spatial`. The metadata cache version is 8.
- The not-tested payload of `/databases/{id}/facet-tests` takes `facet_scope` from the dataset (it reported
  `human_postmortem` for datasets without a run manifest, such as the xenograft AZ5/NST; None for a dataset in no scope) and its
  `reason` from today's rules (exclusion reason, else `no facet scope: ...` for a dataset in no scope) instead of a stale run log
  or manifest line.
- `/facets/{facet}/effects/top` rows add `r_strong`, `replicated_strong` (the 25 percent tier; null when the run has no strong
  p values) and, for facet `age`, `persists_after_pathology_adjustment` (the gene also replicates, same direction, in
  `age_adjusted` of the same scope and cell type group; null for other facets or when `age_adjusted` has no such group); the
  response adds `n_replicated_strong`.
- Unit cards of `/facets/{facet}/effects` and `/genes/{gene}/facet-effects` add `replicate_unit` and `replicate_column`
  (from the unit, else the run).
- Unit `notes` no longer say "small sample: tested and counted" for a unit that is not counted: a small sample below the
  p-value floor reads "below the p-value floor of its replicate unit, descriptive only, not counted", any other small sample
  "small sample: low power".

Library changes in 2.44.0 (no new endpoints):

- **Papers a site does not hold are hidden.** A `papers:` link (dataset, collection, gene set) to a citekey that is not in
  this site's own library is dropped from `/databases/{id}/papers`, `/databases/{id}/context`, `/papers/{citekey}`
  (`datasets`, `genesets`), `/papers` and BibTeX; a paper's `related:` entries to citekeys the library lacks are not shown
  either (the raw `related` field, `/library.bib`).
- **`GET /library/status` is trimmed for non-admins.** `unresolved_references` (the dropped `papers:` links),
  `issues.errors` / `issues.warnings` texts, `library_dir` and `index.path` are given to admins only (on `auth.mode: none`
  nobody is an admin); everybody gets `papers`, `linked_datasets`, `index.*` and the new `n_errors` / `n_warnings`. Clients
  that read `unresolved_references` as a non-admin now find the key missing; `issues` keeps its shape with empty lists.
- **PDF warnings are live.** "`<citekey>`: file ... not found next to the sidecar" is computed from the disk at each
  `/library/status` call, so it no longer outlives the PDF's arrival (it used to survive a reload and `POST /admin/rescan`).
- Plain (non-git) libraries are re-read when record files are added, removed or replaced (a fingerprint replaces the git HEAD
  check); a record symlink with a missing target is an `issues.errors` entry instead of a failed load.

Plain-language help (same unreleased version, 2026-10-07; additive):

- **New `help` endpoints** serve the maintainers' own explanations of anthive concepts, the same texts the web help page
  shows (both read `lib/help_content/`, so they cannot drift). Public like `/version` and `/skill`; responses are cacheable
  (`Cache-Control: public, max-age=3600`, `ETag`, `If-None-Match` answers 304):
  - `GET /help`: `{topics: [{id, title, summary}], glossary: [{slug, term}]}`.
  - `GET /help/{topic}`: `{id, title, summary, body (markdown), children, related, terms, anchor}`. `body` includes the
    subsection topics listed in `children` unless `include_children=false`; `related` and `terms` (glossary slugs) include
    the children's. An unknown id is a 404 whose `detail` lists close ids.
  - `GET /help/glossary` (`?format=markdown|html`, default markdown): `{format, terms: [{slug, term, definition}]}`;
    `GET /help/glossary/{term}`: one term (`replicate-unit`, `r-value`, ...), 404 with close slugs.
  - `GET /help/search?q=`: fuzzy match over topic ids, titles, aliases and glossary terms,
    `{query, matches: [{kind: topic|term, id, title, score}]}`, best first (empty list when nothing is close).
  - Topic ids: `what`, `finding`, `pages` (children `collection-page`, `dataset-page`, `facets-columns`, `dataset-versions`,
    `dataset-caveats`, `differential-expression`), `plots`, `genes` (child `module-scores`), `per-cell-per-donor`,
    `facet-effects` (children `facet-tested`, `facet-replicate-unit`, `facet-replicates-needed`, `facet-contrasts`, `facet-scopes`, `facet-excluded`,
    `donor-groups`), `facet-counting` (children `facet-replication`, `facet-effect-size`, `facet-not-replicated`,
    `facet-cell-type-groups`, `facet-pathology-axis`, `facet-age`, `facet-app-nlgf`, `facet-whole-cell-type`,
    `facet-technical-genes`, `facet-limits`), `facet-howto`, `papers` (child `library-search`). The ids are also the anchors of
    the help page (`/help#<id>`, `/help#glossary-<slug>`).
- **`help_topics`** (list of topic ids) is added to the responses of `GET /facets/{facet}/tests`, `/facets/{facet}/effects`,
  `/facets/{facet}/effects/top`, `GET /genes/{gene}/facet-effects` and `GET /databases/{id}/facet-tests` (`facet-effects`,
  `facet-counting`, `facet-replicate-unit`, `facet-howto`, `facet-scopes`, `donor-groups`) and `GET /databases/{id}/context`
  (`dataset-caveats`, `facet-replicate-unit`, `per-cell-per-donor`, `dataset-versions`). Clients may show or fetch them; ignoring
  the key is safe.
- **`GET /skill`** now serves a short bootstrap skill (sign-in, discovery, the rule to read `/help` before explaining
  concepts, the facet and literature workflow) that points at `/openapi.json`, `/help` and the public skills repository,
  instead of a copy of the endpoint reference that went stale.
- MCP: new tool `explain(topic_or_term)` (fuzzy match on topic ids and glossary terms; the topic list when nothing matches),
  resources `anthive://help` and `anthive://help/{topic}`, and an instructions block telling clients to call `explain` before
  explaining facets, replication, scopes, caveats and similar concepts and to quote it instead of improvising.

Clients to update: pyvark (`scope` on the facet calls, the new replication fields, `by_scope`, `get_help` / `explain`), vark (`help` command), the agent
skills (facet and replication texts, call `/api/help` or MCP `explain` instead of improvising), the JupyterLite vendored client and the web UI (scope selector, effect gate,
`small_sample` instead of descriptive-only for small cohorts, age and app_nlgf facet pages).

## 2.43.0 (2026-10-05)

Minor bump. Mostly additive (new dataset, replication and facet-test fields), with
three contract changes to be aware of:

- `/facets/{facet}/effects/top` rows no longer carry `units_up`, `units_down` and
  `max_abs_logFC`. Use `k`, `m`, `r_direction` and `median_abs_logFC`.
- The replication `badge` text changed ("k of m" wording); the old text is `legacy_badge`.
- The facet `apoe_carrier` is now `apoe4_carrier`; the old name stays as an alias.

pyvark 0.9.0 and the vendored JupyterLite client cover all of this.

- **Facet test polish** (2026-10-05, additive). `disease_basis` on facet-test summaries, units,
  effect cards, anchors and overview rows now follows the dataset `.md`; the value recorded in
  an old run manifest only applies when the `.md` declares none (donor-pooled units take the basis
  their members agree on). `GET /facets/{facet}/tests` rows carry `not_counted_reason` (same rules
  and wording as the effect cards). Cohort `role` is public text (dated curation notes in
  parentheses are stripped, or the `role_public` column of the cohort table when present);
  `/facets/cohorts` and the summaries never serve the raw table text. `GET /facets/{name}` and
  `GET /genes/{gene}/facet-effects` are cached (per-dataset value counts keyed on the .duckdb
  fingerprint; gene tables and replication families no longer evict each other), warm calls take
  well under a second.

- **Facet replication v2: partial-conjunction r-values, treat p values, one anchor per cohort**
  (2026-10-05). `/facets/{facet}/effects`, `/genes/{gene}/facet-effects` (replication
  entries): new `r_value` (Benjamini and Heller partial conjunction, u = 2, Simes combination,
  BH over the genes of one facet x cell type group; replicated = r < 0.05), `p_pc`, `k`
  (cohorts with q_treat < 0.05 in the replicated direction), `m` (cohorts with a treat
  p value), `replicated`, `r_direction`, `anchors` (one per counted cohort: `cohort`,
  `db_id`, `pooled`, `logFC`, `p_treat`, `q_treat`), `overlap_unknown`,
  `cohorts_overlap_unknown`, `replication_u`. `badge` is the new text ("replicated in
  at least 2 of m cohorts (up): r = ...; k of m cohorts individually q < 0.05"); the
  old text is `legacy_badge`. The old fields (`cohorts_tested`, `cohorts_sig_same_sign`,
  `direction`, `cohorts_up`, `cohorts_down`, `sign_discordant`) keep their meaning
  (q_value < 0.05 on the anchor units) for one release. Unit cards add `anchor`,
  `pooled`, `p_treat`, `q_treat`, `tau_log2`, `significant_treat`, `analysis_level`,
  `independence`, `disease_basis`. Counting changed: exactly one anchor unit per
  (cohort, cell type group); a donor-pooled unit (`db_id` `pool:<id>`, MADRC and SEA-AD
  regional files pooled per donor) beats single files; other units are listed with
  `counted: false` and a `not_counted_reason`. The disease facet only combines
  pathology-based definitions (`disease_basis`). `/facets/{facet}/effects/top` now lists
  only genes with r < 0.05, ranked by `r_value`, then `k`, `agreement`,
  `median_abs_logFC`; rows add `r_value`, `p_pc`, `k`, `m`, `replicated`,
  `cohorts_tested_labels`, `overlap_unknown`, `cohorts_overlap_unknown`; the response adds
  `n_replicated` and `n_genes_in_family`; the old row fields `units_up`, `units_down`,
  `max_abs_logFC` are gone. Cohort rows of `/facets/cohorts` add `independence`
  (`checked` or `unknown`), `overlap_unknown` and `pool_members`.
- **`replicate_unit` / `replicate_column` per dataset** (2026-10-05). New `.md` catalog keys
  (`replicate_unit`: donor, animal, sample, none; `replicate_column`: the obs column that
  identifies a replicate), served on `/databases` and the dataset detail as
  `replicate_unit`, `replicate_column`, `replicate_basis` (`declared` or `inferred`) and
  `disease_basis`. `/databases/{id}/facet-tests` summary and units add `replicate_unit`,
  `replicate_column`, `replicate_basis`, `disease_basis`, `pool`, and per unit
  `analysis_level` (`replicate` or `cell`), `analysis_level_label` ("replicate level" or
  "cell-level, descriptive only"), `analysis_level_reason`, `tau_log2`. Gene rows add
  `p_treat`, `q_treat`, `tau_log2`; module rows add `passes_effect_gate`
  (|std_effect| >= 0.2). `/facets/{facet}/tests` rows add the same unit fields,
  `disease_basis`, `pooled`, `independence`, `overlap_unknown`.
- **Per-cell numbers are marked** (2026-10-05). `/module-variability` (`basis`,
  `X-Basis` for CSV, and a `replicate_unit` column in the rows) and
  `/databases/{id}/gene-variance-by-facet` (`basis`, `replicate_unit`,
  `replicate_level_analysis`) state `per-cell (descriptive)`: cells are not replicates.
- **Facet `apoe_carrier` renamed `apoe4_carrier`** (2026-10-05). Same
  definition and levels (E4+ / E4-, test contrast `e4_carrier_vs_noncarrier`).
  The column is `ant_apoe4_carrier`, the facet-test facet `apoe4_carrier`,
  `facets_visible` defaults to it. Deprecated aliases, removed after one
  release: the obs views carry an `ant_apoe_carrier` alias column (not
  listed as a facet), `/facets/ant_apoe_carrier` and
  `/facets/apoe_carrier/...`, the `facet=apoe_carrier` parameters and the
  `facets_visible` entry still work and return the new name; the web page
  `/facets/ant_apoe_carrier` redirects. Facet-test results written before
  the rename are read under the new name. Clients should switch names.

- **Donor exclusions in facet tests** (2026-10-05, `doc/DESIGN_facet_testing.md`).
  - `GET /databases/{id}/facet-tests` gains `donor_exclusion` (`genes` and
    `modules`: the `donor_exclusion` block of the run manifests or null) and
    `supplementary_all_donors` (`counted` false, `n_donors`, `n_cells`,
    `timestamp_utc`, `path` of the full-donor run, null when none). The
    results of that run are not served. `cohort` gains `donor_exclusion_required`,
    `donor_exclusion_applied` and `exclusion_missing`; a unit of a dataset whose
    cohort table row requires an exclusion that its manifest does not record is
    not counted (`not_counted_reason`).
  - Each unit `model.donors` gains `exclusion` (a sentence) when a rule was applied.

- **`ant_donor_uid` from a template or a crosswalk join; distinct donor
  counts** (2026-10-05, `doc/DESIGN_canonical_columns.md`).
  - `.md` `api.obs_derive` accepts two new categorical forms:
    `{source, template: '{group_id}:{value}'}` and `{source, join:
    donor_crosswalk.tsv, cohort, fallback_template}`. The served
    `ant_donor_uid` values do not change shape (`<cohort>:<id>`, or
    `<group_id>:<donor>` for the template).
  - `GET /databases/{id}/facets` and `GET /facets/{name}`: `mapping_kind`
    can be `template` or `join`. Such entries add `template`, or
    `join_table`, `cohort`, `fallback_template`, `n_crosswalk_aliases`
    and `join_problem`, plus `group_id`; with counts, value rows carry the
    rules `template`, `crosswalk`, `fallback`, `unmatched` or `null` and the
    entry has `rule_counts`.
  - `GET /donor-groups`: new `donor_counts` parameter (default true). Each
    group gains `donors` (`n_donors`, `n_donors_uid`, `n_donors_fallback`,
    `n_donors_primary`, `n_datasets_counted`, `n_datasets_without_donor`,
    `basis`) and each dataset `n_donors` / `n_donors_uid`. Donors are keyed
    on `ant_donor_uid`, else `<group_id>:<ant_donor>`.
  - `/facets/{name}/effects` replication entries and
    `/facets/{name}/effects/top` rows gain `n_donors_sum` and
    `n_donors_distinct` (null for results from runs before 2026-10-05);
    the top-genes reply gains `donors_in_cell_type`, and both gain
    `donor_count_definition`.

- **Donor-level facet test results.** Four read-only endpoints over the
  output of `ant-facet-test` and `ant-facet-test-modules`
  (`<h5adstore>/_facet_tests/<group>/`), visible to everyone who can see
  the dataset. All of it is exploratory; see `doc/DESIGN_facet_testing.md`.
  - `GET /databases/{id}/facet-tests` gives one summary row per (facet,
    cell type stratum): `contrast`, `level_ref` / `level_test`,
    `level_counts` (donors per level), `n_donors(_ref/_test)`,
    `covariates`, `flags` (`descriptive_only`, `n_lt5`, `provisional`,
    `composition_flag`, `composition_p`, `batch_used`,
    `batch_confounded`), `genes` (`n_tested`, `n_q05`) and `modules` (per
    method and source: `n_modules`, `n_q05_param`, `n_q05_perm`). Untested
    units carry `reason`. Top level: `status` (`tested`, `not_tested`,
    `not_run`), `reason`, `dataset_changed` (the dataset's sha256 differs
    from the one the run used), `files` and `runs` (tool, timestamp, seed,
    package versions, git commits, counts layer, batch column, permutations).
  - `GET /databases/{id}/facet-tests/genes` with `facet`, `cell_type`,
    `celltype_source`, `gene`, `q` (default 0.05; `q=1` keeps every row),
    `sort` (`q`, `effect`, `p`, `gene`), `limit` (max 2000 as JSON) and
    `format` (`json` or `csv`; CSV allows up to 100000 rows).
  - `GET /databases/{id}/facet-tests/modules` with the same unit filters
    plus `method`, `source`, `module`, `q_by` (`param`, `perm`, `either`;
    default `either`) and `sort` (`q`, `q_perm`, `effect`, `module`).
  - `GET /databases/{id}/facet-tests/volcano.png?facet=&cell_type=` draws a
    small volcano plot of one unit's gene results (404 for descriptive-only
    units).
  - Every summary unit carries `model`, built from the run manifests:
    `stratum` (cell type and how the stratum was defined), `facet_term`,
    `donors` (per level, analysed, left out for a missing covariate, minimum
    cells per donor), `descriptive_only_rule`, and per run (`genes`,
    `modules`) the `unit_of_analysis`, `formula` (for example
    `expression ~ braak + sex + age_z + pmi_z`), `covariates` (each with
    `status` included, dropped or not used and the `reason`), `method`,
    `multiple_testing`, `tool`, `seed`, `run_date`, `versions`. Anything a
    manifest does not record reads `not recorded`.
  - `GET /facets/{name}/tests` (`ant_sex` or `sex`, also disease,
    apoe_carrier, braak; `top_genes`, default 5) lists every tested
    (dataset, stratum) of one facet with the unit fields above plus
    `db_id`, `title`, `group`, `top_genes` and `run_date`, sorted by donors
    analysed. `not_tested` lists datasets with results whose units for this
    facet were not testable. No pooling across datasets.
- **Facets hidden by configuration.** A new config key `facets_visible`
  (default `ant_sex`, `ant_disease`, `ant_apoe_carrier`, `ant_braak`; an
  empty list shows all) decides which facets the web UI shows. The API
  still lists every facet: `GET /facets` rows, `GET /facets/{name}` and
  `GET /databases/{id}/facets` rows gain `hidden_by_config`, and `GET
  /facets` gains `facets_visible`.

  Files are cached by mtime, so a rerun shows up on the next request. A
  missing or partly written file counts as not available (404 for rows,
  `files.genes: false` in the summary).

- **Facet definitions.** `GET /facets` rows gain `label`, `summary` and
  `defined`, and the response gains `definitions_status` (`state`,
  `note`). `GET /facets/{name}` gains `definition` (`label`, `summary`,
  `definition`, `kind`, `values`, `missing`, `hint_keywords`, `defined`)
  and `definitions_status`. The text lives in the `facet:` blocks of
  `lib/obscol_tags_vocab.yaml`. An audit of the facets is running, so
  `definitions_status.state` is `under_audit` and the wording may change.
- **How datasets project onto a facet.** With `mapping=true` (the default)
  every `GET /facets/{name}` `datasets[]` row also carries
  `mapping_kind` (`renamed`, `derived`, `constant` or `native`),
  `source_columns`, `sources` (`column`, `published_as`, `hidden`),
  `value_map` (rows `from`, `to`, `rule`, `n_cells`; `rule` is `mapped`,
  `hidden`, `passthrough` or `default`), `unmapped`, `default`,
  `constant_value`, `n_missing`, `range` (numerical facets),
  `n_distinct_source_values`, `value_map_truncated` (only the 200 most
  frequent author values are listed) and `redacted`. The new
  `non_carriers` list has every other dataset with `candidate_columns`
  (public columns whose names suggest the facet, for example `Gender` for
  `ant_sex`) and `declared` / `declared_kind` (the `.md` declares the
  facet but the served view lacks it). `mapping=false` skips both.
- **New `GET /databases/{id}/facets`.** The same projection for every
  facet of one dataset, plus `label`, `summary`, `defined`, `n_cells`,
  `hidden_sources_visible` and `definitions_status`. `counts=false` skips
  the cell counts.
- Columns listed in a dataset's `api.obs_hidden` stay private: for
  non-admins a facet built from one has empty `source_columns`,
  `redacted: true` and `value_map` rows without `from` (counts summed per
  facet value). Admins (Authelia `admins`) see the column and its values.
- **Dataset lineage (audit L3).** `GET /databases` rows and `GET
  /databases/{id}/info` gain `designation` (`primary`, `reanalysis` or
  `unresolved`: the `.md` has `is_secondary: false`, `true`, or no key),
  `reanalysis_of` (list of db_ids, empty unless reanalysis), `donor_group`
  (slug or null) and `has_donor_uid` (the dataset publishes
  `ant_donor_uid`).
- **New `GET /donor-groups`** (`?donor_group=` for one group): every donor
  group with `n_datasets`, `n_primary`, `n_reanalysis`, `n_unresolved`,
  `n_cells` (primaries only), `has_donor_uid` and `datasets` (`db_id`,
  `title`, `group`, `designation`, `is_secondary`, `reanalysis_of`,
  `has_donor_uid`, `n_cells`); `n_ungrouped` counts datasets without a
  group. 404 for an unknown group.
- **`exclude_secondary` (default true) on pooled and cross-dataset
  features.** Behaviour change: secondary datasets (`is_secondary: true`,
  reanalyses of cells another dataset serves) no longer enter pooled
  numbers unless `exclude_secondary=false`.
  - `GET /gene_stats_all`: secondaries are left out of `results` and of
    `cross_db_cv`; the response gains `n_excluded_secondary` and
    `exclude_secondary`, each result `is_secondary`.
  - `GET /module-variability`: rows of secondaries are left out unless
    `dataset` names one; JSON gains `n_excluded_secondary`, CSV / TSV the
    header `X-Excluded-Secondary`.
  - `GET /facets/{name}`: secondaries stay in `datasets` with
    `is_secondary`, `counted: false` and `reanalysis_of`, but are left out
    of `total_n_cells`, `value_distribution` and `modules`; the response
    gains `n_excluded_secondary` and `exclude_secondary`. The
    `signal_strength` of `GET /facets` ignores secondaries.
  - Facet test views (`/facets/{name}/tests`, `/effects`, `/effects/top`,
    `/genes/{gene}/facet-effects`): secondaries are listed with
    `is_secondary: true` and `counted: false` and never count towards
    replication.
- **Facet test replication counts by cell type group.** Stratum labels map
  to groups in `lib/celltype_groups.yaml` (`microglial cell`, `Microglia`,
  `MG`, `Mic P2RY12` are all `microglia`). `/facets/{name}/effects` units
  gain `celltype_group`, `not_counted_reason`, `q_counted` and
  `n_sibling_strata`, and `counted` now means "counts towards replication"
  (cohort counted, not descriptive only, not secondary, the dataset's most
  direct cell type source for the group). `replication[]` and
  `cell_types[]` are one entry per group with `celltype_group`, `cl_id` and
  the raw `labels`; their `cell_type` holds the group name. `cell_type=`
  accepts a group name or a raw label. `/facets/{name}/tests` rows gain
  `celltype_group` and `is_secondary`. See `doc/DESIGN_facet_testing.md`.
- **Recurring genes ranking.** `/facets/{name}/effects/top` ranks by k
  (cohorts with q < 0.05 and the same sign), then `agreement` (fraction of
  tested cohorts with that sign, significant or not), then
  `median_abs_logFC`. Rows gain `cohorts_same_sign`, `agreement`,
  `median_abs_logFC`; the response gains `celltype_group`, `cl_id`,
  `labels`, `ranking` (the rule as text) and `exclude_secondary`. Before,
  ties on k were broken by "fewer cohorts tested first", which ranked
  "2 of 2" above "2 of 4".
- **Facet registry.** Facet definitions (`GET /facets/{name}` `definition`)
  gain `status` (`registered` or `deprecated`), `scope` and `replaced_by`.
  New registered facets: `ant_cognitive_status`, `ant_comorbidity`,
  `ant_adnc`, `ant_donor_uid`. `ant_treatment` was registered briefly and
  withdrawn the same day: treatment arms are a plain dataset-local
  `treatment` column, not a facet, and `ant_condition` now names only
  `ant_disease` and `ant_genotype` in `replaced_by`. Deprecated:
  `ant_cluster`, `ant_condition`, `ant_region`, `ant_ad_stage`,
  `ant_author_cell_type` (still served while datasets migrate).
- **Computed facets.** When a dataset publishes `ant_braak` or `ant_apoe`
  but declares no `ant_braak_group` / `ant_apoe_carrier`, rebuilt views
  compute them centrally (Braak 0-2 low, 3-4 medium, 5-6 high; E4+ when
  `ant_apoe` holds an E4 allele). The facet mapping reports them with
  `mapping_kind: computed` and `computed_rule`. Views on disk change only
  after a rebuild.

## 2.42.1 (2026-10-03)

Patch release: no contract change.

- **The server notices a library changed on disk.** When `ant sync
  library` (or the host-side `ant-stack publish-library`) pulls new records
  or replaces `library_chunks.parquet`, the running server reloads them in
  the background on the next library request (checked at most every 30 s).
  Before, a site that downloaded a newer published index, or a publisher
  whose ingest container added papers to it, kept searching the old index
  until a restart or rescan.

## 2.42.0 (2026-10-03)

Minor release. Literature search and onboarding.

- **Getting started.** The help page opens with a quick start (browse,
  connect an AI client, install the skills, ask a first question). The MCP
  page has a "Set up with one paste" prompt, filled in with the user's
  link, for agents that can run commands (Claude Code, Codex, Gemini CLI,
  OpenCode, Cursor): it adds the connection, installs the skills and
  optionally vark, asking before each change. The link is filled in by the
  browser and never appears in the page HTML.
- `GET /me/mcp` gains `connection_name` (for example `anthive-intern`, from
  the site's address); `snippets` and `snippets_template` use it instead of
  the fixed name `anthive`, so people with several Anthive servers get one
  name per site.

- **Hybrid literature search keeps paraphrase matches.** The fusion of the
  keyword and semantic rankings favoured passages found by both, so the
  best semantic match of a query that shares few words with the paper
  could drop out of the results. The two best passages of each ranking are
  now interleaved at the top (semantic rank r at place 2r or better,
  keyword rank r at 2r + 1). Example on the dev library: "cells that eat
  debris do not clear the toxic protein clumps but actually help seed
  them" ranks konstantoulea2026phagocytes first (before: not in the top 10).
- **`GET /library/status` `upload_index` shows the queue.** Uploads in quick
  succession are indexed one after another; the status now switches to a
  paper only when its turn starts, lists waiting citekeys in `queued`, and
  keeps the outcome of the last 20 jobs in `recent` (before, a waiting
  upload overwrote the status of the one being indexed).
- MCP instructions and the `search_literature` description tell AI clients
  to search the library whenever they interpret a result or explain
  biology, and how to phrase queries.
## 2.41.0 (2026-10-03)

Minor release. The library has no per-paper access or LLM rules any more,
the upload requirements say more, and uploads report when they are off.

- **Access follows the site login.** `anthive_access` is gone: everyone who
  can sign in sees every paper, note and PDF. The admin-only filtering of
  `restricted` papers and the hiding of their citekeys in other records'
  `related` lists and exports are removed. The endpoints keep their normal
  auth.
- **All library text may go to AI clients.** `anthive_llm` is gone, with the
  `never`/`summary_only` handling for ant-mcp calls; `for_llm` is no longer a
  query parameter (sending it is harmless, it is ignored). Paper summaries no
  longer carry `access` and `llm`. Clients that read those two keys must
  stop; nothing else in the response shape changed.
- Leftover `anthive_access`/`anthive_llm` in records give a validator
  warning (`anthive_access ignored: access follows the site login`);
  uploads drop them with the same warning.
- **Roles.** `anthive_role` defaults to `background` (of interest to the
  lab, not linked to a dataset) whenever it is not given, also when
  `papers_for` proposes dataset links. Set `source` or `reanalysis` only
  when the data in anthive come from the paper.
- **Document types.** BibLaTeX `entrytype` plus the `type` field (now a
  known field): article, online, book, inbook, incollection, inproceedings,
  thesis (`phd thesis`, `master thesis`), report (`project proposal`,
  `grant proposal`, `technical report`, `protocol`), unpublished, misc.
  An identifier (doi, pmid, eprint+eprinttype, url) is required only for
  article, online, book, inbook, incollection and inproceedings; report,
  thesis, unpublished and misc need `institution` or `note`.
- **Folder `proposals`.** A report whose `type` mentions a proposal goes to
  `papers/proposals/` (before the lab, datasets, methods and reading rules).
- `GET /papers/upload/requirements` gains `notice` (who sees the library,
  and that its text may be sent to AI providers), `uploads_enabled`,
  `default_role`, `document_types`; `roles` becomes a mapping role to
  meaning and `entrytypes` a mapping entrytype to description (both were
  lists); `folders` includes `proposals`.
- `POST /papers/upload` answers 503
  `{"status": "disabled", "message": "Paper uploads are switched off on this site (services.paper_upload)."}`
  without writing when the library is not writable (2.40.x answered 500).
  ant-mcp hides `check_paper` and `upload_paper` in that case.
- MCP instructions, `anthive://guide` and the literature and upload tool
  descriptions say that everything the tools return goes to the AI client's
  LLM provider.
- **Upload features are only visible to uploaders.** `GET
  /papers/upload/requirements` now needs `library_upload_groups` too (it was
  open to every signed-in user); both upload routes answer non-uploaders 403
  `{"status": "forbidden", "message": "Only users in <groups> can add papers to the library."}`
  (before any 503) and are no longer in `/openapi.json` (documented in
  `doc/LIBRARY.md`). ant-mcp hides `paper_upload_requirements` as well as
  `check_paper` and `upload_paper` from non-uploaders. The help page no
  longer lists the upload tools.

- **Expression layers are found next to each `.duckdb`.** Served queries
  no longer read the `layer__*` views, whose stored path points at the
  machine that converted the dataset. Moved or renamed datasets work again:
  `gene_stats?top=N` answered 500 on every container site, and renamed
  collections (Wachter2024, Sierksma2026) gave 400/500 on cells, plots and
  SQL. Raw SQL that names `layer__X` gets per-connection views with local
  paths. The `top=N` fallback ranks genes by mean over all cells (it used
  the mean over expressing cells only), matching `__gene_bins.mean_expr`.
- **Pseudobulk works again.** It joined on a column the layer view does not
  have (500 on every parquet-backed dataset) and grouped on stored instead of
  public column names (500 for renamed columns such as `ant_apoe`).
- New web page `/api-guide` (top bar and user menu): how to sign in to the
  API, first requests, vark and pyvark. The raw docs stay at `/api/docs`.
- Deployment (no API change): per-container secrets, host-side
  `ant-stack publish-library`, Caddy proxy trust with `tls: off`, `ant-stack
  pull` fix. See the upgrade notes in `doc/DEPLOY_CONTAINERS.md`.

## 2.40.2 — 2026-10-02

Patch release: no endpoint changes. `/me/mcp` snippets gain more clients.

- New `doc/MCP_CLIENTS.md`: setting up anthive in 22 AI clients (Claude Code,
  claude.ai and Claude Desktop, Codex, ChatGPT, Gemini CLI, OpenCode, Cursor,
  VS Code / Copilot, Windsurf, Zed, Cline, Roo Code, Continue, Goose, Amp,
  Kiro, Warp, JetBrains, Junie, LM Studio, Cherry Studio, Pi) plus the
  `mcp-remote` bridge, each with a link to the vendor docs; cloud connectors
  need a publicly reachable site.
- The help page links to the documentation on GitLab (`docs_base_url`,
  `site.docs_base_url`) instead of repeating it.
- The MCP links page gets setup tabs for Codex, Gemini CLI, VS Code, Cursor
  and mcp-remote, and links to the full guide.

## 2.40.1 — 2026-10-02

Patch release: no endpoint or response changes. Web UI and data layout only.

- Paper PDFs moved out of the h5adstore (`*/static/*.pdf`) into the paper
  library (records in `bdslab/anthive/library_store`, PDFs on S3 under
  `_library/papers/`); collections link to their papers with `papers:`.
- Dataset pages get a Papers tab (before Downloads), collection pages a Papers
  panel (after the datasets), each paper with the reason it is linked; paper
  pages `/papers/<citekey>` and a Library page `/library`; Downloads lists the
  linked papers' PDFs.
- One shared, more distinctive section-heading style on collection and
  dataset pages.
- Version button in the top bar (from 2.40.0) and an exhaustive help page
  covering MCP links, every MCP tool, prompt and resource, skills (install with
  the skills CLI), several servers, the paper library and programmatic access.

## 2.40.0 (2026-10-02)

Additive: adding papers through the API, and a two-way library sync.
Existing clients keep working.

- `GET /papers/upload/requirements`: `required`, `recommended`, `fields`
  (per field `description`, `example`, `type`, optional `vocab`), `roles`,
  `folders`, `entrytypes`, `max_pdf_mb`.
- `POST /papers/upload` (multipart): `metadata` (JSON object or `.md`
  text), optional `file` (PDF), `folder` (`lab|datasets|methods|reading`),
  `enrich` (default true, fills only empty fields), `dry_run`, `update`.
  201 `{status: created, citekey, folder, path, pdf_sha256, record,
  filled_fields, warnings, missing_recommended}`; 200 for `updated` and
  `dry_run`; 409 `{status: duplicate, existing_citekey, matched_on:
  doi|pmid|sha256|citekey}`; 422 `{status: invalid, errors: [{field,
  message}], missing_required}`; 413 PDF too large; 415 not a PDF; 403
  unless the caller is in `library_upload_groups` (default `[admins]`).
  `papers_for` is stored on the paper as `anthive_proposed_links`, never
  written to the h5adstore. Post-commit steps (index, reload, S3) only add
  warnings.
- `GET /library/status` gains `upload_index` (state of the background index
  update after an upload).
- MCP: `paper_upload_requirements`, `check_paper`, `upload_paper` (the last
  two only for uploaders; PDF capped by `mcp.max_upload_mb`).
- `ant sync library` (also part of `ant sync push|pull` unless
  `--no-library`): records via git, PDFs two-way against S3 with conflict
  copies and opt-in delete propagation, literature index publisher rule.
  New config keys: `library_upload_groups`, `library_upload_max_pdf_mb`,
  `library_git_committer`, `library_git_push`, `library_s3_autopush`,
  `library_index_publisher`, `mcp.max_upload_mb`.

## 2.39.0 (2026-10-02)

Additive: the paper library and literature search. Existing clients keep
working.

- **Library** (`lib/library.py`, `doc/LIBRARY.md`): `GET /papers` (filters
  `role`, `celltype`, `tissue`, `disease`, `method`, `cohort`, `organism`,
  `author`, `year`, `q`), `GET /papers/{citekey}` (record, notes, linked
  datasets and gene sets by role, related papers), `GET /papers/{citekey}.bib`,
  `GET /library.bib` (`citekeys`, `classic`), `GET /papers/{citekey}/related`
  (`kind`: explicit, cohort, celltype, tissue, disease, method, author, cites,
  cited_by; each relation with weight and evidence), `GET /papers/{citekey}/file`
  (the PDF, streamed or a presigned S3 link), `GET /library/status`.
- **Literature search** (`lib/library_index.py`, `doc/LIBRARY_INDEX.md`):
  `GET /search/literature` (`q`, `k`, `mode` hybrid|keyword|semantic,
  `section`, `citekey`, `year`) returns passages with citation, page, section
  and a highlighted snippet. Semantic and hybrid fall back to keyword search
  with a warning when the server has no matching query model.
  `GET /papers/{citekey}/pages/{n}` returns the text of one page.
- **Datasets**: `GET /databases/{id}/papers` (linked papers, source first) and
  `GET /databases/{id}/context` (`level` brief|full, `budget_tokens`): a
  background pack for LLM analysis (metadata, caveats, obs columns, module
  scores, source papers with methods passages, related papers).
- **Access**: `anthive_access: restricted` papers are admin-only. Calls from
  ant-mcp (or with `for_llm=true`) honour `anthive_llm`: `never` hides the
  paper, `summary_only` keeps metadata and summary but no passages, notes,
  abstract or page text.
- `/admin/rescan` reloads the library and the index. `GET /skill` works again
  (it looked in the wrong folder and the file was missing from images).

## 2.38.0 — 2026-10-02

Additive: several named MCP links per user. Existing clients keep working.

- `GET /me/mcp` gains `links` (one entry per non-revoked link: `id`,
  `label`, `url`, `active`, `created`, `rotated_at`, `expires_at`,
  `last_used`), `max_links` and `snippets_template` (client snippets with a
  `{url}` placeholder). The 2.37 top-level fields (`has_link`, `url`,
  `created`, ..., `snippets`) stay and describe the user's first link.
- New: `POST /me/mcp/links` (body `{"label": "..."}`, optional; 409 at the
  per-user cap `mcp.max_links_per_user`, default 5),
  `POST /me/mcp/links/{id}/rotate` (only that link's token changes),
  `DELETE /me/mcp/links/{id}`, admin `DELETE /admin/mcp-links/{user}/{id}`.
  Unchanged: `POST /me/mcp/rotate` (first link, created if none),
  `DELETE /me/mcp` and `DELETE /admin/mcp-links/{user}` (all of a user's
  links).
- `GET /admin/mcp-links` returns one row per link and adds `id` and `label`.
- The link store converts a 2.37 database (one row per user) in place on
  first open; existing tokens stay valid and are labelled "default".
- New config keys: `mcp.max_links_per_user`, `issue_tracker_url` (web UI
  "Report an issue" menu entry).

## 2.37.2 — 2026-09-28

Patch release: no endpoint or response changes.

- Startup no longer waits for the module-score and by-gene indexes; they
  build in a background thread. A site whose catalog is mostly S3-remote
  (a fresh deployment that has not pulled every dataset) used to spend
  minutes opening every remote database before answering its first health
  check, so `ant-stack up` gave up on it. Until the background build
  finishes, module-score listings can be incomplete.
- Container images are built from `git archive` and verified by
  `docker/check-image`; the 2.37.1 images accidentally carried untracked
  local files (Authelia user database, server log, a nested checkout) and
  were republished clean.
- `ant-stack check` flags missing S3 key files when `s3.enabled`, warns on
  an empty catalog and on datasets without a local `.duckdb`, and refuses
  authelia mode with a `public_url` host Authelia cannot use as a cookie
  domain (no dot, not an IP, e.g. `localhost`) or without https.
  `site.example.yaml` no longer defaults to that combination.
- `ant-stack up` recreates the Authelia container on every run, so a fixed
  config replaces a container stuck in crash-loop backoff.
- Caddy gets `default_sni <public host>` from ant-stack, so browsing to the
  bare IP of a containerised site no longer aborts the TLS handshake.

## 2.37.1 — 2026-09-25

Middleware fixes. No endpoint or parameter changes.

- **Endpoints no longer block each other.** 71 handlers that ran DuckDB,
  S3 or file IO directly on the event loop are now plain `def` (Starlette
  threadpool). `/info` stayed under ~0.2 s while a 500k-row `/cells` ran;
  before it stalled up to 12 s.
- **`/cells` JSON and CSV are streamed** in 5k-row chunks (chunked
  transfer encoding, no `Content-Length`). The body is byte-identical.
- **`?cache=miss` works on plot endpoints** as documented: it skips the
  cache lookup, re-renders, refreshes the entry and answers
  `X-Plot-Cache: BYPASS`. It is no longer part of the cache key.
- **Plot cache fingerprint includes the `.scores.parquet` sidecar**, so
  a rescored dataset no longer serves stale score plots.
- **503 instead of 500 while a writer holds a dataset.** `/cells`,
  `/obs_summary`, `/genes/{gene}`, `/gene_stats` and the other handlers
  whose broad `except` used to turn the pool's 503 (and explicit 404s)
  into 500 now pass them through.
- **`POST /admin/restore/{db_id}` reloads that dataset's metadata**
  (new `metadata_reloaded` field), so columns an annotator added show up
  in `/info` without a rescan.
- **`/metrics.connection_pool` shape changed** (operator diagnostics):
  the adaptive-throttling block (`total_budget_mb`, `min_per_conn_mb`,
  `last_per_conn_mb`, `next_acquire_per_conn_mb`, `throttle_ratio`) is
  gone, since per-connection `SET memory_limit` never worked (DuckDB
  settings are per instance). New: `retiring_instances`, `lru_closes`,
  `max_open_databases`, `memory.per_db_limit_mb`, `memory.threads_per_db`.
- **Pool connections are sandboxed by DuckDB itself**:
  `enable_external_access=false` with `allowed_directories` limited to
  the data folder, each dataset's own directory and the spill directory,
  then `lock_configuration=true`. `read_text('/any/file')` and similar
  fail with a permission error on every pooled query path.

## 2.37.0 — 2026-08-13

### Added — `obs_derive`: value-map a new obs column at view time

Extends the `api:` section with `obs_derive` — publish a NEW obs
column computed from an existing one via a value mapping, without
touching the h5ad or the underlying duckdb tables.

```yaml
api:
  obs_derive:
    braak:                       # new column name (public)
      source: braak_stage        # existing obs col
      dtype: numerical           # → obsnum view (categorical → obscat)
      map:
        "0": 0
        "I": 1
        "II": 2
        "III": 3
        "IV": 4
        "V": 5
        "VI": 6
      default: null              # optional; unmapped values → NULL
```

The mapping compiles into a `CASE WHEN` expression inside the target
view's SELECT. When the `source` column lives in the sibling __obs*
table (e.g. an obscat string → obsnum numeric derivation), the view
now `LEFT JOIN`s that table too.

Rules:
- `source` must exist in either `__obscat` or `__obsnum` at rebuild
  time; unresolvable sources silently skip (log-friendly).
- `dtype`: `numerical` → the derived col lands in `obsnum`;
  `categorical` → `obscat`.
- `map` values are coerced to `float` (dtype=numerical) or `str`
  (dtype=categorical). Values not in `map` fall back to `default`
  (or NULL if `default` is omitted).
- New col names go through `safe_ident` — must be `[A-Za-z_][A-Za-z0-9_]*`.

Writeable via `POST /admin/api-config/{db_id}` alongside the existing
`obs_rename` / `obs_hidden` / `obs_value_{rename,hidden}` fields.
Reversible in one API call (`{"obs_derive": {}}`).

Additive; existing views without `obs_derive` are byte-for-byte
identical.

---

## 2.36.1 — 2026-08-08

### Fixed — `/notebook` endpoint 500 on `bin_color` and other recently-added params

`_NOTEBOOK_PARAM_TYPES` (the coercion table that turns raw query-string
values into typed Python for the notebook builder) was stale. Any URL
param not listed there was passed to the plot handler as `str`, so
`?bin_color=3` became `bin_color="3"` and blew up inside `pd.qcut` with
a numpy `str >= int` comparison. Same class of bug for any recently-
added param.

Added coercion for: `bin_color` (int), `step_size` (int),
`clip_x`/`clip_y` (float), `show_ci`/`show_ci_corrected`/`rank_x`/
`sort_desc` (bool).

Callers that already worked continue to work; this is strictly a
bug-fix for the notebook path.

---

## 2.36.0 — 2026-08-07

### Added — `bin_color` on `/plot/histogram`, `/plot/ecdf`, `/plot/kde`

Extends the numerical-to-categorical binning pattern (2.34 for
scatter/hexbin/rolling; 2.35 for kde2d) to the three univariate
distribution geoms. `color` may now be numerical or gene when paired
with `bin_color=N`; the distribution is then split into N per-group
overlays. Requiring `bin_color` for continuous `color` (else 400) also
closes a soft failure where a raw numerical hue silently produced an
un-plottable per-value legend.

Additive.

---

## 2.35.0 — 2026-08-07

### Added — multi-category overlay on `/plot/kde2d`

`/plot/kde2d` now accepts `color`, `bin_color`, and `bin_color_method`
(same semantics as scatter/hexbin/rolling). When `color` is set:

- Categorical → one line-contour set per group, overlaid on shared
  axes; distinct palette colour per group + category legend.
- Numerical or gene → require `bin_color=N` to bin first (else 400).

Groups are capped at 8 (readable overlay ceiling). Groups with fewer
than 5 finite points appear in the legend as ``(n=X, no KDE)`` without
drawn contours. Filled-contour density + colorbar are only shown in
single-group (no `color`) mode; multi-group renders line contours with
faint 10% α fills so overlapping regions read cleanly.

Bandwidth, transforms, and the evaluation grid are shared across
groups so contours can be visually compared 1:1.

Additive.

---

## 2.34.0 — 2026-08-07

### Added — on-the-fly binning of continuous colour fields (`bin_color`, `bin_color_method`)

Two new optional query params on `/plot/scatter` and `/plot/hexbin`:

| Param | Values | Effect |
| --- | --- | --- |
| `bin_color` | int, 2–20 | Bin a numerical-obs or gene `color` into N categorical bins before plotting. Ignored when `color` is already categorical or unset. |
| `bin_color_method` | `quantile` (default) or `width` | `quantile` = equal-count bins (`pd.qcut`); `width` = equal-width bins (`pd.cut`). |

Bin labels are formatted `"[NN] lo-hi"` with a zero-padded index prefix
so the alphabetical legend sort respects bin order. The colour field
name is rewritten to `"<col> · N method bins"` so the legend title
and figure caption self-document.

Example — colour a UMAP by age binned into 3 equal-count groups:

```
GET /api/databases/S25/PhM/plot/scatter?color=Age&bin_color=3
```

Example — colour by CX3CR1 expression, 5 equal-width bins:

```
GET /api/databases/S25/PhM/plot/hexbin?color=CX3CR1&bin_color=5&bin_color_method=width
```

Quantile binning uses `duplicates='drop'`; if many values coincide (dropout
genes, discrete metadata), the actual bin count is smaller than requested
and the resulting field name / caption reflects it.

Additive. Callers that don't pass `bin_color` see identical behaviour to 2.33.

---

## 2.33.0 — 2026-08-07

### Added — value-level rename + hide (`obs_value_rename`, `obs_value_hidden`)

Extends the `api:` section from 2.32 with two more knobs, applied
inside the same obscat view via `CASE` expressions:

```yaml
api:
  obs_value_rename:
    Gender:                         # collapse mixed vocab
      M: male
      F: female
      Male: male
      Female: female
  obs_value_hidden:
    Gender: [None, Unknown, ""]     # values that get NULL'd
```

Applied in this order inside the view's CASE for each affected col:
1. Hidden values → NULL (hide wins over rename if a value is in both)
2. Rename branches (many→one collapse supported)
3. Passthrough

Zero-cost to plain (unaffected) cols. Adds a few nanoseconds per row
per query on cols with a CASE — imperceptible next to plot render
time. Predicate pushdown still works: `WHERE sex = 'male'` on the
aliased col gets pushed to `Gender IN ('M', 'male', 'Male')` on the
underlying table.

Both endpoints (`POST /admin/api-config/{db_id}`,
`POST /admin/refresh-views/{db_id}`) now accept + echo these fields.

---

## 2.32.0 — 2026-08-07

### Added — `.md` `api:` section + admin endpoints for column rename/hide

New optional section in dataset `.md` YAML frontmatter:

```yaml
api:
  obs_rename:
    Gender: sex               # internal → public
    Cognitive.Status: cognitive_status
  obs_hidden:
    - internal_qc_col
```

Applied at the `obscat` / `obsnum` **view** projection layer in DuckDB:

* Renamed cols surface under the public alias in every API surface
  (`/info`, `/plot/*`, `/cells/*`, `/analytics/query` SELECT-side, CSV export).
* The old internal name is unreachable via the API — any endpoint that
  references it returns 400 "column not found".
* `obs_hidden` cols are omitted from the view entirely (same effect as
  rename, no public alias offered).
* A rename automatically hides the internal name — no separate hide
  entry needed.
* Cols stay unchanged in the underlying `__obsnum` / `__obscat` tables,
  so internal tools (`ant-annotate`, `ant-score`, direct SQL against
  `__obsnum`) continue to see the original names.

**Two new admin endpoints:**

* `POST /admin/api-config/{db_id}` — body `{obs_rename, obs_hidden}`
  writes the api section to the `.md` and rebuilds the views. Both keys
  are optional; omit to leave existing values alone; pass `{}`/`[]` to
  clear. Validates: no two renames targeting the same public name; no
  target colliding with `cell_id`/`cell_name`.
* `POST /admin/refresh-views/{db_id}` — rebuild the views from the
  current `.md` without touching it. Useful after an out-of-band
  edit (text editor, git pull).

Both go through the connection-pool release/restore dance so an
in-flight query completes before the r/w rebuild.

**h5adstore git**: the `.md` file is written to disk but NOT
auto-committed — the admin's job to commit + push separately.

**Client impact**: additive. Existing datasets without an `api:`
section behave unchanged. Clients that read `/info` will see whatever
names are exposed (rename applied) — they should treat those as the
canonical names for that dataset.

---

## 2.31.0 — 2026-07-29

### Added — `/plot/rolling` — `rank_x`, `show_ci`, `show_ci_corrected`, `step_size`

Two new optional booleans on `GET /databases/{id}/plot/rolling`:

* `rank_x` (default `false`) — plot against the cell rank along `x`
  (0..n-1 after sorting) instead of the raw `x` values. With `color`,
  ranks are computed per group. The axis label becomes `rank(<x>)` and
  the caption reflects the change. Motivation: pseudotime is often
  clumped (bimodal / saturated); rank-x gives each cell equal visual
  weight so the rolling mean isn't dominated by dense pseudotime
  regions.

* `show_ci` (default `false`) — shade a **95% confidence interval**
  around the rolling mean (±1.96·SE, where SE = rolling_std / √window).
  Complements the existing `show_band` (IQR); both may be enabled
  simultaneously — the CI band is narrower and slightly more opaque so
  they read as distinct layers.

* `show_ci_corrected` (default `false`) — shade a **95% CI corrected for
  autocorrelation between adjacent windows** (which overlap by
  window-1). Uses `n_eff = min(window, actual_n) · step_size / window`
  — with the default `step_size=1` (every point plotted) n_eff ≈ 1, so
  `SE_corrected = rolling_std / √n_eff ≈ rolling_std`. Prefer this over
  the naive `show_ci` on large windows — the naive band is too tight
  because it assumes each of the `window` cells is an independent draw.
  The interior median `n_eff` per trace is written to stdout for
  sanity-checking the correction factor.

* `step_size` (default `1`, range 1..50000) — stride between plotted
  rolling-window centers. Larger values subsample the plot (faster
  render, less clutter) AND scale n_eff in `show_ci_corrected`. Set
  `step_size == window` for independent, non-overlapping windows
  (`n_eff = window`, corrected CI ≈ naive CI).

**Client impact:** additive. Older clients that don't send these params
still get the raw-x, IQR-only behaviour (unchanged).

---

## 2.30.0 — 2026-07-02

### Added — `/catalogs` — unified module-catalog view (gene weights for starCAT + genesets)

Cross-source view of every module catalog in the anthive stack:

* `GET /catalogs[?source=geneset|starcat]` — list all catalogs
* `GET /catalogs/{source}/{catalog_id}` — one catalog with its module
  summaries + score recipes
* `GET /catalogs/{source}/{catalog_id}/modules/{module_name}[?top=N]` —
  one module's genes ± weights, top-N by |weight| for starCAT
* `GET /catalogs/{source}/{catalog_id}/scores/{score_name}` — one
  derived-score recipe (composite / binary threshold / multinomial)
* `POST /catalogs/rescan` — force cache invalidation

**What starCAT programs actually are** (relevant because the previous
API surfaced BOTH `usage__Microglia_Border_CAM` (continuous) AND
`Border_CAM_pos` (binary) — the latter is derivable from the former):

* Each cell gets a **usage vector** — one NNLS coefficient per program
  (21 programs), L1-normalized so they sum to 1. That's the atomic
  measurement.
* Derived **composite** scores (`DAM_progression`, `Activation_index`,
  `Disease_burden`, `DC_total`) are documented linear combinations of
  usage — coefficients + column names in the catalog YAML.
* Derived **binary calls** (`Border_CAM_pos`, `IRM_pos`, etc.) are just
  `usage[column] > threshold`, where the threshold is the 90th
  percentile computed on the in-build cells. Each binary call has one
  column and one threshold — trivially reproducible client-side.

`GET /catalogs/starcat/MICROGLIA_V1_0/scores/Border_CAM_pos` now
returns exactly the recipe:

```json
{"name": "Border_CAM_pos", "kind": "discrete",
 "columns": ["Microglia_Border_CAM"], "threshold": 0.0448,
 "normalization": "normalized"}
```

Once callers reach for the recipes, the `_pos` sidecar columns become
optional — a future release may drop them from the parquet sidecar
now that thresholds are queryable. Continuous `usage__*` columns
stay (they're the atomic signal).

`/genesets` is unchanged — the new `/catalogs` view is a wrapper on
top of the same registry, plus the starCAT side which had no API
surface before.

---

## 2.29.0 — 2026-07-02

### Fixed / added — `attach_sessions` columns usable as `x` / `y` in XY plots

`_handle_xy` (which powers `plot/scatter?x=<a>&y=<b>` and
`plot/hexbin?x=<a>&y=<b>` — the non-embedding XY modes) previously
refused any request where x or y was an `attach_sessions` column.
The color= embedding path worked already; the XY path was a
deferred TODO.

Now supported — all combinations:

* **session-vs-gene** e.g. `?x=<DAM_session_col>&y=SPP1`
* **session-vs-numerical-obs** e.g. `?x=<DAM_session>&y=microglia_score`
* **session-vs-session** e.g. `?x=<DAM_session>&y=<Homeo_session>`
  (works as long as the two sessions don't emit a column of the same
  name — the loader prefixes collisions with `session_1::` etc.)

Implementation: `_handle_xy` now categorises each axis into
`{gene, numerical, session}`, fetches genes+obs via
`get_cell_table` as before, then LEFT-merges each session frame
onto `data_df` by `cell_name`. Filter + sample constraints stay
correct because they're applied on the base fetch; missing cells
in the session land as NaN and get dropped by the existing
finite-value guard. CSV export (`_raw_df`) works transparently
since the columns are on `data_df` regardless of source.

No client change required — plot.html already passes
`attach_sessions` on all plot requests, and the previous 400 was
rendered as an error card. Users get a plot instead now.
Chat assistant's earlier "DAM score vs SPP1" refusal path now
succeeds.

---

## 2.28.0 — 2026-07-02

### Added — `GET /module-variability`

Fleet-wide per-`(dataset × source × catalog × module × tag × statistic)`
variability table. Sourced from `data/h5adstore/module_variability.parquet`
(produced by `bin/annot/ant-module-variability`).

Statistics — in a scale-invariant `[0, 1]` variance-explained frame so
categorical (η²/ω²) and numerical (R²/spearman) tags are directly
comparable, AND datasets with wildly different overall variability are on
the same scale (the statistics are ratios by construction).

Query params (all optional AND-composed):

* Filters: `dataset`, `source`, `catalog`, `module`, `tag`, `tag_column`,
  `statistic`.
* Value bounds: `min=`, `max=` (raw value), `min_abs=` (for signed stats
  like `spearman`).
* Sort: `sort=value:desc` (default), `value:asc`, `abs_value:desc`.
* Paging: `limit=N` (max 100 000, default 500).
* `format=json` (default), `csv`, or `tsv`.

Example — top 20 (dataset, module) where braak explains ≥ 10 % of
module-score variance:

```
GET /module-variability?tag=braak&statistic=R2&min=0.1&limit=20
```

`POST /module-variability/rescan` forces a reload from disk.

### Added — `module_variability` visible in the analytics SQL sandbox

The same table is now attached as a `TEMP VIEW module_variability` in
every `/analytics/query` call. Full SQL: JOIN with obs, aggregate,
group-by, whatever. E.g.:

```sql
SELECT dataset, AVG(value) AS mean_r2
FROM module_variability
WHERE tag = 'braak' AND statistic = 'R2'
GROUP BY dataset
ORDER BY mean_r2 DESC
LIMIT 10
```

### Deferred (behind-the-scenes)

* Added the `extra_parquet_views` argument to `lib/analytics.run_query`
  so ant-serve can attach ad-hoc parquets as SQL views. Composable —
  more global tables can be added later (e.g. per-DE-study summaries)
  without further signature churn.
* `bin/ops/ant-sync` now syncs `module_variability.parquet` (added to
  `SYNC_EXTENSIONS` + `PULL_DEFAULT_EXTS` as a specific-basename allow).

---

## 2.27.0 — 2026-07-01

### Added — External score sidecar (`<basename>.scores.parquet`) exposed as virtual obs columns

Module scores (Sierksma WGCNA, MICROGLIA_V1 starcat, future scorers)
now live in a **long-format parquet sidecar** next to each dataset's
`.duckdb`, keyed on `(cell_id, source, catalog, module, score)`. See
`lib/scores_parquet.py` and `bin/annot/ant-score-ext` /
`bin/annot/ant-starcat-ext` for the write path.

The API reads them transparently. Each `(source, catalog, module)`
row appears as a **virtual numerical obs column** with the legacy
naming convention that pre-sidecar callers already used:

* `source=geneset`  → `<catalog with / → ::>::<module>`
  (e.g. `Sierksma2025::WGCNA::Magenta_AG_presentation`)
* `source=starcat`  → `starcat__<catalog>__<module>`
  (e.g. `starcat__MICROGLIA_V1__DAM_progression`)

Impact on clients: **none** (additive). Score columns appear in
`GET /databases/{db_id}/info` `obs.numerical` alongside real obsnum
fields; `GET /plot/…?color=<score>` and analytics queries JOIN them
transparently.

Internal change of note: the metadata-cache fingerprint now includes
the sidecar's mtime so a new/updated `.scores.parquet` invalidates
cached entries on the next scan.

### Deprecated (soft) — writing new scores into the duckdb via `AnnotationWriter`

`ant-score` and `ant-starcat` still work, but **new module scores
should be written to the parquet sidecar** via `ant-score-ext` /
`ant-starcat-ext`. This decouples score compute from duckdb rebuilds
and lets a dataset carry different score panels without a full h5ad
→ duckdb round-trip. Existing in-duckdb score columns keep working;
the drop-and-remigrate tooling (`tmp/drop_conflicting_duckdb_scores.py`)
can move them out.

### Added — obs-column tag vocabulary (`.md` `obs_tags`)

New `.md` frontmatter field `obs_tags: {column: [tag, …]}` where each
tag is drawn from a curated 5-item vocab (`sex`, `braak`, `cluster`,
`age`, `genotype` — see `lib/obscol_tags_vocab.yaml`). Purely
additive; not surfaced in `/info` yet.

Tools:
* `ant md tag COLUMN=TAG1[,TAG2] FILE` — add tags (vocab-gated,
  hard-fails when column isn't in `obsnum_include ∪ obscat_include`)
* `ant md untag COLUMN[=TAG] FILE` — remove
* `ant md tags FILE [--by-tag TAG]` — list / reverse-lookup
* `ant md tag-vocab` — show the vocab
* `ant tag-propose DB_ID [--apply]` — heuristic proposer

---

## 2.13.0 — 2026-06-23

### Added — `GET /databases/{db_id:path}/pseudobulk`

Aggregated count matrix grouped by one or more obs categorical
columns, in the shape DESeq2 / edgeR expect: rows = genes, columns =
pseudobulk groups, plus a `gene_name` column.  The missing input for
bulk-style downstream tools (DESeq2, edgeR, limma-voom) — previously
callers had to round-trip the `.h5ad` themselves.

Query parameters:

* `group_by` (required) — comma-separated obs **categorical** column
  names that define the groups.  `leiden` → one column per leiden
  level; `leiden,donor` → one column per (leiden × donor) combination,
  with group ids formatted as `<leiden>_x_<donor>` (separator `_x_`
  chosen so it cannot clash with the values themselves).
* `layer` — layer to aggregate, default `counts`.  Must be integer-
  valued; the endpoint validates against the catalog `layer_stats`
  and 400s with a helpful "this layer looks log-normalized, use
  layer=counts" message when it isn't.
* `min_cells` — drop groups with fewer cells (default 10, range
  1..10000).  Dropped combinations are reported in the metadata so
  callers can audit them.
* `subset` — pre-filter applied BEFORE the GROUP BY, same format as
  the plot endpoints (`field:value`, `field:v1,v2`, `field:min..max`).
  Categorical only — numerical range filters are rejected with 400.
* `max_inline_mb` — threshold (default 100) for auto-escalation to
  async/temp-file mode.
* `async` (alias) — force the async branch regardless of size.
* `format` — `parquet` (default) | `csv` | `tsv`.  CSV/TSV are
  always synchronous; the colData metadata is exposed via an
  `X-Pseudobulk-Meta` header instead of being embedded in the body.

Sync (inline) response:

* Content-Type `application/x-parquet` (or `text/csv` / `text/tab-
  separated-values`), `Content-Disposition: attachment`.
* Parquet schema metadata key `anthive_pseudobulk_meta` carries the
  colData as JSON: per-group `n_cells`, `column_index`, the original
  obs-column values, plus dataset-level stats (`db_id`, `layer`,
  `group_by`, `subset`, `n_genes`, `n_groups`, `n_dropped_groups`,
  `dropped_groups`).
* Response headers: `X-Pseudobulk-N-Groups`, `X-Pseudobulk-N-Genes`,
  `X-Pseudobulk-Dropped-Groups`, `X-Pseudobulk-Layer`,
  `X-Pseudobulk-Group-By`, `X-Plot-Caption` (Latin-1 safe).

Async response (auto-escalated above `max_inline_mb` or via
`?async=true`):

* Content-Type `application/json`.
* Body `{artifact_url, expires_at, expires_in, size_bytes, sha256,
  db_id, layer, group_by, subset, min_cells, n_genes, n_groups,
  n_dropped_groups, groups, dropped_groups, caption}`.
* The parquet is written to
  `webapp/artifacts/pseudobulk/<token>.parquet` with TTL 1 hour and
  served by the reverse proxy at `/artifacts/pseudobulk/<token>.parquet`
  — same Authelia gate as `/api/`.  A background task sweeps expired
  files every 5 minutes.

Reverse-proxy note: the deployment may need a new `location
/artifacts/` block (see `config/nginx.conf` / `config/Caddyfile`).
Existing static-site location blocks for `/plots/` are the model.

Caller impact: purely additive.  Callers tracking `/version` will
NOT see `pseudobulk` in `plot_geoms` — it isn't a plot geom; it sits
under tag `data` alongside `/cells`.  vark and antomaton's
`pseudobulk_deseq` workflow can now drop their mock-mode fallbacks.

---

## 2.12.0 — 2026-06-22

### Added — `de_quadrant` plot geom (16th geom)

New `GET /databases/{db_id:path}/plot/de_quadrant` endpoint. Renders a
2-experiment effect-vs-effect scatter: one gene per point at
`(stat_x, stat_y)` where `stat` is the gene's DE statistic on each
axis. Quadrants encode concordance vs discordance; colour encodes the
4-way significance category (significant in X only / Y only / both /
neither, gated by `padj_threshold` independently on each side).

Query-param surface (in addition to the standard plot knobs):

* `study`, `term`, `contrast` — A side (lives on the path `db_id`).
* `db_y` (optional, default = path db_id), `study_y`, `term_y`,
  `contrast_y` — B side.
* `axis_x`, `axis_y` — one of `log2fc` (default), `signed_log10p`
  ( -log10(padj) * sign(log2fc) ), or `zeff`. Picked independently
  per axis.
* `padj_threshold` (default 0.05) — significance cutoff for the
  colour split.
* `n_label` (default 8) — top-N concordant + top-N discordant genes
  to label, ranked by `|stat_x * stat_y|`.
* `mode` — `scatter` (default) | `hex` (hexbin density variant).
* `log2fc_clip` — symmetric visual clamp applied to BOTH axes.

Cross-dataset comparisons are allowed only when both datasets carry
the same `organism` field in the obs catalog metadata; mismatch
returns 400 with a descriptive detail. Genes present on only one
side are dropped from the scatter (inner-join on `gene_name`), but
the caption reports the full set sizes (`|X|`, `|Y|`, union,
intersection) so callers can see how much got dropped. If the
intersection is empty, the endpoint returns 400 (not 500) with a
"no genes in common" explanation.

Colour mapping (with default `palette_cat='tab10'`): neither sig =
light grey (#bbbbbb), X-only = tab10[0] (blue), Y-only = tab10[1]
(orange), both = tab10[3] (red). Reference overlays: dashed
`y = x` (concordance reference) plus solid axis-zero crosshairs.
Bottom-right corner carries the Pearson r + Spearman ρ + n.

Caller impact: purely additive. Existing geoms keep working
unchanged. Clients tracking `/version.plot_geoms` will see
`de_quadrant` appear in the list. vark / pyvark need to extend
their geom enums to expose it.

---

## 2.11.1 — 2026-06-22

### Changed — prettier notebook params + real DE de-novo snippets

Patch release on top of 2.11.0. Same endpoint contract; only the
content of the generated `.ipynb` changes.

* **Multi-line `PLOT_PARAMS` / `RENDER_PARAMS`** in cell 3 — non-empty
  `dict(...)` calls now render one kwarg per line, 4-space indented,
  with a trailing comma on the last entry. Empty dicts stay on one line
  (`RENDER_PARAMS = dict()`). Easier to skim, easier to edit. The
  non-identifier-key fallback (`**{"weird.key": "value"}`) gets the
  same line-per-entry layout.
* **Real matplotlib snippets for all 4 DE geoms** (`volcano`, `ma`,
  `forest`, `de_heatmap`) — these previously emitted a comment-only
  stub pointing at `lib/plotting.py`. They now produce a ~15-line
  matplotlib snippet good enough to recognise the plot type. No
  significance overlays, no `adjustText` labels, no auto-zero-centred
  divergent palettes — those polish steps still live server-side; the
  PNG from cell 4 remains the canonical rendering.

Caller impact: none on the wire — only the rendered notebook content
changes. Clients that were grepping the cell-8 source for the string
`"lib/plotting.py"` need to drop that assertion.

---

## 2.11.0 — 2026-06-22

### Changed — generated notebook content overhaul (8-cell layout, split params, de-novo snippet)

The plot-to-notebook generator (`/api/notebook/{b64}/...ipynb`,
`/api/databases/{id}/plot/{geom}/notebook[.ipynb]`) now returns an 8-cell
notebook instead of the old 3-cell version. The endpoint URLs and
response headers are unchanged — only the rendered `.ipynb` content
changes.

* **New cell order** (8 cells total, types
  `[markdown, code, code, code, markdown, code, markdown, code]`):
  1. markdown — title + caption + JupyterLite persistence disclaimer
  2. code — connect to the anthive server (generic auth setup,
     `"pyodide" in sys.modules` detection for the in-browser micropip
     install path)
  3. code — `DB_ID`, `GEOM`, `PLOT_PARAMS`, `RENDER_PARAMS`
  4. code — render the plot via the API (PNG + caption inline)
  5. markdown — "Pull the raw data" intro
  6. code — fetch the data as a pandas DataFrame
  7. markdown — "Render the plot yourself" intro
  8. code — matplotlib/seaborn snippet re-rendering the plot from the
     DataFrame
* **`PLOT_PARAMS` (biology) vs `RENDER_PARAMS` (visual)** are emitted as
  two separate `dict(...)` calls in cell 3 and merged at the call site
  in cells 4 + 6:
  ```python
  img = c.get_plot(DB_ID, GEOM, **PLOT_PARAMS, **RENDER_PARAMS, format="png")
  ```
  Biology bucket covers selectors (`x`, `y`, `color`, `subset`, `layer`,
  `study`, `term`, `contrast`, etc.) + hypothesis-level thresholds
  (`padj_threshold`, `log2fc_threshold`). Render bucket covers visual
  tweaks (`width`/`height`/`dpi`, `alpha`, palettes, clip ranges,
  `n_label`, `window`, etc.). Unknown keys default to the biology
  bucket.
* **`dict(...)` syntax instead of `{...}`** for the params — reads
  better and lets the user kwarg-unpack cleanly. Non-identifier keys
  (rare) fall back to `**{"weird.key": "value"}` for that entry only.
* **Generic auth cell** — the auth helper no longer branches on
  Authelia / HTTP Basic / public; the active line
  `c = AnthiveClient(BASE_URL)` works for all four deployment cases.
  Inline comments point at the pyvark and vark CLI documentation
  (`codeberg.org/mfiers/pyvark`, `codeberg.org/mfiers/vark`). The
  `X-Anthive-Auth-Case` response header is kept for client telemetry.
* **JupyterLite detection** uses `"pyodide" in sys.modules` instead of
  the older `try: from vark import AnthiveClient / except ImportError:
  micropip.install` pattern — avoids the misleading "ImportError →
  install" code path when the real issue (outside JupyterLite) is that
  the user needs to `pip install pyvark` first.
* **De-novo snippet (cell 8)** — geom-specific matplotlib/seaborn
  starting point that re-renders an approximation of the figure from
  the DataFrame in cell 6. Eleven geoms get real code (`scatter`,
  `hexbin`, `violin`, `box`, `bar`, `histogram`, `ecdf`, `kde`,
  `kde2d`, `heatmap`, `rolling`). Four DE-driven geoms (`volcano`,
  `ma`, `forest`, `de_heatmap`) get a comment-only stub pointing at
  the server-rendered PNG from cell 4 and at `lib/plotting.py` as the
  reference implementation.

Caller impact: clients that consumed the notebook content positionally
(e.g. "cell 1 = data pull", "cell 2 = render") need to follow the new
cell order. Endpoint URLs, content type
(`application/x-ipynb+json`), and response headers (`X-Plot-Title`,
`X-Plot-Filename`, `X-Plot-Caption`, `X-Anthive-Auth-Case`) are all
unchanged.

---

## 2.10.0 — 2026-06-22

### Added — three new knobs on `/plot/de_heatmap`

`/plot/de_heatmap` gains colour-scale clipping, exclude-filters, and
axis sorting. All additive — existing queries continue to render
identically.

* **Colour-scale clipping**: `?vmin=N`, `?vmax=N`, `?vmin_quantile=Q`,
  `?vmax_quantile=Q` (semantics mirror the existing `/plot/hexbin`
  knobs). Quantiles are computed over the finite cell values of the
  pivoted matrix that's actually being coloured.  On the
  auto-divergent palette: setting only `vmax` mirrors to `-vmax` for
  `vmin` (keeps zero centred); setting both lets the user choose an
  asymmetric range; quantiles are honoured asymmetrically too (the
  user's call).
* **Negative-prefix filters** on `?contrasts=` and `?genes=`:
  * `?genes=-APOE` — every gene except APOE (include-all baseline,
    then drop negatives).
  * `?genes=A,B,-A` — start from `{A, B}`, drop A → `{B}`.
  * A negative pointing at a value that isn't in the data is a
    silent no-op (no 404).
  * The "none of contrasts/genes matched" 404 only fires when explicit
    *positives* were given and missed; pure-negative queries fall back
    to include-all.
* **`?sort=LABEL`** + **`?sort_desc=true`**: if `LABEL` matches a
  column (gene/module), the rows are reordered by that column's
  values; if it matches a row (contrast), the columns are reordered
  by that row's values. NaNs always sort to the end. Mis-matched
  labels return `400` with the closest suggestions. Overrides any
  positional ordering from `?contrasts=` / `?genes=`. No-op when the
  matrix has only 1 row or 1 column.

The `X-Plot-Caption` header now includes the active clip range,
quantile band, exclude tokens, and sort note when these are used.

Caller impact: purely additive — older clients keep working unchanged.

---

## 2.9.2 — 2026-06-22

### Added — biology-keyed filename in the notebook URL, plus a no-render `/meta` descriptor

The base64-encoded-params notebook endpoint now accepts the biology-keyed
filename as a real URL path segment, so JupyterLite (which names the
saved file after the URL basename, ignoring `Content-Disposition`)
stores it under the descriptive basename instead of literal `plot.ipynb`.

* **New route** `GET /notebook/{b64params}/{filename}.ipynb`
  — `filename` is purely cosmetic (the server still drives content
  from the b64 blob) and must match `[A-Za-z0-9_-]{1,100}`. Otherwise
  identical behaviour to the old fixed `/plot.ipynb` route.
* **New route** `GET /notebook/{b64params}/meta` — returns just
  `{title, filename, filename_truncated}` for the encoded params,
  WITHOUT rendering the plot. Pure-Python / param-only, typically
  <50 ms. Lets clients pre-compute the biology-keyed filename before
  opening JupyterLite. Field kinds are not resolved against the DB
  (this is the static-descriptor path), so for scatter/hexbin with no
  explicit `x` the title's embedding falls back to "embedding" — the
  filename token is still stable.
* **Back-compat**: the legacy `GET /notebook/{b64params}/plot.ipynb`
  URL keeps working unchanged (registered as a second decorator on
  the same handler). The legacy query-string
  `GET /databases/{id}/plot/{geom}/notebook[.ipynb]?…` URL is also
  unchanged.

Caller impact: purely additive.

---

## 2.9.1 — 2026-06-22

### Changed — hexbin plots draw one silhouette instead of a per-hex grid

`/plot/hexbin` (and the embedded hexbin path used by `/plot/scatter` with
`geom=hexbin`) no longer outlines each individual hex. Instead, a single
thin black line traces the perimeter of the union of all filled hexes;
disconnected regions each get their own outline, and internal holes get
their own inner outline. Affects all three colour modes (density,
numerical / gene, categorical).

Pure visual change — no contract change to query params or response
headers. Hence `patch`.

---

## 2.9.0 — 2026-06-22

### Added — biology-keyed `X-Plot-Title` and `X-Plot-Filename` response headers

Every `/plot/{geom}` response (image AND data formats) and the
`/plot/{geom}/notebook` endpoint now carry two new headers alongside the
existing `X-Plot-Caption`:

* **`X-Plot-Title`** — a short, human-readable one-line title that
  describes **what** is plotted (biology), NOT the render knobs. Examples:
  * `S25/V6A: P2RY12 expression on X_umap`
  * `S25/V6A: cell density on X_umap - subset patient_geno_short=L276`
  * `S25/V6A: Sex_donor / F (volcano)`
  * `S25/V6W: DE matrix - patient_geno_short_relevel_sexreg`
* **`X-Plot-Filename`** — a filesystem-safe basename (snake_case,
  `[A-Za-z0-9_-]` only, no extension, capped at 80 chars). Examples:
  * `S25_V6A_scatter_P2RY12`
  * `S25_V6A_hexbin_density_patient_geno_short_L276`
  * `S25_V6A_volcano_Sex_donor_F`

Both headers are ASCII-safe (latin-1, same encoding pass as
`X-Plot-Caption`). When the filename gets truncated to the 80-char cap,
an extra header `X-Plot-Filename-Truncated: true` is emitted.

`Content-Disposition: attachment; filename="…"` (CSV/TSV plot data and
the notebook `.ipynb`) now uses the biology-keyed filename instead of
the old `{db_id}_{geom}` heuristic — saved files reflect what they're
of, not what knob produced them.

The notebook's in-cell H1 markdown (`# {db_id} · {geom}`) was also
changed to use the biology-keyed title.

**Title templates by geom** — see `lib/plotting.build_plot_descriptor`
for the canonical mapping (per-geom string template + filename recipe).

**Caller impact**: purely additive. `X-Plot-Caption` semantics are
unchanged. Clients still using the older `Content-Disposition` filename
will start receiving a more descriptive basename — adjust hardcoded
filename parsing accordingly (no schema change).

### Cache shape

The in-memory plot LRU now also stores `title` / `filename` /
`filename_truncated`. Older cache entries (in flight at restart, or
serialised in alternative implementations) without these fields fall
back to rebuilding the descriptor from the raw `PlotRequest` — graceful
degradation, no error.

---

## 2.8.0 — 2026-06-22

### Added — plot-to-notebook endpoint

New endpoint `GET /databases/{db_id:path}/plot/{geom}/notebook` returns a
JupyterLite-compatible `.ipynb` (nbformat 4) for any plot URL. Accepts the
**same query-param surface** as the matching `/plot/{geom}` endpoint
(`x`, `y`, `color`, `study`, `term`, `contrast`, `padj_threshold`,
`log2fc_threshold`, `log2fc_clip`, `neglog10p_clip`, `logmean_clip`,
`n_label`, `palette_*`, `transform_*`, `bar_mode`, `normalize`, `facet`,
`group_by`, `color_scale`, `layer`, `subset`, `sample`, `width`,
`height`, `dpi`, …) — everything that geom understands.

Response:

```
content-type:         application/x-ipynb+json
content-disposition:  attachment; filename="<db_id>_<geom>.ipynb"
x-anthive-auth-case:  authelia | basic | public | unknown
```

Notebook has exactly three cells:

1. **Markdown** — title + the same X-Plot-Caption the image carries +
   a JupyterLite persistence disclaimer + a pyvark[vark-config] tip.
2. **Code** — instantiates a `vark.AnthiveClient(BASE_URL)` and fetches
   the plot's source dataframe via `format=csv` into pandas.
3. **Code** — re-fetches via `format=png`, displays the image inline,
   prints the caption underneath.

`BASE_URL` is derived from `X-Forwarded-Host` (with `X-Forwarded-Proto`
fallback) so the notebook is portable across deployments.

The auth block in cell 2 is templated per request:

* `Remote-User` / `X-Forwarded-User` header present → Authelia case,
  username pre-filled in the commented escape-hatch.
* `Authorization: Basic` header present → HTTP Basic case, username
  pre-filled.
* `ANTHIVE_PUBLIC=1` env var on the server → passwordless case (no
  credentials needed).
* Otherwise → unknown case (gated, generic `<user>` / `<password>`).

The active (uncommented) line is always `c = AnthiveClient(BASE_URL)` —
works inside JupyterLite at this server's origin (cookie / basic carried
by the browser) and on workstations via `pyvark[vark-config]`
auto-discovery. The commented line is the explicit-password fallback for
users who copy the notebook elsewhere.

### Integration with JupyterLite

Open the notebook in the in-browser Jupyter via:

```
/jupyter/lab/index.html?fromURL=<urlencoded-notebook-URL>
```

JupyterLite materialises the file in IndexedDB; *File → Download* lets
the user persist changes off-browser.

---

## 2.6.0 — 2026-06-20

### Added — plot data export (`format=csv` | `format=tsv`)

Every `/plot/{geom}` endpoint now accepts `format=csv` and `format=tsv`
in addition to the existing image formats (`png` | `svg` | `pdf`).
Same URL + same params (x/y/color/subset/sample/transform/bar_mode/
facet/group_by/de selectors/etc.) — but the response is the
post-subset/post-sample dataframe the handler queried from the DB,
not the rendered image:

```
content-type:        text/csv; charset=utf-8
content-disposition: attachment; filename="<db_id>_<geom>.csv"
x-plot-caption:      data behind /plot/<geom> for <db_id>: <N> rows x <M> cols
```

The dataframe emitted depends on the geom:
* scatter / hexbin (embedding or XY) — cell-level: `cell_name, dim_0,
  dim_1[, color_field]` (or `x_field, y_field, color_field` in XY mode)
* violin / box / bar — cell-level: `cell_name, x_field[, y_field][, hue_field]`
* histogram / ecdf / kde — cell-level: `cell_name, x_field[, color_field]`
* rolling — cell-level: `cell_name, x_field, *y_fields[, color_field]`
* kde2d — cell-level: `cell_name, x_field, y_field`
* heatmap — aggregated: `y_field, *gene_columns` (per-group mean matrix,
  since that's what's plotted)
* volcano / ma / forest — DE rows: `gene, log2fc, padj, base_mean, ...`

Guarantees the export is consistent with the plotted image (no
client-side reconstruction via /query/sql, no drift between the data
behind the figure and what the user downloads).

### Added — `group_by` on `/plot/bar`

In-plot clustering of x-bars by a 3rd categorical (distinct from
`facet`'s small-multiples). Single axes; x re-ordered so all values in
one group are adjacent; vertical separator + bold group-label band
between groups. Used for Sierksma Fig 1B where mice should cluster
under their Village. Doesn't multiply bars; cap unchanged. Coexists
with `facet`.

---

## 2.5.0 — 2026-06-19

### Added — group/collection metadata endpoint

The web UI needed the prose body and the full frontmatter from each
group's `index.md` to render a per-collection landing page (citation,
accession, study description). Previously only `group`, `experiment`,
`authors`, `year`, and `default_dataset` were surfaced on the dataset
detail; the prose body was silently discarded.

* **`GET /api/groups/{group_id}`** — new endpoint. Returns the
  group-level metadata + prose body from the nearest `index.md`,
  plus a list of `{db_id, title, id}` for every dataset in the group.
  404 if the 3-char `group_id` doesn't match any dataset's catalog
  entry. Example payload keys:
  `group, group_id, experiment, authors, year, doi, pmid, accession,
   default_dataset, description, datasets[]`.
* **`GET /api/databases/{db_id}/info`** — the `collection` block now
  carries `group_id`, `doi`, `pmid`, `accession`, and `description`
  (markdown body of the nearest `index.md`) in addition to the prior
  fields. Existing callers keep working — additive only.

Trigger to rescan if you don't see the new fields after upgrading:
`POST /api/admin/rescan`. The metadata cache is per-process and
doesn't auto-invalidate on .md edits.

---

## 2.4.0 — 2026-06-12

### Added — 2-D scatter / hexbin / KDE pack for module-level exploration

Driven by a module-annotator request — making two-continuous-field
plots (e.g. WGCNA module score A vs B) actually readable on 235k+
cell datasets instead of a single dense blob.

* **`GET /databases/{db_id}/plot/kde2d`** — new geom.  2-D Gaussian
  kernel-density estimate over two continuous fields.  Fits the KDE
  on a configurable subsample (default 15,000 cells) and evaluates
  on a 200×200 grid.  Renders ``n_levels`` filled magma contours +
  optional thin white iso-density overlay.  Optional ``point_overlay``
  raster scatter under the contours.
* **Per-axis numerical transforms** on ``/plot/scatter`` (XY-mode),
  ``/plot/hexbin`` (XY-mode), and ``/plot/kde2d`` via new
  ``transform_x`` / ``transform_y`` query params.  Seven kinds:
    * ``linear`` (default)
    * ``log`` — natural log; non-positive values become NaN and are
      dropped.
    * ``log1p`` — ``log(1+x)``; the legacy ``log_x``/``log_y`` booleans
      still work as aliases for this.
    * ``signed_log1p`` — ``sign(x) * log1p(|x|)``.  Symmetric, defined
      for all reals, very close to ``asinh`` visually; cheap.
    * ``asinh`` — smooth (C∞) symmetric transform; tunable via
      ``asinh_scale``.
    * ``signed_sqrt`` — ``sign(x) * sqrt(|x|)``.  Gentler compression
      than log.
    * ``rank`` — percentile in [0, 1].  Gives every cell equal visual
      weight.
* **Auto-clipping the hexbin density colour scale** at user-supplied
  quantiles via new ``vmin_quantile`` / ``vmax_quantile`` params on
  ``/plot/hexbin``.  e.g. ``vmin_quantile=0.10&vmax_quantile=0.90``
  clips at the 10th–90th percentile of per-hex counts, restoring
  contrast in the bulk of the distribution when a few hexes are
  10× denser than the rest.  Default off (preserves prior behaviour).
  Density mode only; ignored when ``color`` is set.  The colorbar
  shows ``extend='both'`` arrows when clipping fires.
* **Optional marginal univariate KDE strips** on ``/plot/scatter``,
  ``/plot/hexbin``, and ``/plot/kde2d`` via ``marginals=true``.
* **Optional regression overlay** via ``regline=global`` (one line +
  R²/Pearson/Spearman/slope in a stats box) or ``regline=by_group``
  (one line per categorical-colour group, capped at 30 groups; only
  meaningful when ``color`` resolves to a categorical field).
  Server uses ``scipy.stats.linregress`` + ``spearmanr``.

### Changed

* ``plot_geoms`` in ``/version`` now lists 14 entries (added ``kde2d``).
* The hexbin density colorbar grows ``extend='both'`` arrows when
  auto-clipping is active.  No visible change when it isn't.

### Backwards compatibility

* All 2.3.0 endpoints, params, and response shapes unchanged.
* The legacy ``log_x`` / ``log_y`` booleans on scatter+hexbin still
  work; they're now aliases for ``transform_x=log1p`` / ``transform_y=log1p``.
* antclient 1.7.0 picks up everything new via ``**extra`` on
  ``get_plot``.  No client-API change required.

## 2.3.0 — 2026-06-09

### Added — differential-expression data endpoints

Read-only surfaces over the new `__de_study` + `__de` tables (see
`doc/DESIGN_var_and_de.md`). Datasets without any DE data return
empty lists / 404 cleanly.

* `GET /databases/{db_id}/de` — list DE studies on this dataset, with
  per-study row, term, and contrast counts.
* `GET /databases/{db_id}/de/{study_id}` — one study's metadata plus
  its (term, n_contrasts) list.
* `GET /databases/{db_id}/de/{study_id}/terms/{term}` — contrasts
  within one term, each with significance counts at padj < 0.05, 0.01,
  0.001.
* `GET /databases/{db_id}/de/{study_id}/terms/{term}/contrasts/{contrast}`
  — paged gene rows. Sortable by `padj` / `effsize` / `estimate`;
  filterable by `padj_max` and `abs_log2fc_min`; `limit` / `offset`
  for paging.
* `GET /databases/{db_id}/de/by-gene/{gene_name}` — every DE row for
  one gene across the dataset's studies / terms / contrasts. Forest-
  plot data source.

Ingestion is **not** an API endpoint — the analysis itself happens
offline via the `ant-de-ingest` CLI, which produces an
`<basename>.de.parquet` sidecar (backed up to S3 by `ant-sync`) and
re-imports it into the duckdb. `ant-convert` auto-re-ingests the
sidecar after a rebuild, so DE survives full duckdb rebuilds.

### Added — DE-driven plot geoms

Three new plot endpoints, slot into the existing per-geom shape:

* `GET /databases/{db_id}/plot/volcano?study=&term=&contrast=…` —
  log2fc on x, -log10(padj) on y, top genes labelled.  Tunable
  `padj_threshold`, `log2fc_threshold`, `n_label`.
* `GET /databases/{db_id}/plot/ma?study=&term=&contrast=…` —
  log10(base_mean) on x, log2fc on y.  Requires the underlying DE
  rows to carry `base_mean` (DESeq2 / edgeR ingest captures it
  automatically; Wilcoxon / GLMM rows don't — endpoint returns a 400
  with a helpful "switch to volcano or re-ingest from DESeq2" hint).
* `GET /databases/{db_id}/plot/forest?gene=…[&study=&term=&contrast=]`
  — one gene across every (study, term, contrast); point = log2fc
  (or estimate fallback), bar = 95% CI when available. Optional
  filter args narrow the view.

Updated `/version`:

* `plot_geoms` now includes `volcano`, `ma`, `forest`.

### Client compatibility

* `antclient >= 1.6.0` — still compatible. The new endpoints flow
  through `get_plot(geom='volcano', study=…, term=…, contrast=…)`
  via `**extra`; no client API change.
* All 2.2 endpoints still work — additive change.

---

## 2.2.0 — 2026-06-07

### Added — per-geom plot endpoints

The unified plot endpoint was split into one path per geom. Each
endpoint now declares only the Query parameters that geom understands,
so OpenAPI shows a focused operation per geom instead of one 30-knob
kitchen sink, and FastAPI 422 errors actually reference the param that
is wrong **for that geom**.

* `GET /databases/{db_id}/plot/scatter` — embedding **or** 2-axis (x, y both continuous). Knobs: `point_size`, `alpha`, `log_x`, `log_y`, `vmin`, `vmax`.
* `GET /databases/{db_id}/plot/hexbin` — same polymorphism as scatter, hex-aggregated. Knobs: `gridsize`, `mincnt`, `alpha`, `log_x`, `log_y`, `vmin`, `vmax`.
* `GET /databases/{db_id}/plot/violin`, `/box`, `/bar` — `x` (categorical, required), `y` (continuous, optional), `color` (categorical, optional). Bar adds `ci`.
* `GET /databases/{db_id}/plot/histogram`, `/ecdf`, `/kde` — `x` (continuous, required), `color` (categorical, optional). Histogram adds `bins`.
* `GET /databases/{db_id}/plot/heatmap` — `y` (categorical, required), `x` (csv genes, optional), `n_genes`, `zscore`.
* `GET /databases/{db_id}/plot/rolling` — rolling-mean trajectory. `x` (continuous, required), `y` (csv continuous, required), `color` (optional), `window`, `show_band`.

### Added — discoverability endpoints

* `GET /version` — version + canonical-contract pointers + plot geoms + deprecated-endpoints list. Designed for LLM clients to cache and diff.
* `GET /changelog` — this file, served as `text/markdown`.

### Added — connection-pool memory throttling

`/metrics` gained a `connection_pool.memory` block with the live state of the **progressive DuckDB memory throttling** (`total_budget_mb`, `min_per_conn_mb`, `last_per_conn_mb`, `next_acquire_per_conn_mb`, `throttle_ratio`, `temp_directory`). Solo queries get the full budget; concurrent queries share it, floored at the minimum per-conn cap. Past-budget queries spill to `temp_directory` rather than OOM-erroring.

### Deprecated — unified `/plot?geom=X`

`GET /databases/{db_id}/plot` (the old dispatcher with `geom` as a query parameter) is **deprecated** but still functional. The OpenAPI spec marks it `deprecated: true`. Scheduled for removal in **2.3**. New code should call the per-geom endpoints above.

### Removed

* The long-deprecated `palette` query parameter (back-compat alias for `palette_categorical`) was dropped from `_PlotRequest` — passing it is now silently ignored by the server, and antclient 1.6.0 dropped the kwarg entirely. Use `palette_categorical=`, `palette_continuous=`, `palette_divergent=`.

### Internal / safety

* Plot cache key now includes the geom (the geom moved from query string to URL path; without this two different geoms with the same params would have collided on the same cache entry).
* Cache key normalises numerics so `dpi=80`, `dpi=80.0`, `dpi=80.00` all hash to the same key; param order in the URL no longer matters.

### Client compatibility

* `antclient >= 1.6.0` — uses the per-geom URLs.
* `antclient 1.5.x` — still works via the deprecated `/plot?geom=X` shim. Pin to 1.5.x against a 2.1 server.

---

## 2.1.0 — 2026-06-05

### Added — speed signal + module score

* `/health` gained `mean_response_ms`, `p50_response_ms`, `n_samples` — fed by a 1024-slot ring buffer of non-probe request wall-clock. Designed for client-side server selection (`AnthiveClient.pick_fastest`).
* `/metrics` gained a `latency_ms` block with `p50`, `p95`, `p99`, `max`, `mean`, `n_samples`.
* `POST /databases/{db_id}/module_score` — Seurat-style module score on the fly from an arbitrary gene list. Knobs: `genes`, `layer`, `n_control`, `seed`, `subset`.
* `GET /databases/{db_id}/module_scores` — list pre-computed module-score obs columns.

### Added — geneset store

* `GET /genesets`, `/genesets/{study}/{experiment}`, `/genesets/{study}/{experiment}/{name}` — geneset registry (returns 404 if no geneset store configured).
* `POST /admin/rescan_genesets` — rebuild the geneset index from disk.

### Added — analytics sandbox

* `GET /databases/{db_id}/analytics/schema` — list tables + columns visible to the sandbox (call before constructing a query).
* `POST /databases/{db_id}/analytics/query` — sandboxed SELECT-only query. INSERT/UPDATE/DELETE/ATTACH rejected. Result persisted as Parquet under a `session_id`.
* `POST /databases/{db_id}/analytics/viz` — run a Python visualisation script server-side against the Parquet result of an earlier `analytics_query`. Sandboxed, AST-scanned, 60 s timeout.

### Added — admin coordination

* `POST /admin/release/{db_id}` — drop the r/o pool handles for a duckdb so an external writer can take the r/w lock.
* `POST /admin/restore/{db_id}` — clear the eviction so r/o queries resume.

---

## 2.0.0 — Initial release

* `GET /` — API info.
* `GET /databases`, `/databases/{db_id}/info` — discovery.
* `GET /databases/{db_id}/genes`, `/databases/{db_id}/cells` — gene + cell data retrieval.
* `GET /databases/{db_id}/layers`, `/databases/{db_id}/metadata_fields`, `/databases/{db_id}/embeddings`, `/databases/{db_id}/embeddings/{name}` — schema endpoints.
* `POST /databases/{db_id}/sql` — raw SQL execution (replaced by analytics sandbox in 2.1).
* `GET /databases/{db_id}/plot` — unified plot endpoint (split into per-geom endpoints in 2.2; deprecated).
* `POST /admin/rescan` — re-scan the database registry.
