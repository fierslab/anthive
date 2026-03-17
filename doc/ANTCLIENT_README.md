# Anthive Client

Lightweight Python client for the Anthive REST API - query single-cell expression databases with ease.

## Features

- **Lightweight**: Only requires `requests` library
- **Optional pandas support**: Install pandas for DataFrame output
- **Pyodide-compatible**: Runs in JupyterLite / browser environments
- **Type hints**: Full type annotations for IDE support
- **Auto-detection**: Works in standard Python, Pyodide, and Streamlit
- **Comprehensive**: Complete API coverage including gene statistics

## Installation

### From PyPI

```bash
# Basic installation
pip install anthive-client

# With pandas support for DataFrames
pip install anthive-client[pandas]
```

### From Source

```bash
# Clone repository
git clone https://github.com/anthive/anthive4.git
cd anthive4

# Install
pip install -e .

# Or just copy the single file
cp antclient.py /path/to/your/project/
```

## Quick Start

```python
from antclient import AnthiveClient
from anthelper import find_database, find_metadata

# Connect to API (auto-detects URL in Pyodide/browser)
client = AnthiveClient()

# Browse available datasets
find_database(client, "brain")          # search by keyword
find_metadata(client, "LC5/SPM")        # list obs columns

# Get database info and detect best layer
info = client.get_database_info("LC5/SPM")
layer = 'data' if 'data' in info['layers'] else info['layers'][0]

# Search genes
genes = client.search_genes("LC5/SPM", q="Apoe", limit=10)

# Get per-gene statistics (fast — no full cell download)
stats = client.get_gene_stats("LC5/SPM", ["Apoe", "Actb"], format='dataframe')
print(stats[["gene", "pct_nonzero", "mean", "median_nonzero"]])

# Get cell data as DataFrame
df = client.get_cells(
    "LC5/SPM",
    genes=["Apoe", "Trem2"],
    metadata=["genotype", "cell_type"],
    layer=layer,
    format='dataframe'
)
print(df.head())
```

## Usage Examples

### Database Discovery

```python
# List all databases
databases = client.get_databases()

# Get as DataFrame
df = client.get_databases(format='dataframe')

# Force refresh from disk
databases = client.get_databases(refresh=True)

# Get specific database info
info = client.get_database_info("Voet2025/slide_st.counts")
print(f"Title: {info['title']}")
print(f"Layers: {info['layers']}")
print(f"Embeddings: {info['embeddings']}")
```

### Gene Operations

```python
# Search genes
genes = client.search_genes("mydb", q="CD", limit=20)

# Case-sensitive search
genes = client.search_genes("mydb", q="Cd4", case_sensitive=True)

# Get gene info
info = client.get_gene_info("mydb", "CD4")

# Get all genes (careful - may be large!)
all_genes = client.get_all_genes("mydb")
```

### Gene Statistics (lightweight — no full cell download)

```python
# Single gene
stats = client.get_gene_stats("mydb", "APOE")
# Returns list of dicts: gene, layer, n_cells, n_nonzero, pct_nonzero,
#   mean, std (all cells), mean_nonzero, std_nonzero, median_nonzero,
#   q10_nonzero, q90_nonzero, max

# Multiple genes as DataFrame
df = client.get_gene_stats("mydb", ["APOE", "CLU", "ACTB"],
                           layer="data", format='dataframe')
print(df[["gene", "pct_nonzero", "mean", "median_nonzero"]])
```

### Cell Data Retrieval

```python
# Get expression data for specific genes
result = client.get_cells(
    "mydb",
    genes=["CD4", "CD8"],
    limit=100
)

# Get with metadata
result = client.get_cells(
    "mydb",
    genes=["CD4"],
    metadata=["cell_type", "tissue"]
)

# Get all metadata
result = client.get_cells(
    "mydb",
    genes=["CD4"],
    metadata="*"  # or metadata=["*"]
)

# With filters
result = client.get_cells(
    "mydb",
    genes=["CD4"],
    metadata=["cell_type", "n_counts"],
    filters=[
        "cell_type:T-cell",
        "n_counts:1000,5000"  # range filter
    ]
)

# Get as DataFrame
df = client.get_cells(
    "mydb",
    genes=["CD4", "CD8"],
    metadata=["cell_type"],
    format='dataframe'
)

# Export to CSV
csv_data = client.get_cells(
    "mydb",
    genes=["CD4"],
    format='csv'
)

# Export to Parquet
parquet_bytes = client.get_cells(
    "mydb",
    genes=["CD4"],
    format='parquet'
)
```

### Embeddings

```python
# List available embeddings
embeddings = client.get_embeddings("mydb")

# Get UMAP coordinates
umap = client.get_embedding_data("mydb", "Umap", n_dims=2, limit=1000)

# Get as DataFrame
df = client.get_embedding_data("mydb", "Umap", format='dataframe')

# Get all dimensions
pca = client.get_embedding_data("mydb", "Pca", n_dims=0)  # 0 = all
```

### Metadata Operations

```python
# List metadata fields
fields = client.get_metadata_fields("mydb")
print(f"Numerical fields: {fields['numerical']}")
print(f"Categorical fields: {fields['categorical']}")

# List layers
layers = client.get_layers("mydb")
```

### SQL Queries

