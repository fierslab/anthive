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

`pending_state_diagnosis(duckdb, h5ad_obs_keys)` is the shared truth used by
both `bin/ingest/ant-convert`'s guard and `bin/ingest/ant-merge-h5ad`: it
returns `clean`, `only_in_duckdb`, `already_merged`, `unrecorded`,
`missing_from_duckdb`.

## Tests
`tests/test_annotation_writer.py`, `test_ant_annotate_cli.py`,
`test_ant_merge_h5ad.py`, `test_ant_serve_release.py` cover the writer +
coordination paths. `test_analytics_security.py` guards the SQL sandbox.
