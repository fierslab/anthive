# H5AD Convert Process

The `ant convert` command converts h5ad files to optimized DuckDB databases using metadata from the `.md` file.

## Command Usage

```bash
ant convert <path/to/file.h5ad>
```

## Prerequisites

- h5ad file must have been checked with `ant check` first
- Corresponding `.md` file must exist
- Sufficient disk space (at least 2x h5ad file size recommended)

## Process Flow

### 1. Validation

#### Check Prerequisites
- Verify `.md` file exists
- Verify `.md` file has valid YAML preamble
- Verify h5ad file exists and is readable

**Action on failure**: Exit with error (fail fast)

#### Load Configuration
- Read `anthive.yaml` (or use defaults)
- Load metadata from `.md` file preamble
- Validate configuration parameters

**Action on invalid config**: Exit with error, reference `anthive.default.yaml`

### 2. Database Initialization

#### Create DuckDB File
Create `<basename>.duckdb` in same directory as h5ad file.

**If file already exists**:
- Prompt user for confirmation to overwrite
- Or use `--force` flag to skip prompt

#### Create Schema Version Table
```sql
CREATE TABLE __schema_version (
    version INTEGER PRIMARY KEY,
    applied_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

INSERT INTO __schema_version VALUES (1, CURRENT_TIMESTAMP, 'Initial schema');
```

### 3. Cell and Gene Reference Tables

#### Create Cell Index
```python
cell_names = adata.obs_names.tolist()

# Create mapping
cells_df = pd.DataFrame({
    'cell_id': range(len(cell_names)),
    'cell_name': cell_names
})

# Write to database
conn.execute("""
    CREATE TABLE __cells (
        cell_id INTEGER PRIMARY KEY,
        cell_name TEXT NOT NULL UNIQUE
    )
""")

conn.execute("INSERT INTO __cells VALUES (?, ?)", cells_df.values)
conn.execute("CREATE INDEX idx_cells_name ON __cells(cell_name)")

# Create view
conn.execute("CREATE VIEW cells AS SELECT * FROM __cells")
```

#### Create Gene Index
Same process for genes using `adata.var_names`.

### 4. Expression Layer Conversion

For each layer in `layers_include` (including .X):

#### Process in Chunks
```python
layer_name = 'X'  # or layer name
internal_name = metadata['layers_include'][layer_name]
chunk_size = config['melt_chunk_size']
num_cells = adata.n_obs
num_genes = adata.n_vars

# Create table
conn.execute(f"""
    CREATE TABLE __layer__{internal_name} (
        cell_id INTEGER NOT NULL,
        gene_id INTEGER NOT NULL,
        value FLOAT NOT NULL,
        PRIMARY KEY (cell_id, gene_id)
    )
""")

# Process in chunks
for chunk_start in range(0, num_cells, chunk_size):
    chunk_end = min(chunk_start + chunk_size, num_cells)

    # Progress reporting
    progress = (chunk_end / num_cells) * 100
    print(f"Processing {layer_name}: {progress:.1f}% "
          f"(cells {chunk_start}-{chunk_end})")

    # Load chunk
    if layer_name == 'X':
        chunk_data = adata.X[chunk_start:chunk_end, :]
    else:
        chunk_data = adata.layers[layer_name][chunk_start:chunk_end, :]

    # Convert to COO format if sparse
    if scipy.sparse.issparse(chunk_data):
        chunk_coo = chunk_data.tocoo()
    else:
        chunk_coo = scipy.sparse.coo_matrix(chunk_data)

    # Create dataframe with integer indices
    melted = pd.DataFrame({
        'cell_id': chunk_coo.row + chunk_start,  # Offset by chunk start
        'gene_id': chunk_coo.col,
        'value': chunk_coo.data
    })

    # Remove zeros (if any slipped through)
    melted = melted[melted['value'] != 0]

    # Write to database
    conn.executemany(
        f"INSERT INTO __layer__{internal_name} VALUES (?, ?, ?)",
        melted.values
    )

    # Free memory
    del chunk_data, chunk_coo, melted
    gc.collect()
```

#### Create Indices
After all chunks processed:
```sql
CREATE INDEX idx_layer_{name}_cell ON __layer__{name}(cell_id);
CREATE INDEX idx_layer_{name}_gene ON __layer__{name}(gene_id);
```

#### Create View
```sql
CREATE VIEW layer__{name} AS
SELECT
    c.cell_name,
    g.gene_name,
    l.value
FROM __layer__{name} l
JOIN __cells c ON l.cell_id = c.cell_id
JOIN __genes g ON l.gene_id = g.gene_id;
```

### 5. Observation Data Conversion

#### Numerical Columns (`obsnum`)
```python
obsnum_cols = metadata['obsnum_include']

if obsnum_cols:
    # Build CREATE TABLE statement dynamically
    col_defs = ['cell_id INTEGER PRIMARY KEY']
    for col in obsnum_cols:
        col_defs.append(f"{col} FLOAT")

    conn.execute(f"""
        CREATE TABLE __obsnum (
            {', '.join(col_defs)}
        )
    """)

    # Prepare data
    obsnum_df = adata.obs[obsnum_cols].copy()
    obsnum_df['cell_id'] = range(len(obsnum_df))

    # Reorder columns
    obsnum_df = obsnum_df[['cell_id'] + obsnum_cols]

    # Insert
    placeholders = ','.join(['?'] * len(obsnum_df.columns))
    conn.executemany(
        f"INSERT INTO __obsnum VALUES ({placeholders})",
        obsnum_df.values
    )

    # Create view
    conn.execute("""
        CREATE VIEW obsnum AS
        SELECT c.cell_name, o.*
        FROM __obsnum o
        JOIN __cells c ON o.cell_id = c.cell_id
    """)
```

