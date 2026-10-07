# lib/ — shared core

The contract every `bin/<domain>/` tool and `ant-serve` depends on (imported
via a `sys.path` shim, not pip). **A change here ripples everywhere** — there
is no version boundary. Tread carefully and run the test suite.

After the 2026-06 reorgs, the consumers live under:

- `bin/api/`, `bin/ingest/`, `bin/annot/`, `bin/meta/`, `bin/ops/` — two
  levels up: `script_dir.parent.parent / 'lib'`.
- `web/` — one level up: `script_dir.parent / 'lib'` (the web UI is a
  top-level seam, not under `bin/`).

## Modules

| Module | Role |
| --- | --- |
| `metadata.py` | parse/write **anthive-catalog** fields in `.md` YAML frontmatter (id, sha256, n_obs, layer_stats, obscat_include, default_layer). Legacy `ANNOTATION_FIELDS` / `validate_annotation_field()` kept for backward-compat — **deprecated, use `mus.py`** |
| `mus.py` | **(post-2026-06)** read `<basename>.h5ad.mus` sidecars + `.env` folder cascade — the new home for experimental annotation (organism, tissue, cell_type, technology, suspension_type, …). Vocabulary at codeberg.org/mfiers/mus |
| `config.py` | load `anthive.yaml` (S3 creds, API host/port) |
| `anthive_db.py` | DuckDB query layer (`AnthiveDB`) + `AnnotationWriter` + pending-state helpers |
| `h5adstore.py` | resolve a `db_id` (`group/file`) to files on disk (`find_h5ad`, `H5adEntry`) |
| `h5ad_utils.py` | `load_h5ad`, dtype/layer inspection |
| `geneset.py` | genesetstore parsing/registry (`Experiment`, `Geneset`, `GenesetRegistry`) |
| `analytics.py` | SELECT-only SQL sandbox for ant-serve's analytics endpoint |
| `facet_mapping.py` | how a dataset's `.md` `api:` section projects author columns onto `ant_*` facets (renamed / derived / template / join / constant / native / computed, value translation, redaction of `obs_hidden` sources, gap hints). Mirrors `anthive_db.build_obs_view_sql`; keep the two in step |
| `obscol_tags.py` + `obscol_tags_vocab.yaml` | obs-column tag vocab; the `facet:` blocks are the single source of the facet definitions shown in the UI and API (`facet_definition`) |
| facet registry | `obscol_tags_vocab.yaml` is also the closed `ant_*` registry (`status: registered / deprecated`, scope, controlled values, range); `canonical_columns.CANONICAL_TAGS` and friends are derived from it (`obscol_tags.facet_registry`). Register a tag there before any `.md` publishes `ant_<tag>` |
| `obs_derive_forms.py` | the template (`{source, template: '{group_id}:{value}'}`) and join (`{source, join: donor_crosswalk.tsv, cohort, fallback_template}`) forms of `obs_derive`, used for `ant_donor_uid`: spec validation, value normalisation, crosswalk parsing (cached by mtime), the view SQL (the cohort's alias table is embedded as a `VALUES` subquery, so served views need no file) and `group_id_for_md`. Join tables resolve in the store root (`config.get_store_file`) |
| `donor_keys.py` | distinct donor counting: key = `ant_donor_uid`, else `<group_id>:<ant_donor>`; `key_digest` for the facet-test manifests. Used by `/donor-groups`, the facet test tools and `facet_results` |
| `facet_lint.py` | `ant md-check` rules for a dataset's `api:` section (namespace, rename chains, numeric facets, Braak range, controlled values, Braak group rule, facet outside its facet scopes; template / join derive forms; with the `.duckdb`: missing sources, unmapped values, join values without a crosswalk entry) and lineage (`reanalysis_of`, `is_secondary`, `donor_group`) |
| `celltype_groups.py` + `celltype_groups.yaml` | stratum label -> Cell Ontology based group for facet-test replication counting (exact, case folded, prefix; else `unmapped`) |
| `replicates.py` | `replicate_unit` / `replicate_column` of a dataset (declared in the `.md` or inferred), the analysis level rule (replicate level with >= 3 replicates per level, numeric facets >= 4 replicates with 3 distinct values, else "cell-level, descriptive only"), `per_cell_basis` marker of the per-cell variability numbers |
| `replication.py` | partial-conjunction replicability (Benjamini and Heller; Simes by default, Fisher available) and BH, pure numpy; used by `facet_results.py` for `r_value` |
| `disease_rule.py` | the common pathology disease definition (Braak x CERAD 2x2) and the `disease_derivation` spec check |
| `md_edit.py` | verified minimal text edits of a dataset `.md` front matter (set / delete a key, version patch bump + changelog); a YAML round trip rewrites most `.md` files |
| `facet_results.py` | read-only view of the donor-level facet test results (`facet_results_dir`, default `<h5adstore>/_facet_tests`): unit summaries with the model from the run manifests, gene and module rows, volcano PNG, sex positive control, DRAFT cohort table, replication badge and recurring genes counted per facet scope and cell type group (secondaries listed, never counted), with summed and distinct donors (`donor_key_digests` in the manifests). Defensive reads (missing or partial files mean not available), caches keyed by file mtime. v2 (2026-10-05): replication is a partial-conjunction r-value with exactly one anchor unit per cohort and cell type group; v3 (2026-10-06): on the ordinary one-sided limma p values, `replicated` = r < 0.05 and the effect gate (median effect over the cohorts >= tau, replicated sign), every answer per facet scope (`scope` argument, default `default_scope(facet)`), small cohorts count (`small_sample`); donor-pooled `pool:<id>` units from `_facet_tests/_pooled/` win the anchor; disease only combines pathology-based `disease_basis` values, units carry `analysis_level` |
| `facet_rules.py` | replicate-count thresholds shared by `bin/annot/ant-facet-test` and `ant ingest-check` (v3 statistical minimum: 3 replicates per level, 4 replicates with 3 distinct values for numeric facets, 2 residual df; `n_lt5` is only the `small_sample` flag) and the effect-gate taus (`gate_tau`: log2(1.1), Braak /3 per stage, age per decade; modules 0.2); one definition |
| `gene_classes.py` | gene-class flags (mitochondrial, ribosomal, lncRNA or clone, sex chromosome, long gene, APOE locus) from names and the GENCODE GTF (`gene_annotation_gtf` / `ANTHIVE_GENE_GTF`) for the facet result views |
| `facet_scopes.py` | facet scope of a dataset (`human_postmortem`, `xenograft_human_in_mouse`, `in_vitro`; pure mouse datasets have none) from `.h5ad.mus` and the published facets; `facet_exclusion_reason` says why a dataset is outside the facet layer (spatial or mass spectrometry technology, `facet_exclude: true` in the `.md`); `facet_allowed_scopes` reads the `facet_scopes` field of the facet registry. Facets are tested, counted and replicated per scope; `facet_lint.lint_facet_scope` errors on a facet outside its scopes |
| `tissue_groups.py` + `tissue_groups.yaml` | coarse tissue groups of the `ant_tissue` facet (umbrella terms such as prefrontal cortex) and the specific UBERON labels and ids behind the plain columns `tissue_uberon` / `tissue_uberon_id`; the group list is the registry's `controlled_values` for `tissue` (tested) |
| `help_content/` | **single source of the plain-language help**: `topics/<id>.yaml` (id = anchor on the help page, `summary`, `body_html`, `related`, `terms`, `children` rendered as `<h3>` subsections, `aliases`) and `glossary.yaml`. `web/templates/help.html` renders sections from it (`help_topic_html`, `help_glossary_html`), `ant-serve` serves it (`/help`, `/help/{topic}`, `/help/glossary[/{term}]`, `/help/search`) with markdown derived by `html_to_markdown`, and `ant-mcp` `explain` / `anthive://help` read it through the API. Edit the YAML, never the section text in `help.html`; `tests/test_help_content.py` and the parity test in `test_web_browser.py` guard it |
| `ingest_check/` | `ant ingest-check` package: registry of 33 checks with frozen ids (`checks_files/meta/layers/facets/donors/cells/design/access.py`), `CheckContext` (lazy sha256, backed h5ad, scratch convert, published `ant_*` views via `DatasetView`), report model and redaction (`report.py`: counts and digests only, crosswalk tokens scrubbed), obs `profile.py`, overlap index reader/writer (`overlap_index.py`), `thresholds.py`. Read-only on its input (`tests/test_ingest_check_no_destructive.py`) |
| `layer_stats.py`, `plotting.py`, `checksum.py`, `md_check.py` | layer stats, server-side plots, file hashing, `.md` sanity checks |

## DuckDB schema model (know this before touching anthive_db)

`ant-convert` builds, per dataset:
- `__cells(cell_id, cell_name)`, `__genes(gene_id, gene_name)` — base tables
- `__obsnum(cell_id, <numeric cols…>)`, `__obscat(cell_id, <varchar cols…>)`
- `obsnum` / `obscat` — **views** that join the `__`-tables to `__cells` to
  expose `cell_name`. The view's `o.*` is bound at CREATE time, so after an
  `ALTER TABLE … ADD COLUMN` you must DROP+CREATE the view to surface it.
- `layer__<name>` views over `__layer__<name>(cell_id, gene_id, value)` (sparse,
  zeros omitted). Current datasets keep layers in
  `<stem>.layer.<name>.{by_gene,by_cell}.parquet` sidecars and the views bake
  the converting host's absolute path. Never query `layer__*` in served code;
  use `_layer_source_for_gene_filter` / `_layer_source_with_names` (resolved
  from the duckdb's own location). See `doc/DATABASE_SCHEMA.md`.

## Two contracts that must not be bypassed

### `safe_ident(name)`
Every SQL identifier interpolated into an f-string MUST go through
`safe_ident()` — it validates against a strict regex and double-quotes. Never
hand-concatenate a column/table name into SQL.

### `AnnotationWriter` (incremental obs writes)
Context manager for adding obs columns without an h5ad rebuild. Invariants:
- **flock** on `<duckdb>.lock` serialises writers; refuses if a
  `<duckdb>.rebuilding` sentinel exists (set by ant-convert).
- `add_numerical` → `DOUBLE` in `__obsnum`; `add_categorical` → `VARCHAR` in
  `__obscat`. No overwrite unless `on_conflict='overwrite'`. Categorical
  cardinality capped (rejects cell-id-like columns).
- Records every column in **`__pending_cols`** (col_name, target_table, kind,
  added_at, annotator, run_id, catalog, source_hash) — the audit trail
  `ant-merge-h5ad` and `ant-convert`'s guard read.
- All-or-nothing per `with` block; auto-rollback on exception or no-commit.
- Optional `db_id`+`ant_serve_url`: on DuckDB "Conflicting lock", POSTs
  `/admin/release/{db_id}`, retries, POSTs `/admin/restore/{db_id}` on exit.

`build_obs_view_sql` takes `group_id` (for `{group_id}` templates) and
`store_root` (join tables; default the configured store); every caller
passes the group id. It also computes `ant_braak_group` / `ant_apoe4_carrier`
from `ant_braak` / `ant_apoe` when the `.md` declares neither
(`COMPUTED_FACETS`), and logs a warning for a derive whose source column is
missing (pass the sibling table, or cross-table derives look missing).

`pending_state_diagnosis(duckdb, h5ad_obs_keys)` is the shared truth used by
both `bin/ingest/ant-convert`'s guard and `bin/ingest/ant-merge-h5ad`: it
returns `clean`, `only_in_duckdb`, `already_merged`, `unrecorded`,
`missing_from_duckdb`.

## Tests
`tests/test_annotation_writer.py`, `test_ant_annotate_cli.py`,
`test_ant_merge_h5ad.py`, `test_ant_serve_release.py` cover the writer +
coordination paths. `test_analytics_security.py` guards the SQL sandbox.
