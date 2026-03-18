# Changelog

All notable changes to the Anthive Client will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
