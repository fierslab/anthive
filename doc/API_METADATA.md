# API Metadata Response Format

## Overview

The `/databases/{db_id}/info` endpoint now returns enhanced metadata with friendly names and better organization.

## Response Structure

```json
{
  "title": "Dataset Name",
  "n_cells": 12345,
  "n_genes": 2000,

  "layers": {
    "X": "Normalized expression",
    "counts": "Raw counts"
  },

  "embeddings": {
    "X_umap": {
      "name": "Umap",
      "dimensions": 2
    },
    "X_pca": {
      "name": "Pca",
      "dimensions": 50
    }
  },

  "obs": {
    "numerical": ["n_genes", "n_counts", "percent_mito"],
    "categorical": ["cell_type", "sample", "condition"],
    "total": 6
  },

  "var": {
    "columns": ["highly_variable", "gene_score"],
    "total": 2
  },

  "version": "1.0.0",
  "sha256": "abc123...",
  "date": "2026-02-27",
  "year": 2025,

  "collection": {
    "group": "Sierksma2025",
    "experiment": "Kidney transplant study",
    "authors": ["Sierksma M", "et al"],
    "year": 2025
  }
}
```

## Field Details

### Core Metadata

- **title** (string): Dataset name/title
- **n_cells** (int): Total number of cells
- **n_genes** (int): Total number of genes

### Layers

Dictionary mapping layer keys to friendly names:
```json
{
  "X": "Normalized expression",
  "counts": "Raw counts"
}
```

- **Key** (string): Layer key used in API calls (e.g., `"X"`, `"counts"`)
- **Value** (string): Friendly name or description

**Source**: Friendly names from `layers_include` in `.md` file YAML preamble.

**Fallback**: If no friendly name, uses the key as value.

### Embeddings

Dictionary mapping embedding keys to metadata objects:
```json
{
  "X_umap": {
    "name": "Umap",
    "dimensions": 2
  },
  "X_pca": {
    "name": "Pca",
    "dimensions": 50
  }
}
```

Each embedding object contains:
- **name** (string): Friendly display name
- **dimensions** (int): Number of dimensions in the embedding

**Source**: Friendly names from `obsm_include` in `.md` file YAML preamble.

**Fallback**: If no friendly name, capitalizes key with `X_` prefix removed.

### Observations (obs)

Metadata about observation (cell) columns:

- **numerical** (array): List of numerical column names (from `obsnum` table)
- **categorical** (array): List of categorical column names (from `obscat` table)
- **total** (int): Total count of obs columns

**Examples of obs columns**:
- Numerical: `n_genes`, `n_counts`, `percent_mito`, `doublet_score`
- Categorical: `cell_type`, `sample`, `condition`, `cluster`

### Variables (var)

Metadata about gene/variable columns:

- **columns** (array): List of var column names (from `var` table)
- **total** (int): Total count of var columns

**Examples of var columns**:
- `highly_variable`, `gene_score`, `dispersion`

### File Metadata

- **version** (string): Anthive version used to process the file
- **sha256** (string): SHA256 checksum of source `.h5ad` file
- **date** (string): Date of last check (YYYY-MM-DD format)
- **year** (int): Publication year

### Collection Metadata

Present if the dataset has an `index.md` file in its parent directory:

- **group** (string): Collection/study identifier
- **experiment** (string): Experiment description
- **authors** (array): List of authors
- **year** (int): Publication year

## Changes from Previous Version

### Before

```json
{
  "n_cells": 12345,
  "n_genes": 2000,
  "layers": ["X", "counts"],
  "embeddings": ["X_umap", "X_pca"],
  "metadata_fields": {
    "numerical": ["n_genes", "n_counts"],
    "categorical": ["cell_type", "sample"]
  },
  "title": "Dataset Name",
  "group_metadata": {
    "group": "Study2025"
  }
}
```

### After

```json
{
  "title": "Dataset Name",
  "n_cells": 12345,
  "n_genes": 2000,
  "layers": {
    "X": "Normalized expression",
    "counts": "Raw counts"
  },
  "embeddings": {
    "X_umap": {"name": "Umap", "dimensions": 2},
    "X_pca": {"name": "Pca", "dimensions": 50}
  },
  "obs": {
    "numerical": ["n_genes", "n_counts"],
    "categorical": ["cell_type", "sample"],
    "total": 4
  },
  "var": {
    "columns": ["highly_variable"],
    "total": 1
  },
  "collection": {
    "group": "Study2025"
  }
}
```

### Key Improvements

✅ **Friendly names** for layers and embeddings
✅ **Dimensions** included for embeddings
✅ **Better organization** - `obs` and `var` are grouped
✅ **Var columns** now included (was missing before)
✅ **Renamed** `group_metadata` → `collection` (clearer)
✅ **Renamed** `metadata_fields` → `obs` (clearer)
✅ **Title first** - more logical field ordering

## Usage Examples

### Get database info

```bash
curl http://localhost:8080/databases/Sierksma2025/dataset/info
```

### Extract specific fields with jq

```bash
# Get all obs columns
curl -s http://localhost:8080/databases/Sierksma2025/dataset/info | jq '.obs'

# Get layer keys and names
curl -s http://localhost:8080/databases/Sierksma2025/dataset/info | jq '.layers'

# Get embedding dimensions
curl -s http://localhost:8080/databases/Sierksma2025/dataset/info | jq '.embeddings'

# Check if specific column exists
curl -s http://localhost:8080/databases/Sierksma2025/dataset/info | \
  jq '.obs.categorical[] | select(. == "cell_type")'
```

### Python with pyvark

The `vark` Python client (`codeberg.org/mfiers/pyvark`) will
automatically use this enhanced metadata:

```python
from vark import AnthiveClient

client = AnthiveClient()

# Get database info
info = client.get_info('Sierksma2025/dataset')

# Access layers
for key, name in info['layers'].items():
    print(f"{key}: {name}")

# Check available embeddings
for key, emb in info['embeddings'].items():
    print(f"{key} → {emb['name']} ({emb['dimensions']}D)")

# List obs columns
print("Numerical:", info['obs']['numerical'])
print("Categorical:", info['obs']['categorical'])
```

## Backward Compatibility

The response structure has changed, so clients using the old format need updates:

**Old code (breaks):**
```python
layers = info['layers']  # Was: ["X", "counts"]
# Now: {"X": "Normalized expression", ...}
```

**New code:**
```python
layer_keys = list(info['layers'].keys())
layer_names = list(info['layers'].values())
```

**Old code (breaks):**
```python
obs_cols = info['metadata_fields']['numerical']
```

**New code:**
```python
obs_cols = info['obs']['numerical']
```

## Setting Friendly Names

Use `ant md` to set friendly names in metadata files:

```bash
# Set layer descriptions
ant md set layers_include='{"X": "Normalized", "counts": "Raw"}' dataset.md

# Set embedding names
ant md set obsm_include='{"X_umap": "Umap", "X_pca": "PCA"}' dataset.md
```

Or edit the `.md` file directly:

```yaml
---
layers_include:
  X: Normalized expression
  counts: Raw counts
obsm_include:
  X_umap: Umap
  X_pca: PCA
---
```

## License

BSD-3-Clause
