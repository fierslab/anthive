# Changelog

All notable changes to the Anthive Client will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2026-06-05

### Added — full coverage of new plot params

The server-side `/plot` endpoint gained several params over the last
day; `get_plot()` already forwards them via `**extra`, but the
docstring now enumerates them so callers know what's available without
reading the OpenAPI spec:

- **`hexbin`** (embedding) — new geom in REST API 2.1.  Aggregates
  cells into hex bins; three colour modes (categorical with dominance
  α, continuous mean, density).  Params: `gridsize`, `mincnt`.
- **`xy_scatter` / `xy_hexbin`** — generic 2-variable plots (not
  embedding-based).  `x` and `y` are arbitrary continuous fields
  (gene or numerical).  Params: `log_x`, `log_y` (apply log1p before
  plotting).
- **Three palette families** (replacing the single `palette`):
  `palette_categorical` (default `tab10`),
  `palette_continuous` (default `inferno_r`),
  `palette_divergent` (default `RdBu_r`).  Server auto-detects
  divergent for continuous fields straddling zero.
- **`vmin` / `vmax`** — clip the continuous colour scale at user-
  supplied limits.  Useful when one outlier hex would otherwise wash
  out the gradient.  Applies to scatter (continuous colour) and
  hexbin (all colour modes).

Existing call sites are unchanged.  All new params pass through
`**extra` to the URL query string — no client API break.

## [1.3.0] - 2026-06-05

### Added — full API coverage

After a 1.2.0 audit found 5 server endpoints had no client method, these
are now wrapped:

- **`analytics_viz(session_id, code, output_format='png')`** — run a
  Python visualisation script server-side against the Parquet result of
  an earlier ``analytics_query`` (sandboxed, AST-scanned, 60 s timeout).
  Pre-injected vars: ``df``, ``pd``, ``plt``, ``sns``, ``np``,
  ``DATA_PATH``, ``OUTPUT_PATH`` — do NOT call ``pd.read_parquet``
  (blocked).  Returns base64-encoded image bytes.
- **`rescan_genesets()`** — re-scan ``data/genesetstore/`` from disk and
  rebuild the score index.  Pair with ``rescan_databases``.
- **`get_plot_cache_stats()`** — ant-serve plot-LRU diagnostics.
- **`clear_plot_cache()`** — drop every entry from the plot-LRU.
- **`get_skill()`** — fetch the Anthive Claude Code skill file
  (``SKILL.md``) as plain Markdown; for collaborators installing the
  anthive skill into ``~/.claude/skills/``.

### Coverage

Every non-deprecated REST endpoint now has a client method.  The 4
``/databases/{id}/plot/{umap,gene,cat,num}`` endpoints are intentionally
skipped — they are deprecated on the server in favour of the unified
``/databases/{id}/plot`` (already wrapped as ``get_plot``).

## [1.2.0] - 2026-06-05

### Added
- **Constructor `auth=` parameter** — pass a `(user, password)` tuple or any
  `requests.auth.AuthBase` for HTTP Basic and friends. Required for servers
  fronted by a Caddy basic-auth gate.
- **`get_plot(db_id, geom, **params)`** — render plots server-side; returns
  a dict with `bytes` (image bytes), `caption`, `format`, `cache`,
  `source_url`, `mime`. Supports every geom the API exposes
  (scatter/violin/box/bar/histogram/ecdf/kde/heatmap) plus subset filters
  and random sampling.
- **`analytics_schema(db_id)`** — list tables/columns visible to the
  SELECT-only analytics sandbox (call before constructing a query).
- **`analytics_query(db_id, sql, ...)`** — sandboxed SELECT-only query,
  safer than `execute_sql` for untrusted input. Returns
  `{session_id, columns, dtypes, row_count, truncated, preview, ...}`;
  `session_id` can be reused for downstream visualization endpoints.
- **`module_score(db_id, genes, ...)`** — compute a Seurat-style module
  score on the fly from an arbitrary gene list.
- **`list_module_scores(db_id)`** — discover pre-computed module-score obs
  columns on a dataset.
