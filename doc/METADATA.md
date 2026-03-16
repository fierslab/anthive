# Metadata Management

Anthive uses markdown files with YAML preambles to store human-readable and machine-processable metadata about h5ad files and collections.

## Per-File Metadata: `<basename>.md`

Each h5ad file has a corresponding `.md` file (e.g., `dataset1.h5ad` → `dataset1.md`).

### YAML Preamble Fields

The YAML preamble is delimited by `---` markers:

```markdown
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
  - internal_id
  - _temp_column
var_include:
  - highly_variable
var_exclude:
  - _temp_var
layers_include:
  X: counts
  normalized: normalized_counts
layers_exclude:
  - _temp_layer
obsm_include:
  X_umap: Umap
  X_pca: Pca
obsm_exclude: []
---

# Dataset One

Description of the dataset goes here...
```

### Field Descriptions

#### `title`
- **Type**: string
- **Required**: yes
- **Default**: basename of h5ad file
- **Description**: Short, user-friendly title for API display

#### `version`
- **Type**: string (semantic version)
- **Required**: yes
- **Default**: current anthive version
- **Description**: Version of anthive that last checked/converted this file
- **Format**: `major.minor.patch` (e.g., `0.1.0`)

#### `sha256`
- **Type**: string (64 hex characters)
- **Required**: yes
- **Default**: computed on first check
- **Description**: SHA256 checksum of the h5ad file
- **Usage**: On re-check, verify file hasn't changed. If changed, update checksum and report to user.

#### `year`
- **Type**: integer
- **Required**: yes
- **Default**: current year
- **Description**: Year this file was first checked

#### `date`
- **Type**: string (ISO 8601)
- **Required**: yes
- **Default**: current date
- **Description**: Last date this file was checked
- **Format**: `YYYY-MM-DD`

#### `obsnum_include`
- **Type**: list of strings
- **Required**: no
- **Description**: Observation columns to include in `__obsnum` table (numerical data)
- **Mutually exclusive with**: `obscat_include`, `obs_exclude` for each column

#### `obscat_include`
- **Type**: list of strings
- **Required**: no
- **Description**: Observation columns to include in `__obscat` table (categorical data)
- **Note**: If column is numerical, convert to string before storage
- **Mutually exclusive with**: `obsnum_include`, `obs_exclude` for each column

#### `obs_exclude`
- **Type**: list of strings
- **Required**: no
- **Description**: Observation columns to exclude from database
- **Mutually exclusive with**: `obsnum_include`, `obscat_include` for each column

**Important**: Each obs column can appear in ONLY ONE of these three lists.

#### `var_include` / `var_exclude`
- **Type**: list of strings
- **Required**: no
- **Description**: Variable (gene) columns to include/exclude

#### `layers_include`
- **Type**: mapping (dict)
- **Required**: no
- **Description**: Layers to include with optional name mapping
- **Example**: `{'X': 'X', 'counts': 'counts', 'log1p': 'log_normalized'}`
- **Default**: If layer found but not in mapping, defaults to original name (e.g., 'X': 'X', 'counts': 'counts')

#### `layers_exclude`
- **Type**: list of strings
- **Required**: no
- **Description**: Layer names to exclude from conversion

#### `obsm_include`
- **Type**: mapping (dict)
- **Required**: no
- **Description**: Embeddings to include with optional name mapping
- **Example**: `{'X_umap': 'Umap', 'X_pca': 'Pca'}`
- **Default**: Capitalize name and remove `X_` prefix if present

#### `obsm_exclude`
- **Type**: list of strings
- **Required**: no
- **Description**: Embedding names to exclude from conversion

### Metadata Update Behavior

When running `ant check`:

1. **File exists**:
   - Read existing preamble
   - DO NOT overwrite user-modified fields
   - Add missing fields with defaults
   - Update `date` to current date
   - Verify `sha256` and warn if changed

2. **File does not exist**:
   - Create new .md file
   - Populate all fields with detected/default values
   - Create markdown body with title as header

3. **List/mapping fields**:
   - Add newly discovered columns/layers/embeddings
   - DO NOT remove or reorder existing entries
   - Respect user choices in include/exclude lists

## Collection Metadata: `index.md`

Each directory containing h5ad files has an `index.md` for collection-level metadata.

### YAML Preamble Fields

```markdown
---
group: Mouse Brain Atlas
experiment: na
authors: []
year: 2026
---

# Mouse Brain Atlas

Collection description...
```

#### `group`
- **Type**: string
- **Required**: yes
- **Default**: name of the directory
- **Description**: Human-readable name for this collection of datasets

#### `experiment`
- **Type**: string
- **Required**: yes
- **Default**: `na`
- **Description**: Subgroup or experiment name (if applicable)

#### `authors`
- **Type**: list of strings
- **Required**: yes
- **Default**: `[]` (empty list)
- **Description**: List of authors/contributors for this collection

#### `year`
- **Type**: integer
- **Required**: yes
- **Default**: current year
- **Description**: Year this collection was added to anthive

### Update Behavior

- Create `index.md` if it doesn't exist
- NEVER overwrite existing `index.md`
- User is responsible for updating collection metadata manually
