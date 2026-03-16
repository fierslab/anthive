# Metadata Structure Changes

## Overview

The metadata structure for layers and embeddings has been simplified to avoid confusion between original keys (used in h5ad) and nice names (used for display).

## Changes

### Old Structure (Before)

```yaml
layers_include:
  X: X
  counts: counts
  spliced: Spliced

obsm_include:
  X_pca: Pca
  X_umap: Umap
  X_tsne: Tsne
```

**Problems:**
- Mixed original keys with nice names in dict values
- ant-convert used the VALUES (nice names) for table names
- This caused mismatches: database had `obsm__Pca` but validator expected `__obsm__X_pca`

### New Structure (After)

```yaml
layers_include:
  - X
  - counts
  - spliced

obsm_include:
  - X_pca
  - X_umap
  - X_tsne

layer_description:
  X: "Normalized expression"
  counts: "Raw counts"
  spliced: "Spliced counts"

embedding_description:
  X_pca: "Principal component analysis"
  X_umap: "UMAP embedding"
  X_tsne: "t-SNE embedding"

layer_stats:
  X:
    dtype: float32
    min_value: 0.0
    min_nonzero: 0.001
    max_value: 10.5
    median: 0.8
    q90: 2.3
    num_zeros: 15000000
    pct_zeros: 75.0
  counts:
    dtype: float32
    min_value: 0.0
    min_nonzero: 1.0
    max_value: 5000.0
    median: 50.0
    q90: 500.0
    num_zeros: 18000000
    pct_zeros: 90.0
```

**Benefits:**
- Clear separation between keys (used for table names) and descriptions (for display)
- `layers_include` and `obsm_include` are simple lists of original keys
- `layer_description` and `embedding_description` store user-friendly names/descriptions
- Table names always match original keys: `__layer__X`, `__obsm__X_pca`
- `layer_stats` stores comprehensive statistics for each layer in YAML (not markdown)

### Layer Statistics

The `layer_stats` field is automatically calculated and stored in the YAML metadata:

```yaml
layer_stats:
  X:
    dtype: float32           # Data type
    min_value: 0.0           # Minimum value (including zeros)
    min_nonzero: 0.001       # Minimum non-zero value
    max_value: 10.5          # Maximum value
    median: 0.8              # Median of non-zero values
    q90: 2.3                 # 90th percentile of non-zero values
    num_zeros: 15000000      # Number of zero values
    pct_zeros: 75.0          # Percentage of zeros
```

**Note:** Layer statistics are always recalculated when `ant h5ad-check` is run. They are not preserved during regeneration.

## Technical Details

### Database Table Naming

**Layer tables:**
- Pattern: `__layer__{key}`
- Examples: `__layer__X`, `__layer__counts`, `__layer__spliced`

**Embedding tables:**
- Pattern: `__obsm__{key}`
- Examples: `__obsm__X_pca`, `__obsm__X_umap`, `__obsm__X_tsne`

### Files Changed

1. **bin/ant-h5ad-check** - Generates metadata
   - Initialize `layers_include` as list (not dict)
   - Initialize `obsm_include` as list (not dict)
   - Add `layer_description` and `embedding_description` dicts
   - Update `preserve_user_customizations()` to preserve descriptions
   - Change from `ensure_in_mapping()` to `ensure_in_list()`

2. **bin/ant-convert** - Creates DuckDB tables
   - Iterate over `layers_include` list
   - Iterate over `obsm_include` list
   - Use original keys for table names
   - Use descriptions for display messages

3. **bin/ant-duckdb-check** - Validates database
   - Read `layers_include` as list
   - Read `obsm_include` as list
   - Validate table names using original keys

4. **bin/ant-serve** - REST API
   - Read `layers_include` list and `layer_description` dict
   - Read `obsm_include` list and `embedding_description` dict
   - Build API response with friendly names from descriptions

5. **antclient** - No changes needed
   - Consumes API response format (unchanged)

## Migration

### For Existing Metadata Files

When you run `ant h5ad-check` on an existing dataset, the tool will:
1. Detect the file has changed (SHA256 differs)
2. Force regeneration with new structure
3. Preserve user customizations:
   - Title and description fields
   - Layer descriptions (converted from old nice names)
   - Embedding descriptions (converted from old nice names)

### For Existing Databases

After updating metadata with `ant h5ad-check`:
1. Run `ant convert dataset.h5ad` to regenerate the database
   - Tables will now use correct names: `__layer__X`, `__obsm__X_pca`
2. Run `ant duckdb-check dataset.h5ad` to verify

## REST API Format

The REST API response format remains backward compatible:

```json
{
  "layers": {
    "X": "Normalized expression",
    "counts": "Raw counts"
  },
  "embeddings": {
    "X_pca": {
      "name": "Principal component analysis",
      "dimensions": 50
    },
    "X_umap": {
      "name": "UMAP embedding",
      "dimensions": 2
    }
  }
}
```

API clients (antclient, webapp) require no changes.

## Example Metadata File

Complete example of new `.md` metadata file:

```markdown
---
type: dataset
title: My Dataset
version: 0.4.0
date: 2026-03-02
sha256: abc123...
n_obs: 10000
n_vars: 2000

layers_include:
  - X
  - counts

layers_exclude:
  - raw

layer_description:
  X: "Normalized expression"
  counts: "Raw UMI counts"

obsnum_include:
  - n_genes
  - n_counts
  - percent_mito

obscat_include:
  - cell_type
  - tissue

obs_exclude:
  - batch

var_include:
  - highly_variable
  - mt

var_exclude: []

obsm_include:
  - X_pca
  - X_umap

obsm_exclude:
  - X_tsne

embedding_description:
  X_pca: "PCA (50 components)"
  X_umap: "UMAP embedding"

layer_stats:
  X:
    dtype: float32
    min_value: 0.0
    min_nonzero: 0.001
    max_value: 10.5
    median: 0.8
    q90: 2.3
    num_zeros: 15000000
    pct_zeros: 75.0
  counts:
    dtype: float32
    min_value: 0.0
    min_nonzero: 1.0
    max_value: 5000.0
    median: 50.0
    q90: 500.0
    num_zeros: 18000000
    pct_zeros: 90.0
---

# My Dataset

Description of the dataset...
```
