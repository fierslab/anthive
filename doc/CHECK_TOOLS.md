# Check Tools Split

## Overview

The checking functionality has been split into three separate tools for better modularity and control:

1. **ant-ant h5ad-check** - Validates h5ad files and generates metadata
2. **ant-ant duckdb-check** - Validates DuckDB database integrity
3. **ant-check** - Convenience wrapper that runs both (backward compatible)

## Which checker is safe to run where

| Tool | Writes | Safe on a live store or a submission |
| --- | --- | --- |
| `ant h5ad-check` | rewrites the dataset `.md` | no |
| `ant check` | runs `ant h5ad-check` as a subprocess, so it rewrites the `.md` too | no |
| `ant md-check` | writes missing ids unless `--no-fix` | only with `--no-fix` |
| `ant duckdb-check` | builds `__gene_bins` in the database, may write `default_layer` into the `.md`, appends to `./ant-<date>.log` in the working directory, and takes a write lock | no, run it on a copy |
| `ant ingest-check` | nothing in the submission or the store (scratch dir and the output files you name only) | yes, built for it |

`ant ingest-check` is the checker for a dataset that is not published yet or must not be touched
(`doc/DESIGN_dataset_ingestion.md`, section 8). It runs `ant duckdb-check` itself, on a private copy
in the scratch dir.

```bash
ant ingest-check /path/to/Group --dataset basename --store /data/h5adstore --json report.json
ant ingest-check /path/to/Group/basename.h5ad --no-convert --audience curator
ant ingest-check /path/to/Group --dataset basename --profile-only --json profile.json
ant ingest-index build        # overlap indexes (donors, cells, genes); first, and after the store changes
```

Exit code 0: no errors. 1: at least one failed check. 2: the tool itself failed. The checks, their ids
and what to do about each are in `doc/DATASET_INGESTION.md`.

## ant h5ad-check

**Purpose:** Validate h5ad file structure and generate/update metadata file.

**Usage:**
```bash
ant h5ad-check dataset.h5ad
```

**What it does:**
1. Validates filename format
2. Loads and validates h5ad structure
3. Calculates SHA256 checksum
4. Detects file changes and forces regeneration (preserving user customizations)
5. Analyzes layers (sparsity, statistics)
6. Categorizes observation columns (numerical/categorical)
7. Analyzes variable columns
8. Validates embeddings (shape, NA/inf values)
9. Creates/updates `dataset.md` metadata file
10. Checks/creates collection `index.md`

**Output:**
- `dataset.md` - YAML metadata + markdown description
- Exit code 0 (success) or 1 (failure)

**Does NOT:**
- Check DuckDB files
- Perform any database operations

**Example output:**
```
======================================================================
CHECKING: dataset.h5ad
======================================================================
File size: 2.1 GB

[1/9] Validating filename...
  ✓ Filename valid

[2/9] Loading h5ad file (2.1 GB)...
  ✓ Loaded successfully (50,000 cells × 2,000 genes)

[3/9] Calculating SHA256 checksum...
  ✓ SHA256: a1b2c3d4... [UNCHANGED]

[4/9] Validating structure...
  ✓ Required structures present

[5/9] Analyzing layers...
  ✓ Analyzed 3 layer(s)

[6/9] Analyzing observations...
  ✓ Analyzed 15 column(s)

[7/9] Analyzing variables...
  ✓ Analyzed 3 column(s)

[8/9] Analyzing embeddings...
  ✓ Found 2 embedding(s)

[9/9] Writing metadata...
  ✓ Metadata file UPDATED: dataset.md

======================================================================
✓ H5AD CHECK COMPLETED SUCCESSFULLY
======================================================================

Next steps:
  - To check DuckDB: ant duckdb-check dataset.h5ad
  - To convert to DuckDB: ant-convert dataset.h5ad
```

## ant duckdb-check

**Purpose:** Validate DuckDB database integrity against metadata.