- **Geneset access**: `list_genesets()`,
  `get_geneset_experiment(study, experiment)`,
  `get_geneset(study, experiment, name)`. `list_genesets` returns `{}`
  gracefully when the server has no geneset store configured.
- **`admin_release(db_id)` / `admin_restore(db_id)`** — coordination
  endpoints used by external writers (rarely needed directly; usually
  driven by `lib.anthive_db.AnnotationWriter`).
- **`AnthiveClient.pick_fastest(base_urls, auth=..., prefer='p50')`**
  classmethod — probe several anthive endpoints' `/health` and return
  the fastest base URL. Uses server-side `p50_response_ms` /
  `mean_response_ms` when available, falls back to client-observed RTT
  for cold servers (`n_samples < 10`).
- `get_health` docstring now documents the new `mean_response_ms`,
  `p50_response_ms`, and `n_samples` fields the server exposes (since
  REST API 2.1).

### Changed
- `_get` / `_post` now forward `self.auth` through to `requests`, so the
  new constructor param is honoured uniformly across every method.

### Compatibility
- All existing methods and call signatures are unchanged.
- Optional dep `pandas` is still the only optional; required deps remain
  just `requests`.

## [1.1.3] - 2026-03-18

### Added
- `get_gene_stats_all(genes, case_sensitive=False, format='json')` — query expression
  statistics for selected genes across **all** databases that have a `default_layer`;
  returns a flat DataFrame when `format='dataframe'`
- `get_gene_stats`: `case_sensitive` parameter — when False, resolves gene names
  case-insensitively so `APOE` matches `Apoe`; results include `gene_found` when
  the stored name differs from the queried name

## [1.1.2] - 2026-03-17

### Changed
- `search_genes`: added `exact` parameter for full-name matching (e.g. `APOE` no longer matches `APOER2`)
- `search_genes`: SQL injection fix — queries now use parameterized placeholders

## [1.1.1] - 2026-03-17

### Changed
- Version bump for PyPI upload

## [1.1.0] - 2026-03-17

### Added
- `anthelper` module now included in the package (`find_database`, `find_metadata`)
- `get_gene_stats(db_id, genes, layer, format)` — per-gene statistics without
  downloading all cells: n_cells, n_nonzero, pct_nonzero, mean, std (all cells),
  and mean/std/median/q10/q90/max over expressing cells only

### Changed
- Minimum Python version raised to 3.9 (was 3.8; required for anthelper)
- `anthelper`: replaced `str | None` union syntax with `Optional[str]` (3.9 compat)
- `anthelper`: removed unused `IPython.display` import (broke non-Jupyter installs)
- `anthelper`: hardened `find_database` against missing keys in database metadata

## [1.0.0] - 2026-02-24

### Added
- Initial release for Anthive REST API v2.0 (Phase 2)
- Single-file lightweight client (`antclient.py`)
- Full API coverage for all Phase 2 endpoints
- Optional pandas support for DataFrame output
- Pyodide/browser compatibility
- Streamlit caching support
- Auto-detection of API URL from environment
- Type hints for IDE support
- Comprehensive documentation and examples

### Features
- Database discovery and querying
- Gene search operations
- Cell data retrieval with filtering
- Embedding access
- Raw SQL query execution
- Metadata field listing
- Server health and metrics monitoring (Phase 2)
- Admin operations (database rescan)
- Multiple output formats (JSON, DataFrame, CSV, Parquet)

### API Methods
- `get_root()` - API information
- `get_health()` - Health check
- `get_metrics()` - Performance metrics
- `get_databases()` - List databases
- `get_database_info()` - Database details
- `search_genes()` - Gene search
- `get_gene_info()` - Gene details
- `get_layers()` - List layers
- `get_metadata_fields()` - List metadata fields
- `get_embeddings()` - List embeddings
- `get_cells()` - Retrieve cell data
- `get_embedding_data()` - Get embedding coordinates
- `execute_sql()` - Execute SQL queries
- `rescan_databases()` - Force rescan

### Requirements
- Python >= 3.8
- requests >= 2.25.0
- pandas >= 1.3.0 (optional)

## [Unreleased]

### Planned
- Async client support
- Batch operations
- Query builder utilities
- Additional export formats
- Enhanced error handling
- Retry logic with exponential backoff
