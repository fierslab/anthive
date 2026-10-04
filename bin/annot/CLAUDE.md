# bin/annot/ — module scoring + obs annotation

Compute new obs columns and add them to a `.duckdb` (and eventually back to
the `.h5ad`).

| Tool | Role |
| --- | --- |
| `ant-score` | Scanpy module scores from the **genesetstore** (`data/genesetstore/`) → one numerical obs column per geneset. Default: writes to duckdb via `AnnotationWriter`. `--rebuild` does the legacy h5ad rewrite. |
| `ant-starcat` | Project a dataset onto the `MICROGLIA_V1` starCAT catalog → 43 `starcat__MICROGLIA_V1__*` obs columns. Human-only; needs raw integer counts (not SCT). |
| `ant-annotate` | Load obs columns from a TSV. `status` shows what's pending, `rollback --run-id` reverts a batch. |
| `ant-facet-test` | Donor-level facet tests (sex, AD vs control, APOE e4, Braak) by pseudobulk limma-voom per cell type; exploratory. Output only under `<h5adstore>/_facet_tests/`. See `doc/DESIGN_facet_testing.md`. |
| `ant-facet-test-modules` | Same facets for module scores: donor means of the `.scores.parquet` scores (variant A) and pseudobulk background-matched scores (variant B, needs counts), OLS + 10,000-shuffle permutation p (default), BH per (dataset, stratum, facet, method). |
| `ant-suggest-tops` | Walk all `.md` files and propose `top_genes` / `top_cat` / `top_num` from curated marker panels + the cell's actual obs/var. See the script's docstring for the panels. |

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
