# DuckDB Database Schema

## Overview

Each h5ad file is converted to a single DuckDB database file with the same basename.
Tables use a `__prefix` naming convention for raw storage, with views providing the clean interface.

## Schema Version Management

### `__schema_version` table

Tracks the database schema version for migration support.

```sql
CREATE TABLE __schema_version (
    version INTEGER PRIMARY KEY,
    applied_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);
```

Current version: 1

## Core Reference Tables

### `__cells` table

Maps cell indices to original cell names from `adata.obs_names`.

```sql
CREATE TABLE __cells (
    cell_id INTEGER PRIMARY KEY,
    cell_name TEXT NOT NULL UNIQUE
);

```

**View**:
```sql
CREATE VIEW cells AS SELECT * FROM __cells;
```

### `__genes` table

Maps gene indices to original gene names from `adata.var_names`.

```sql
CREATE TABLE __genes (
    gene_id INTEGER PRIMARY KEY,
    gene_name TEXT NOT NULL UNIQUE
);

```

**View**:
```sql
CREATE VIEW genes AS SELECT * FROM __genes;
```

## Expression Data (Layers)

All expression layers (including .X) are stored in melted (long) format with zeros omitted.
This provides significant space savings for sparse single-cell data.

### Layer tables: `__layer__<name>`

Template for each layer (e.g., `__layer__X`, `__layer__counts`):

```sql
CREATE TABLE __layer__<name> (
    cell_id INTEGER NOT NULL,
    gene_id INTEGER NOT NULL,
    value FLOAT NOT NULL,
    PRIMARY KEY (cell_id, gene_id)
);

CREATE INDEX idx_layer_<name>_gene ON __layer__<name>(gene_id);
CREATE INDEX idx_layer_<name>_cell ON __layer__<name>(cell_id);
```

**View**:
```sql
CREATE VIEW layer__<name> AS
SELECT
    c.cell_name,
    g.gene_name,
    l.value
FROM __layer__<name> l
JOIN __cells c ON l.cell_id = c.cell_id
JOIN __genes g ON l.gene_id = g.gene_id;
```

**Note**: When reading expression data, zeros must be filled in for any (cell, gene) pairs not present in the table.

### Parquet sidecars (current layout)

Since the 2026-07 migration, `ant-convert` (and `ant-migrate-to-parquet` for
older files) no longer keeps layer values inside the `.duckdb`. Each layer is
written twice next to it, as `<stem>.layer.<name>.by_gene.parquet` (sorted by
gene_id) and `<stem>.layer.<name>.by_cell.parquet` (sorted by cell_id), each
with `(cell_id, gene_id, value)`. The `.duckdb` still gets three views per
layer: `layer__<name>` and `layer__<name>_by_gene` over the by_gene file,
`layer__<name>_by_cell` over the by_cell file. All three expose
`(cell_name, gene_name, value)`.

DuckDB resolves a relative `read_parquet` path against the process working
directory, not the database file, so these views have to name the sidecar
by absolute path. That path belongs to the host and folder that converted
the dataset. Copy the store to another host, sync it to a site with a
different store path, or rename a group folder (SerranoPozo2024 became
Wachter2024 in the dev store), and the views point at files that are not
there.

We keep writing the views. People who open a `.duckdb` directly in DuckDB,
or with a notebook on the host that built it, rely on `FROM layer__X`, and
`AnthiveDB._get_layers` uses the views to list the layers. Served code never
reads through them, though. Every query path resolves the sidecar at query
time from the `.duckdb` file's own location and current stem
(`lib/anthive_db.py`, `layer_sidecar_path` and the `AnthiveDB._layer_source_*`
helpers). The resolved path is not symlink-resolved, so it stays inside the
directories ant-serve already allows for the dataset (its own folder and the
folder of its symlink target). The hardening (`enable_external_access =
false`, `allowed_directories`) is unchanged.

Hand-written SQL (`/query/sql`, the analytics sandbox, `ant-duckdb-check`)
may still say `FROM layer__X`. For those, `install_portable_layer_views`
creates TEMP views on the request's own connection with the same SQL but
the locally resolved sidecar paths. TEMP names win over the attached
database's names, the TEMP views die with the connection, and nothing is
written to the `.duckdb`. A view whose sidecar is not found locally is left
alone. Remote `s3://` datasets resolve to the sibling object in the same
bucket folder.