#### Categorical Columns (`obscat`)
```python
obscat_cols = metadata['obscat_include']

if obscat_cols:
    # Build ENUM types
    col_defs = ['cell_id INTEGER PRIMARY KEY']
    for col in obscat_cols:
        # Get unique values for ENUM
        unique_values = adata.obs[col].unique()
        unique_values = [v for v in unique_values if pd.notna(v)]

        # Convert to string if numerical
        if adata.obs[col].dtype in ['int64', 'float64']:
            adata.obs[col] = adata.obs[col].astype(str)
            unique_values = [str(v) for v in unique_values]

        # Create ENUM definition
        enum_values = "', '".join(unique_values)
        col_defs.append(f"{col} ENUM('{enum_values}') DEFAULT NULL")

    conn.execute(f"""
        CREATE TABLE __obscat (
            {', '.join(col_defs)}
        )
    """)

    # Prepare and insert data (similar to obsnum)
    # ...

    # Create indices on frequently used categorical columns
    for col in obscat_cols:
        conn.execute(f"CREATE INDEX idx_obscat_{col} ON __obscat({col})")

    # Create view
    conn.execute("""
        CREATE VIEW obscat AS
        SELECT c.cell_name, o.*
        FROM __obscat o
        JOIN __cells c ON o.cell_id = c.cell_id
    """)
```

### 6. Variable Data Conversion

Apply same process as observation data for `var_include` columns.

### 7. Embedding Conversion

For each embedding in `obsm_include`:

```python
for obsm_key, internal_name in metadata['obsm_include'].items():
    embedding = adata.obsm[obsm_key]
    num_dims = embedding.shape[1]

    # Create table
    col_defs = ['cell_id INTEGER PRIMARY KEY']
    for i in range(num_dims):
        col_defs.append(f"dim_{i} FLOAT NOT NULL")

    conn.execute(f"""
        CREATE TABLE __obsm__{internal_name} (
            {', '.join(col_defs)}
        )
    """)

    # Prepare data
    embedding_df = pd.DataFrame(embedding)
    embedding_df.columns = [f'dim_{i}' for i in range(num_dims)]
    embedding_df['cell_id'] = range(len(embedding_df))

    # Reorder columns
    embedding_df = embedding_df[['cell_id'] +
                                 [f'dim_{i}' for i in range(num_dims)]]

    # Insert
    placeholders = ','.join(['?'] * len(embedding_df.columns))
    conn.executemany(
        f"INSERT INTO __obsm__{internal_name} VALUES ({placeholders})",
        embedding_df.values
    )

    # Create view
    conn.execute(f"""
        CREATE VIEW obsm__{internal_name} AS
        SELECT c.cell_name, e.*
        FROM __obsm__{internal_name} e
        JOIN __cells c ON e.cell_id = c.cell_id
    """)
```

### 8. Finalization

#### Optimize Database
```sql
VACUUM;
ANALYZE;
```

#### Verify Integrity
Basic sanity checks:
- Cell count matches
- Gene count matches
- Layers have expected data

### 9. Error Handling

**On any exception**:
```python
try:
    convert_h5ad(...)
except Exception as error:
    # Print detailed error
    print(f"\n{'='*60}")
    print(f"ERROR: Conversion failed")
    print(f"{'='*60}")
    print(f"File: {h5ad_path}")
    print(f"Error: {error}")
    print(f"Traceback: ...")

    # Clean up partial database
    duckdb_path = h5ad_path.replace('.h5ad', '.duckdb')
    if os.path.exists(duckdb_path):
        print(f"\nRemoving partial database: {duckdb_path}")
        os.remove(duckdb_path)

    sys.exit(1)
```

### 10. Output Summary

```
Converting: dataset1.h5ad → dataset1.duckdb

Initializing database...
✓ Schema version: 1
✓ Cell index: 50000 cells
✓ Gene index: 20000 genes

Converting layers...
  layer__X (counts):
    Chunk 0-10000 (20%)... 8.2s
    Chunk 10000-20000 (40%)... 8.1s
    Chunk 20000-30000 (60%)... 8.3s
    Chunk 30000-40000 (80%)... 8.2s
    Chunk 40000-50000 (100%)... 8.1s
  ✓ Completed in 41.0s

Converting observations...
  ✓ obsnum: 5 columns
  ✓ obscat: 8 columns

Converting variables...
  ✓ varnum: 3 columns
  ✓ varcat: 2 columns

Converting embeddings...
  ✓ obsm__Umap: 2 dimensions
  ✓ obsm__Pca: 50 dimensions

Finalizing...
  ✓ Optimizing database
  ✓ Verifying integrity

Conversion complete!
  Input: dataset1.h5ad (2.4 GB)
  Output: dataset1.duckdb (380 MB)
  Compression: 84%
  Total time: 1m 23s
```

## Exit Codes

- `0`: Conversion completed successfully
- `1`: Conversion failed (error during processing)
- `2`: Invalid command line arguments
- `3`: Missing prerequisites (.md file, h5ad file)