```python
# Execute raw SQL
result = client.execute_sql(
    "mydb",
    "SELECT cell_name, celltype FROM obscat LIMIT 10"
)

# Get as DataFrame
df = client.execute_sql(
    "mydb",
    "SELECT * FROM obsnum WHERE n_counts > 5000",
    format='dataframe'
)

# With limit
result = client.execute_sql(
    "mydb",
    "SELECT * FROM cells",
    limit=100
)
```

### Server Monitoring (Phase 2)

```python
# Check server health
health = client.get_health()
print(f"Status: {health['status']}")

# Get performance metrics
metrics = client.get_metrics()
print(f"Cache hit rate: {metrics['connection_pool']['hit_rate']:.2%}")
print(f"Uptime: {metrics['uptime_seconds']} seconds")
```

### Admin Operations

```python
# Rescan databases
result = client.rescan_databases()
print(f"Databases: {result['previous_count']} -> {result['current_count']}")
print(f"Added: {result['added']}")
print(f"Removed: {result['removed']}")
```

## Advanced Usage

### Environment Variables

```bash
# Set default API URL
export ANTHIVE_API_URL="http://myserver:8080"
```

```python
# Client will auto-detect from environment
client = AnthiveClient()  # Uses ANTHIVE_API_URL
```

### Pyodide/Browser Integration

```javascript
// In browser JavaScript, set global variable
window.ANTHIVE_API_URL = "https://api.example.com";
```

```python
# In Pyodide Python
from antclient import AnthiveClient

# Auto-detects from window.location or ANTHIVE_API_URL global
client = AnthiveClient()
```

### Streamlit Integration

The client automatically uses Streamlit's caching when available:

```python
import streamlit as st
from antclient import AnthiveClient

# Cached automatically in Streamlit
client = AnthiveClient("http://localhost:8080")
databases = client.get_databases()  # Cached for 60s

st.dataframe(databases)
```

### Custom Timeout

```python
# Set custom timeout
client = AnthiveClient("http://localhost:8080", timeout=60)
```

## API Reference

### AnthiveClient

**Constructor:**
- `AnthiveClient(base_url=None, timeout=30)`

**Info Methods:**
- `get_root()` - API root information
- `get_health()` - Server health status
- `get_metrics()` - Performance metrics (Phase 2)

**Database Methods:**
- `get_databases(refresh=False, format='list')` - List databases
- `get_database_info(db_id)` - Database details
- `list_database_ids()` - Get database IDs only

**Gene Methods:**
- `search_genes(db_id, q='', limit=100, case_sensitive=False)` - Search genes
- `get_gene_info(db_id, gene_id)` - Gene details
- `get_all_genes(db_id)` - All genes (may be large!)
- `get_gene_stats(db_id, genes, layer='X', format='json')` - Per-gene statistics

**Metadata Methods:**
- `get_layers(db_id)` - List layers
- `get_metadata_fields(db_id)` - List metadata fields
- `get_embeddings(db_id)` - List embeddings

**Data Methods:**
- `get_cells(db_id, genes=None, metadata=None, layer='X', filters=None, limit=None, format='json')` - Get cell data
- `get_embedding_data(db_id, embedding_id, n_dims=2, limit=None, format='json')` - Get embeddings

**Query Methods:**
- `execute_sql(db_id, query, limit=None, format='json')` - Execute SQL

**Admin Methods:**
- `rescan_databases()` - Force database rescan

## Error Handling

```python
import requests

try:
    result = client.get_database_info("nonexistent")
except requests.HTTPError as e:
    if e.response.status_code == 404:
        print("Database not found")
    elif e.response.status_code == 503:
        print("Server unavailable")
    else:
        print(f"Error: {e}")
```

## Development

### Testing

```bash
# Install dev dependencies
pip install -e .[dev]

# Run tests (when test suite is created)
pytest tests/

# Check code style
black antclient.py
flake8 antclient.py
```

### Building for PyPI

```bash
cd antclient
./build_pypi.sh            # copies README, builds, checks

# Upload (requires PyPI credentials)
uvx twine upload --repository testpypi dist/*   # test first
uvx twine upload dist/*                          # production
```

## anthelper — Jupyter Notebook Helpers

The companion `anthelper` module ships with `antclient` and provides pretty-printing helpers for interactive use:

```python
from anthelper import find_database, find_metadata

# Browse available datasets (sorted by year, up to 20)
find_database(client)
find_database(client, "alzheimer")   # filter by keyword

# List obs columns for a dataset
find_metadata(client, "LC5/SPM")
find_metadata(client, "LC5/SPM", "cell")   # filter column names
```

`anthelper` is designed for Jupyter / JupyterLite but works in any Python environment.

## Requirements

**Required:**
- Python >= 3.9
- requests >= 2.25.0

**Optional:**
- pandas >= 1.3.0 (for `format='dataframe'` support)

## Compatibility

- **Python**: 3.9, 3.10, 3.11, 3.12
- **Environments**: Standard Python, Pyodide, Streamlit
- **API**: Anthive REST API v2.0 (Phase 2)

## License

MIT License - see LICENSE file for details

## Contributing

Contributions welcome! Please open an issue or pull request on GitHub.

## Support

- **Issues**: https://github.com/anthive/anthive4/issues
- **Documentation**: https://github.com/anthive/anthive4
- **API Documentation**: http://your-server:8080/docs

## Changelog

### 1.0.0 (2026-02-24)

- Initial release for Anthive REST API v2.0 (Phase 2)
- Single-file lightweight client
- Full API coverage
- Optional pandas support
- Pyodide compatible
- Streamlit caching support
