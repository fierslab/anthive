# bin/ingest/ — h5ad → duckdb pipeline

The conversion + maintenance tools that turn `.h5ad` files into the
`.duckdb` files served by `bin/api/ant-serve`.

| Tool | Role |
| --- | --- |
| `ant-convert` | Full rebuild of a `.duckdb` from a `.h5ad`. Refuses if `__pending_cols` is non-empty (`--force-discard-pending` to override). |
| `ant-merge-h5ad` | Cold-path merge of pending duckdb obs columns back into the h5ad without rewriting X. Self-heals half-completed merges. |
| `ant-build-all` | Walk the whole h5adstore, rebuild stale `.duckdb` (h5ad newer than duckdb), refresh `.md` (sha256/n_obs/layer_stats). Parallel-friendly. |
| `ant-h5ad-check` | Validate one `.h5ad` against the ingestion contract — log-norm in `.X`, raw counts in `layers['counts']`, embedding present, etc. |
| `ant-drop-indexes` | Rewrite existing `.duckdb` files without the redundant ART indexes older ant-convert runs created (flock + sentinel + ant-serve release/restore, verified content, atomic swap). `--dry-run` first. |
| `ant-duckdb-check` | Validate a `.duckdb` schema (`__cells`, `__genes`, `__obscat`, `__obsnum`, `__layer__*`, views, schema_version). |
| `ant-ingest-check` | **Read-only** check of a dataset submission (a group folder or one h5ad) before publication: 33 checks with frozen ids (`ant ingest-check --list-checks`), JSON report `anthive.ingest_check/1` plus markdown, exit 0 / 1 / 2. Runs `ant-convert` on a hard-linked scratch copy when there is no `.duckdb`, runs `ant-duckdb-check` on a private copy, never writes into the folder or the store. Package `lib/ingest_check/`, thresholds in `lib/ingest_check/thresholds.py` and `lib/facet_rules.py`. Docs: `doc/DATASET_INGESTION.md`. |
| `ant-ingest-index` | Builds the overlap indexes `ant-ingest-check` reads (donor digests with demographics, hashed cell barcodes, gene names per organism) under `config.get_ingest_index_dir()`, never in the store. Incremental; `build`, `status`. |
| `ant-ingest` | End-to-end ingestion from S3 `__incoming/` to the catalog: pull → sha-validate → promote (with collision detection) → run `ant-h5ad-check`/`-convert`/`-duckdb-check` → push to catalog root → re-stamp `s3_*` in the sidecar. Has `prune` subcommand for cleanup. Uses `lib.sidecar_merge` for `.h5ad.mus` merges (KEY=VALUE, machine-keys silent, semantic conflicts warn-and-keep-local). |

## Which tools rewrite files (do not run these on a submission or a live dataset)

- `ant-h5ad-check` rewrites the dataset `.md`, and `bin/meta/ant-check` calls it as a subprocess.
- `ant-duckdb-check` is **not** read-only: it builds a missing `__gene_bins` table inside the database,
  may write `default_layer` into the `.md`, and appends to `./ant-<date>.log` in the working directory.
  Run it on a copy. (It modified a served database once during the P1 work.)
- `bin/meta/ant-md-check` writes ids unless `--no-fix`; `lib/md_check.py` skips `_` folders below the scan
  root, so pass the group folder itself as the root.
- `ant-convert` resolves symlinks: a scratch copy must be a hard link or a copy, or the `.duckdb` lands
  next to the original.

For anything that must stay untouched use `ant-ingest-check`.

## DE sidecar

`ant-convert` re-imports `<stem>.de.parquet` into the fresh duckdb (full study rows
included). A failed reimport aborts the conversion (non-zero exit); a staged
build leaves the served duckdb untouched. `ant-duckdb-check` flags sidecar
studies the duckdb lacks.

## Dependencies

These tools rely on `lib/h5ad_utils.py` (load_h5ad, dtype inspection),
`lib/layer_stats.py` (per-layer min/max/median/q90/%zeros), `lib/metadata.py`
(parse/write `.md` frontmatter), and `lib/anthive_db.py` (DuckDB schema).

## Coordination with ant-serve

`ant-convert` and `ant-merge-h5ad` need **read-write** access to the
duckdb, but `ant-serve` holds it **read-only**. The `AnnotationWriter` context
manager (in `lib/anthive_db.py`) handles the dance:

1. POST `/admin/release/{db_id}` to ant-serve → pool closes idle handles, evicts the db key.
2. Open duckdb r/w, do work, commit.
3. POST `/admin/restore/{db_id}` → re-enable.

`--no-coordinate` skips this when ant-serve isn't running.

## Pending columns

`__pending_cols` is the audit trail for obs columns added incrementally by
`bin/annot/ant-score`, `ant-starcat`, `ant-annotate`. `ant-convert` refuses
to rebuild if anything is pending (you'd lose those columns); use
`ant-merge-h5ad` first to push them back into the .h5ad.

`pending_state_diagnosis()` in `lib/anthive_db.py` is the shared truth used
by both `ant-convert`'s guard and `ant-merge-h5ad`: returns `clean`,
`only_in_duckdb`, `already_merged`, `unrecorded`, `missing_from_duckdb`.

## Required-h5ad-contract

For ant-convert to accept a file, the .h5ad must have:
- `.X` = **log-normalized expression** (not raw counts, not SCT residuals)
- `layers['counts']` = raw integer counts
- At least one `obsm['X_*']` embedding (UMAP recommended)
- Consistent `var` index (gene symbols, not Ensembl IDs unless you're OK
  with `top_genes` being empty)

See `doc/INGESTION_CHECKLIST.md` for the longer form.