**Usage:**
```bash
# Using h5ad file (finds matching .duckdb and .md)
ant duckdb-check dataset.h5ad

# Using duckdb file directly (finds matching .md)
ant duckdb-check dataset.duckdb

# With WAL cleanup
ant duckdb-check dataset.duckdb --clean-wal

# Verbose mode (shows orphaned tables)
ant duckdb-check dataset.duckdb --verbose

# Deep data integrity checks (loads h5ad, compares actual values)
ant duckdb-check dataset.h5ad --deep
```

**What it does:**
1. Reads metadata from `dataset.md`
2. Opens DuckDB database
3. Checks schema version
4. Validates core tables (`__cells`, `__genes`) exist with correct row counts
5. Checks layer tables exist (one per layer in `layers_include`)
6. Checks obs tables exist (one per column in `obsnum_include`/`obscat_include`)
7. Checks var tables exist (one per column in `var_include`)
8. Checks embedding tables exist (one per embedding in `obsm_include`)
9. Validates table row counts match expected values
10. Checks required views exist (`cells`, `genes`)
11. Optionally finds orphaned tables (--verbose)
12. Optionally cleans up WAL files (--clean-wal)
13. **Deep checks (--deep):** Loads h5ad file and compares actual data values:
    - Samples ~100 genes and checks expression values match
    - Samples ~100 cells and checks obs table values match
    - Samples ~100 cells and checks embedding coordinates match

**Exit codes:**
- 0: Success (database healthy)
- 1: Errors found (missing tables, size mismatches)
- 3: File not found

**Example output:**
```
======================================================================
CHECKING: dataset.duckdb
======================================================================

Database size: 1.2 GB

[1/9] Loading metadata...
  ✓ Loaded from dataset.md
    - Expected cells: 50,000
    - Expected genes: 2,000

[2/9] Opening database...
  ✓ Connected successfully

[3/9] Checking schema...
  ✓ Schema version: 1

[4/9] Checking core tables...
  ✓ Core tables validated

[5/9] Checking layer tables...
  ✓ Checked 3 layer(s)

[6/9] Checking observation tables...
  ✓ Checked 15 obs column(s)

[7/9] Checking variable tables...
  ✓ Checked 3 var column(s)

[8/9] Checking embedding tables...
  ✓ Checked 2 embedding(s)

[9/9] Checking views...
  ✓ Found 2 view(s)

  ✓ Integrity checks passed

Check Summary:

⚠ Orphaned tables (not in metadata):
  - layer_old_counts
  - obs_deprecated_column

======================================================================
⚠ CHECK COMPLETED - Warnings found
======================================================================

Database is functional but has warnings.
```

### Deep Data Integrity Checks

The `--deep` flag enables extensive data validation by loading the h5ad file and comparing actual values:

```bash
ant duckdb-check dataset.h5ad --deep
```

**What deep checks do:**
1. Load the h5ad file into memory
2. Sample ~100 random genes from each layer
3. For each sampled gene, compare all cell expression values between h5ad and DuckDB
4. Sample ~100 random cells and compare obs table values (numerical and categorical)
5. Sample ~100 random cells and compare embedding coordinates
6. Report any mismatches found

**When to use:**
- After conversion to verify database accuracy
- When debugging data discrepancies
- Before deploying to production
- After major updates to conversion code

**Performance:**
- Slower than regular checks (loads h5ad file)
- Takes ~30 seconds for 10k cells × 2k genes
- Sampling ensures reasonable runtime even for large datasets

**Example output:**
```
======================================================================
DEEP DATA INTEGRITY CHECKS
======================================================================

Loading h5ad file: dataset.h5ad
  ✓ Loaded (10,000 cells × 2,000 genes)

[Deep Check] Verifying layer expression values...
  ✓ Layer X: 100 genes checked, all match
  ✓ Layer counts: 100 genes checked, all match

[Deep Check] Verifying obs table values...
  ✓ Obs numerical: 300 values checked, all match
  ✓ Obs categorical: 200 values checked, all match

[Deep Check] Verifying embedding coordinates...
  ✓ Embedding X_pca: 100 cells checked, all match
  ✓ Embedding X_umap: 100 cells checked, all match

✓ Deep checks passed (6 tests)
```

## ant-check (Wrapper)