Rule for new code: never `FROM layer__<name>` in a served path. Use
`db._layer_source_for_gene_filter(layer)` for `(cell_id, gene_id, value)`
or `db._layer_source_with_names(layer)` for `(cell_name, gene_name, value)`.
`tests/test_layer_path_portability.py` moves a converted dataset to another
directory and checks the endpoints still work.

## Observation Data (.obs)

The .obs dataframe is split into numerical and categorical columns.

### `__obsnum` table

Numerical observation columns (stored as floats).

```sql
CREATE TABLE __obsnum (
    cell_id INTEGER PRIMARY KEY,
    <column_name> FLOAT,
    ...
);

CREATE INDEX idx_obsnum_cell ON __obsnum(cell_id);
```

Columns included based on `obsnum_include` list in metadata.

**View**:
```sql
CREATE VIEW obsnum AS
SELECT
    c.cell_name,
    o.*
FROM __obsnum o
JOIN __cells c ON o.cell_id = c.cell_id;
```

### `__obscat` table

Categorical observation columns (stored as ENUM types for space efficiency).

```sql
CREATE TABLE __obscat (
    cell_id INTEGER PRIMARY KEY,
    <column_name> ENUM('<value1>', '<value2>', ...) DEFAULT NULL,
    ...
);

CREATE INDEX idx_obscat_cell ON __obscat(cell_id);
```

Columns included based on `obscat_include` list in metadata.
Columns with more than `max_obscat_values` unique values are automatically excluded.

**View**:
```sql
CREATE VIEW obscat AS
SELECT
    c.cell_name,
    o.*
FROM __obscat o
JOIN __cells c ON o.cell_id = c.cell_id;
```

## Variable Data (.var)

Similar split to .obs, but for gene/feature metadata.

### `__varnum` and `__varcat` tables

Follow the same pattern as obsnum/obscat, but keyed by gene_id.

```sql
CREATE TABLE __varnum (
    gene_id INTEGER PRIMARY KEY,
    <column_name> FLOAT,
    ...
);

CREATE TABLE __varcat (
    gene_id INTEGER PRIMARY KEY,
    <column_name> ENUM(...),
    ...
);
```

## Embeddings (.obsm)

Each embedding (UMAP, PCA, etc.) gets its own table.

### `__obsm__<name>` tables

For each embedding (e.g., `__obsm__umap`):

```sql
CREATE TABLE __obsm__<name> (
    cell_id INTEGER PRIMARY KEY,
    dim_0 FLOAT NOT NULL,
    dim_1 FLOAT NOT NULL,
    ... (additional dimensions as needed)
);

```

**View**:
```sql
CREATE VIEW obsm__<name> AS
SELECT
    c.cell_name,
    e.*
FROM __obsm__<name> e
JOIN __cells c ON e.cell_id = c.cell_id;
```

## Indexing Strategy

The only ART indexes are the ones DuckDB builds for PRIMARY KEY (every
cell_id / gene_id) and UNIQUE (cell_name, gene_name) constraints, plus a few
task-specific ones (`__de_*`, `idx_gene_bins_*`).

Until 2026-09 ant-convert also created one index per categorical obs column
(`idx_obscat_*`), duplicate `idx_cells_name` / `idx_genes_name`, and
`idx_obsm_*_cell` on primary-key columns. DuckDB 1.5 plans sequential scans
for the filters anthive issues, so these were never used; they roughly
tripled file size and made AnnotationWriter overwrites of an indexed column
100-4000x slower (Francken PFC microglia: 363 s vs 0.08 s). Existing files
are cleaned with `bin/ingest/ant-drop-indexes`.

## Storage Efficiency

### Integer References
- Using INTEGER for cell_id and gene_id dramatically reduces storage
- Original names stored once in lookup tables
- Expression data references by integer

### Sparse Storage
- Zeros omitted from layer tables
- For typical scRNA-seq data (95%+ zeros), this gives 20x+ compression

### ENUM Types
- Categorical columns use ENUM to store categories once
- Much more space-efficient than repeated strings

### Chunk Processing
- Data loaded and melted in chunks (configurable via `melt_chunk_size`)
- Prevents memory exhaustion on large datasets
- Optimal chunk size balances speed vs memory
