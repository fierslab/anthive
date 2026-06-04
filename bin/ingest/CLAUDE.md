# bin/ingest/ — h5ad → duckdb pipeline

The conversion + maintenance tools that turn `.h5ad` files into the
`.duckdb` files served by `bin/api/ant-serve`.

| Tool | Role |
| --- | --- |
| `ant-convert` | Full rebuild of a `.duckdb` from a `.h5ad`. Refuses if `__pending_cols` is non-empty (`--force-discard-pending` to override). |
| `ant-merge-h5ad` | Cold-path merge of pending duckdb obs columns back into the h5ad without rewriting X. Self-heals half-completed merges. |
| `ant-build-all` | Walk the whole h5adstore, rebuild stale `.duckdb` (h5ad newer than duckdb), refresh `.md` (sha256/n_obs/layer_stats). Parallel-friendly. |
| `ant-h5ad-check` | Validate one `.h5ad` against the ingestion contract — log-norm in `.X`, raw counts in `layers['counts']`, embedding present, etc. |
| `ant-duckdb-check` | Validate a `.duckdb` schema (`__cells`, `__genes`, `__obscat`, `__obsnum`, `__layer__*`, views, schema_version). |

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
