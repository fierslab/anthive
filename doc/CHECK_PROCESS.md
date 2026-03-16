# H5AD Check Process

The `ant check` command validates h5ad files and generates/updates metadata files.

## Command Usage

```bash
ant check <path/to/file.h5ad>
```

## Process Flow

### 1. File Validation

Check the h5ad file meets basic requirements:

#### Filename Sanity
- No spaces in filename
- No special characters (only alphanumeric, dash, underscore, dot)
- Basename (without extension) ≤ 50 characters

**Action on failure**: Exit with error, provide corrected filename suggestion

#### File Integrity
- File can be opened by anndata
- Not corrupted or truncated

**Action on failure**: Exit with error message about corruption

### 2. SHA256 Checksum

Calculate SHA256 checksum of the h5ad file.

**If .md file exists**:
- Compare with stored `sha256` field
- If different: warn user that file has changed since last check
- Update `sha256` and `date` fields

**If .md file does not exist**:
- Store calculated checksum in new .md file

### 3. Structure Validation

Verify required h5ad components exist:

#### Required Components
- `.X` (main expression matrix) **OR** `.layers` (alternative expression matrices)
  - At least one must be present
- `.obs` (observation/cell annotations)
- `.var` (variable/gene annotations)

**Action if missing**: Exit with error listing missing components

#### Optional Components
- `.layers` (additional expression matrices)
- `.obsm` (embeddings like UMAP, PCA)
- `.varm` (gene embeddings)
- `.uns` (unstructured metadata)

**Action if missing**: Note in output, continue processing

### 4. Layer Validation

For `.X` and each layer:

#### Check Zero Dominance
Verify that 0 is the most common value (indicating sparse data suitable for storage).

```python
value_counts = layer.data.value_counts()
most_common_value = value_counts.index[0]

if most_common_value != 0:
    warn(f"Layer '{layer_name}' does not have 0 as most common value")
    warn(f"Most common value: {most_common_value}")
    add_to_layers_exclude(layer_name)
```

**Reasoning**: Layers without zero dominance (e.g., log-normalized data) don't compress well with our sparse storage strategy.

#### Check for Reasonable Value Range
Warn if values are outside expected range for count data:
- Contains negative values
- Contains NaN or infinite values

**Action**: Warn user, suggest adding to `layers_exclude`

### 5. Observation (.obs) Analysis

For each column in `.obs`:

#### Identify Column Type
- Numerical: dtype is int or float
- Categorical: dtype is category, object, or string

#### Check for Underscore Prefix
Any column starting with `_` is automatically added to `obs_exclude`.

**Reasoning**: Underscore prefix conventionally indicates internal/temporary data

#### Numerical Columns with Few Values
Warn if numerical column has ≤ 10 unique values:

```python
if column.dtype in ['int64', 'float64']:
    unique_count = column.nunique()
    if unique_count <= 10:
        warn(f"Column '{col_name}' has only {unique_count} unique values")
        warn(f"Consider adding to obscat_include for categorical treatment")
```

**Action**: Warn only, do not auto-exclude

#### High-Cardinality Categorical
If categorical column has > `max_obscat_values` unique values:

```python
if column.dtype in ['category', 'object']:
    unique_count = column.nunique()
    if unique_count > config['max_obscat_values']:
        warn(f"Column '{col_name}' has {unique_count} unique values")
        warn(f"Exceeds max_obscat_values ({config['max_obscat_values']})")
        add_to_obs_exclude(col_name)
```

**Action**: Automatically add to `obs_exclude`

#### Check for NA Values
Warn if column contains any NA/null values:

```python
if column.isna().any():
    na_count = column.isna().sum()
    warn(f"Column '{col_name}' has {na_count} NA values")
```

**Action**: Warn only, do not exclude

### 6. Variable (.var) Analysis

Apply same checks as for `.obs`:
- Exclude underscore-prefixed columns
- Warn about NA values
- Handle high-cardinality categoricals

### 7. Embedding (.obsm) Analysis

For each embedding:

#### Validate Shape
- Must be 2D array
- Number of rows must match number of cells

```python
if embedding.shape[0] != adata.n_obs:
    error(f"Embedding '{name}' has {embedding.shape[0]} rows")
    error(f"Expected {adata.n_obs} rows (number of cells)")
    add_to_obsm_exclude(name)
```

#### Check for NA Values
Warn if embedding contains NA/infinite values.

#### Suggest Friendly Names
If embedding name starts with `X_`, suggest capitalized version without prefix:
- `X_umap` → `Umap`
- `X_pca` → `Pca`

### 8. Metadata File Generation/Update

#### Read Existing .md File
If exists, parse YAML preamble and preserve:
- User-modified field values
- Custom ordering in lists
- User comments in markdown body

#### Update/Add Fields

**Always update**:
- `date`: current date
- `sha256`: current checksum (if changed)
- `version`: current anthive version

**Add if missing**:
- `title`: default to basename
- `year`: current year
- All include/exclude lists

**Add to lists**:
- Newly discovered columns/layers/embeddings
- Items flagged for exclusion during validation

**Never remove from lists**:
- User-selected inclusions/exclusions

#### Write .md File
Format:
```markdown
---
title: dataset_name
version: 0.1.0
sha256: abc123...
year: 2026
date: 2026-02-24
obsnum_include:
  - col1
  - col2
obscat_include:
  - cell_type
obs_exclude:
  - _temp
...
---

# dataset_name

[User description preserved here]
```

### 9. Folder Index Update

Check if `index.md` exists in the same directory:

**If not exists**:
- Create with default values
- `group`: directory name
- `experiment`: "na"
- `authors`: []
- `year`: current year

**If exists**:
- Leave unchanged (user responsibility to update)

### 10. Output Summary

Print check summary:
```
Checking: dataset1.h5ad
✓ Filename valid
✓ File integrity OK
ℹ SHA256: abc123... [UNCHANGED / CHANGED]
✓ Required structures present
  - .X: 50000 cells × 20000 genes
  - .obs: 50000 rows, 15 columns
  - .var: 20000 rows, 8 columns
  - .obsm: 2 embeddings (umap, pca)
  - .layers: 2 layers (counts, normalized)

Warnings:
⚠ obs column 'barcode' has 250 unique values (>100)
  → Added to obs_exclude
⚠ obs column 'n_counts' has 8 unique values
  → Consider adding to obscat_include
⚠ layer 'log1p' does not have 0 as most common value
  → Added to layers_exclude

Metadata file: dataset1.md [CREATED / UPDATED]
Collection index: index.md [OK]
```

## Exit Codes

- `0`: Check completed successfully (warnings allowed)
- `1`: Check failed (corrupt file, missing required structures)
- `2`: Invalid command line arguments
- `3`: File not found
