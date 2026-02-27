# anthive - database tools for scanpy h5ad files

A collection of command-line tools to validate, convert, and serve single-cell analysis data from h5ad files.

## Main Goals

- **Check** any scanpy h5ad file for consistency & errors
- **Convert** h5ad files to high-performance DuckDB databases for fast data access
- **Manage** high-level metadata for each h5ad file
- **Serve** data through a fast, lightweight REST API (future)
- **Query** data with a simple client library (future)

## Current Status

✅ **Implemented**: `ant check` and `ant convert` are fully working
🚧 **Planned**: REST API and client library

## Quick Start

```bash
# 1. Check and validate an h5ad file
ant check mydata.h5ad

# Creates: mydata.md (metadata with statistics)

# 2. (Optional) Review and edit mydata.md to adjust include/exclude lists

# 3. Convert to DuckDB database
ant convert mydata.h5ad

# Creates: mydata.duckdb (optimized database)
```

## Technology

### Command Structure

Git-like interface with subcommands:
- Main wrapper: `ant`
- Subcommands: `ant-check`, `ant-convert`, etc. in `./bin/`
- Discovers available commands automatically

### Implementation

- **Language**: Python with uv-shebangs for dependency management
- **Libraries**: anndata, pandas, numpy, scipy, duckdb, pyyaml
- **Versioning**: Strict version requirements for reproducibility

### Shared Libraries (`./lib/`)

- `config.py` - Configuration management
- `h5ad_utils.py` - H5AD file operations
- `metadata.py` - Metadata file management
- `checksum.py` - SHA256 checksums
- `layer_stats.py` - Layer statistics calculation

## Configuration

Configuration file: `anthive.yaml` (optional, uses defaults if missing)

### Key Parameters

- **`max_obscat_values`** = 100 - Maximum unique values for categorical columns
- **`melt_chunk_size`** = 10000 - Cells per chunk during layer conversion (memory vs speed)
- **`rest_api_host`** = localhost - REST API host (future)
- **`rest_api_port`** = 8080 - REST API port (future)

See `./doc/CONFIG.md` for complete documentation and `anthive.default.yaml` for all defaults.

## H5AD File Processing

### Data Extraction

- **Cell names**: From `adata.obs_names`, stored with integer IDs for efficiency
- **Gene names**: From `adata.var_names`, stored with integer IDs for efficiency
- **Original names**: Preserved in lookup tables (`__cells`, `__genes`)

### Automatic Filtering

- **High-cardinality categorical**: Columns with >100 unique values → excluded
- **High NA percentage**: Columns with >50% NA values → excluded
- **Underscore prefix**: Any column starting with `_` → excluded
- **Non-sparse layers**: Layers without 0 as most common value → excluded

### Included Data

- **Expression layers**: `.X` and/or named layers (at least one required)
- **Observations**: Numerical (`obsnum`) and categorical (`obscat`) cell annotations
- **Variables**: Numerical (`varnum`) and categorical (`varcat`) gene annotations
- **Embeddings**: UMAP, PCA, etc. from `.obsm`

## DuckDB Database Schema

### Design Principles

- **Raw tables**: Prefixed with `__` (e.g., `__layer__X`, `__cells`)
- **Views**: Clean interface without prefix (e.g., `layer__X`, `cells`)
- **Integer indices**: Cell and gene IDs are integers for space efficiency
- **Sparse storage**: Only non-zero values stored in expression layers
- **Schema versioning**: `__schema_version` table for future migrations

### Table Structure

**Expression data** (sparse, chunked processing):
- `__layer__X`, `__layer__counts`, etc. → `layer__X`, `layer__counts` views
- Columns: `cell_id` (int), `gene_id` (int), `value` (double)
- Zeros omitted, must be filled in when reading

**Cell/Gene lookups**:
- `__cells` → `cells` view: `cell_id` ↔ `cell_name`
- `__genes` → `genes` view: `gene_id` ↔ `gene_name`

**Observations** (cell metadata):
- `__obsnum` → `obsnum` view: Numerical columns as DOUBLE
- `__obscat` → `obscat` view: Categorical columns as VARCHAR