**Purpose:** Convenience wrapper that runs both ant h5ad-check and ant duckdb-check.

**Usage:**
```bash
# Run both checks
ant-check dataset.h5ad

# Skip DuckDB check
ant-check dataset.h5ad --skip-duckdb

# Only check DuckDB (skip h5ad)
ant-check dataset.h5ad --skip-h5ad
```

**What it does:**
1. Runs `ant h5ad-check dataset.h5ad`
2. Runs `ant duckdb-check dataset.h5ad` (if DuckDB exists)
3. Reports combined status

**Backward compatibility:**
This tool maintains compatibility with the old `ant-check` behavior.

**Example output:**
```
======================================================================
STEP 1: H5AD CHECK
======================================================================

[... ant h5ad-check output ...]

======================================================================
STEP 2: DUCKDB CHECK
======================================================================

[... ant duckdb-check output ...]

======================================================================
✓ ALL CHECKS COMPLETED SUCCESSFULLY
======================================================================
```

## When to Use Each Tool

### Use ant h5ad-check when:
- ✓ You've modified an h5ad file
- ✓ You want to update metadata
- ✓ You're checking file structure before conversion
- ✓ You don't care about DuckDB yet

### Use ant duckdb-check when:
- ✓ You've run ant-convert
- ✓ You want to verify database integrity
- ✓ You're debugging conversion issues
- ✓ You want to find orphaned tables
- ✓ You need to clean up WAL files

### Use ant-check when:
- ✓ You want to check everything at once
- ✓ You're doing routine validation
- ✓ You want backward-compatible behavior

## Integration with Other Tools

### ant-convert
After running `ant-convert`, you should run `ant duckdb-check` to verify:
```bash
ant-convert dataset.h5ad
ant duckdb-check dataset.h5ad
```

### ant-build-all
Could be updated to run:
1. `ant h5ad-check` before each conversion
2. `ant duckdb-check` after each conversion
3. Skip files with check failures

## Validation Checks

### ant h5ad-check validates:
- ✓ Filename format (no special characters)
- ✓ File can be loaded
- ✓ Required structures exist (.X, .obs, .var, .obsm, .layers)
- ✓ Layer sparsity (excludes non-sparse layers)
- ✓ Observation columns (NA%, cardinality, type)
- ✓ Variable columns (NA%)
- ✓ Embedding shapes and dimensions
- ✓ Metadata consistency

### ant duckdb-check validates:
- ✓ Database can be opened
- ✓ Schema version is correct
- ✓ Core tables exist (__schema_version, __cells, __genes)
- ✓ Core table sizes match expected (n_obs, n_vars)
- ✓ Layer tables exist for each included layer
- ✓ Obs tables exist for each included obs column
- ✓ Var tables exist for each included var column
- ✓ Embedding tables exist for each included embedding
- ✓ All table sizes match expected row counts
- ✓ Required views exist (cells, genes)
- ✓ Database integrity (can query tables)
- ✓ No orphaned tables (optional, --verbose)

## Error Handling

### ant h5ad-check errors:
- File not found → Exit 3
- Invalid filename → Exit 1
- Load failed → Exit 1
- Structure invalid → Exit 1
- Metadata write failed → Exit 1

### ant duckdb-check errors:
- File not found → Exit 3
- Metadata not found → Exit 3
- Database can't open → Exit 1
- Missing tables → Exit 1
- Size mismatches → Exit 1
- Integrity check failed → Exit 1

### Warnings (don't fail):
- Orphaned tables
- Missing views
- Schema version mismatch
- NA/inf values in embeddings

## Future Enhancements

### Possible additions to ant duckdb-check:
- `--repair` flag to fix common issues
- `--clean-orphaned` to remove unreferenced tables
- `--rebuild-indexes` to recreate indexes
- `--stats` to show detailed size/compression statistics
- Compare sample data checksums with h5ad
- Verify compression ratios
- Check query performance
- Validate foreign key relationships

### Possible additions to ant h5ad-check:
- `--quick` mode (skip expensive checks)
- `--strict` mode (fail on warnings)
- `--format` to auto-fix common issues
- Validate against schema specifications
