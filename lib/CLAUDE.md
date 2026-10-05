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
| `facet_lint.py` | `ant md-check` rules for a dataset's `api:` section (namespace, rename chains, numeric facets, Braak range, controlled values, Braak group rule; template / join derive forms; with the `.duckdb`: missing sources, unmapped values, join values without a crosswalk entry) and lineage (`reanalysis_of`, `is_secondary`, `donor_group`) |
| `celltype_groups.py` + `celltype_groups.yaml` | stratum label -> Cell Ontology based group for facet-test replication counting (exact, case folded, prefix; else `unmapped`) |
| `facet_results.py` | read-only view of the donor-level facet test results (`facet_results_dir`, default `<h5adstore>/_facet_tests`): unit summaries with the model from the run manifests, gene and module rows, volcano PNG, sex positive control, DRAFT cohort table, replication badge and recurring genes counted per cell type group (secondaries listed, never counted), with summed and distinct donors (`donor_key_digests` in the manifests). Defensive reads (missing or partial files mean not available), caches keyed by file mtime |
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
passes the group id. It also computes `ant_braak_group` / `ant_apoe_carrier`
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