**Variables** (gene metadata):
- `__varnum` → `varnum` view: Numerical columns as DOUBLE
- `__varcat` → `varcat` view: Categorical columns as VARCHAR

**Embeddings**:
- `__obsm__Umap`, `__obsm__Pca`, etc. → `obsm__Umap`, `obsm__Pca` views
- Columns: `cell_id`, `dim_0`, `dim_1`, ... (dimensions as DOUBLE)

## ant check - Validate H5AD Files

Validates h5ad files and generates metadata files with statistics.

### What It Does

1. **Filename validation**: No spaces, special characters; max 50 chars
2. **SHA256 checksum**: Detects file changes on re-check
3. **Structure validation**: Requires .X or layers, .obs, .var
4. **Layer analysis**: Checks sparsity (0 as most common value)
5. **Column analysis**:
   - Categorizes as numerical/categorical
   - Checks cardinality (>100 unique → exclude)
   - Checks NA percentage (>50% → exclude)
   - Warns about low-cardinality numerical (consider as categorical)
6. **Embedding validation**: Shape and NA checks
7. **Statistics table**: Generates layer statistics (min, max, median, Q90, sparsity)
8. **Grouped reporting**: Similar issues grouped together for readability

### Output

Creates `<basename>.md` with:
- **YAML preamble**: Metadata fields (version, checksum, include/exclude lists)
- **Statistics table**: Layer statistics in markdown format
- **Description area**: For user notes

Creates `index.md` in directory:
- **Collection metadata**: Group name, experiment, authors, year

### Example Output

```
Checking: dataset.h5ad
✓ Filename valid
✓ File integrity OK
ℹ SHA256: a3f5c8b2... [NEW]
✓ Required structures present
  - .X: 50,000 cells × 20,000 genes
  - .obs: 50,000 rows, 15 columns
  - .var: 20,000 rows, 8 columns

Analysis Summary:
⚠ Non-sparse layers excluded (0 not most common): log1p_normalized
⚠ Obs columns with >50% NAs excluded: temp_col1 (78.3%), temp_col2 (91.2%)
ℹ Obs columns with NAs kept (<50%): batch (12.5%), donor (3.2%)
⚠ High-cardinality categorical obs columns excluded: barcode (50000 values)
ℹ Low-cardinality numerical obs columns (consider categorical): cluster (8 values)

Metadata file: dataset.md [CREATED]
Collection index: index.md [OK]

✓ Check completed successfully
```

## ant convert - Convert to DuckDB

Converts h5ad files to optimized DuckDB databases using metadata from `.md` files.

### Prerequisites

- Must run `ant check` first to generate `.md` file
- Sufficient disk space (recommend 2x h5ad file size)

### What It Does

1. **Loads metadata**: Reads configuration from `.md` file
2. **Creates database**: Initializes DuckDB with schema version
3. **Builds indices**: Cell and gene ID-to-name mappings
4. **Converts layers**: Chunked processing (configurable), sparse storage
5. **Converts observations**: Numerical and categorical cell metadata
6. **Converts variables**: Gene metadata
7. **Converts embeddings**: UMAP, PCA, etc.
8. **Optimizes**: VACUUM and ANALYZE for performance
9. **Verifies**: Sanity checks (cell/gene counts)

### Performance

- **Chunked processing**: Configurable via `melt_chunk_size`
- **Bulk inserts**: Uses DataFrame registration (10-100x faster than row-by-row)
- **Memory efficient**: Only one chunk in memory at a time
- **Typical compression**: 70-90% space savings vs h5ad

### Example Output

```
Converting: dataset.h5ad → dataset.duckdb

Initializing database...
  Creating schema version table...
✓ Schema version: 1
  Creating cell index table...
    Creating __cells table...
    Inserting 50,000 cells...
✓ Cell index: 50,000 cells
✓ Gene index: 20,000 genes

Converting layers...
  layer__X (X):
    Creating __layer__X table...
    Chunk 0-10,000 (20%)... 234,567 non-zero values, 8.2s
    Chunk 10,000-20,000 (40%)... 245,123 non-zero values, 8.1s
    ...
  ✓ Completed in 41.0s (1,199,701 values stored, 88.0% sparse)

Converting observations...
  ✓ obsnum: 5 columns (n_genes, n_counts, percent_mito, ...)
  ✓ obscat: 8 columns (cell_type, tissue, donor, ...)

Converting variables...
  ✓ varnum: 3 columns (n_cells, mean_counts, pct_dropout)

Converting embeddings...
  ✓ obsm__Umap: 2 dimensions
  ✓ obsm__Pca: 50 dimensions

Finalizing...
  Running VACUUM...
  Running ANALYZE...
✓ Optimizing database
✓ Verifying integrity

Conversion complete!
  Input: dataset.h5ad (2.4 GB)
  Output: dataset.duckdb (380 MB)
  Compression: 84%
  Total time: 1m 23s
```

## Metadata Files

### Per-File Metadata: `<basename>.md`

YAML preamble with configuration:

```yaml
---
title: Dataset One
version: 0.1.0
sha256: a3f5c8b2e1d4...
year: 2026
date: 2026-02-24
obsnum_include:
  - n_genes
  - percent_mito
obscat_include:
  - cell_type
  - tissue
obs_exclude:
  - barcode
  - _temp_column
layers_include:
  X: X
  counts: counts
layers_exclude: []
obsm_include:
  X_umap: Umap
  X_pca: Pca
obsm_exclude: []
---

# Dataset One

## Layer Statistics

| Layer | dtype | Min | Min (non-zero) | Max | Median | Q90 | Zeros |
|-------|-------|-----|----------------|-----|--------|-----|-------|
| X | float32 | 0 | 1.0 | 5,230.0 | 15.0 | 210.0 | 45,234,567 |
| counts | int32 | 0 | 1.0 | 8,450.0 | 20.0 | 350.0 | 43,123,456 |

Description of the dataset...
```

**Key fields**:
- `version`: Anthive version used
- `sha256`: File checksum for change detection
- `obsnum_include` / `obscat_include` / `obs_exclude`: Mutually exclusive lists
- `layers_include`: Layer name mappings (e.g., `X: X`, `counts: raw_counts`)
- `obsm_include`: Embedding name mappings (e.g., `X_umap: Umap`)

### Collection Metadata: `index.md`

```yaml
---
group: Mouse Brain Atlas
experiment: na
authors: []
year: 2026
---

# Mouse Brain Atlas
```

## Error Handling

**Fail-fast philosophy**: Better to crash with clear errors than leave corrupt data.

- **Missing prerequisites**: Exits if `.md` file missing before convert
- **Corrupted h5ad**: Exits with error details
- **Conversion failures**: Removes partial `.duckdb` file and exits
- **Invalid config**: Exits with specific parameter errors

## File Organization

```
project/
├── anthive.yaml              # Optional config (uses defaults if missing)
├── anthive.default.yaml      # Default configuration reference
├── bin/
│   ├── ant                   # Main wrapper
│   ├── ant-check             # Check command
│   └── ant-convert           # Convert command
├── lib/                      # Shared libraries
├── doc/                      # Documentation
├── data/
│   └── collection/
│       ├── index.md          # Collection metadata
│       ├── dataset1.h5ad     # Input data
│       ├── dataset1.md       # File metadata
│       ├── dataset1.duckdb   # Converted database
│       ├── dataset2.h5ad
│       ├── dataset2.md
│       └── dataset2.duckdb
```

## Documentation

Comprehensive documentation in `./doc/`:

- **CONFIG.md** - Configuration parameters and tuning
- **DATABASE_SCHEMA.md** - DuckDB schema details
- **METADATA.md** - Metadata file formats
- **CHECK_PROCESS.md** - Check command workflow
- **CONVERT_PROCESS.md** - Convert command workflow
- **PERFORMANCE.md** - Memory management and optimization
- **ERROR_HANDLING.md** - Error handling philosophy

## Future Features

- **REST API**: Fast, cached HTTP access to data
- **antclient**: Python/R client library for querying
- **Incremental updates**: Re-convert only changed portions
- **Multi-file queries**: Query across multiple datasets

## Development

**Version**: 0.1.0
**License**: TBD
**Requirements**: Python 3.9+, uv package manager
