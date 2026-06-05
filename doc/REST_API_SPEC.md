# Anthive REST API Specification v2.0

Specification for the anthive4 REST API based on anthive3 with improvements.

## Overview

The Anthive REST API provides HTTP access to single-cell expression data stored in DuckDB databases. It scans a data folder for `.duckdb` files and exposes them through RESTful endpoints.

## Design Principles

1. **Read-only**: All data access is read-only for safety
2. **Fast**: Connection pooling, caching, and efficient queries
3. **Simple**: Clear, predictable endpoint structure
4. **Documented**: OpenAPI/Swagger auto-documentation
5. **Secure**: Optional bearer token authentication
6. **Flexible**: Multiple output formats (JSON, CSV, Parquet)

## Technology Stack

- **Framework**: FastAPI (async, high performance)
- **Database**: DuckDB (read-only connections)
- **Server**: Uvicorn
- **Auth**: Bearer token (optional)
- **Docs**: Auto-generated OpenAPI/Swagger UI

## Changes from anthive3

### ✅ Keep From anthive3

- FastAPI with auto-docs
- Database discovery and scanning
- Gene search
- Metadata search (categorical/numerical)
- Cell data retrieval with filtering
- SQL query execution
- Embedding access
- Bearer token authentication
- CORS support
- Multiple output formats
- Admin rescan endpoint

### ✨ Improvements for anthive4

1. **Use new metadata format**
   - Read from `.md` files directly (YAML preamble)
   - Remove `metadata.yaml` fallback (simpler)
   - Use `index.md` for collection metadata

2. **Connection pooling**
   - Reuse DuckDB connections per database
   - Configurable pool size
   - Better concurrent request handling

3. **Response caching**
   - Cache database list
   - Cache gene lists
   - Cache metadata fields
   - Configurable TTL (from config)

4. **Pagination**
   - Standard pagination for large result sets
   - Cursor-based pagination for efficient iteration
   - Configurable page size limits

5. **Health & metrics**
   - `/health` endpoint for load balancers
   - `/metrics` endpoint (optional Prometheus format)
   - Database connection status

6. **Better error handling**
   - Specific error types (DatabaseNotFound, InvalidQuery, etc.)
   - Detailed error messages
   - Error codes for client handling

7. **Response compression**
   - Automatic gzip compression for large responses
   - Configurable compression threshold

8. **Rate limiting** (optional)
   - Per-IP rate limiting
   - Per-token rate limiting
   - Configurable limits

9. **Streaming support**
   - Stream large result sets
   - Avoid memory issues with huge queries

10. **Better async handling**
    - Proper async database operations
    - Background tasks for long operations

## Endpoint Structure

### Base URL

```
http://localhost:8080
```

### API Versioning

Optional `/v2/` prefix for future compatibility:
- Current: `/databases`
- Future: `/v2/databases`

## Endpoints

### 1. General Information

#### `GET /`
Root endpoint with API overview.

**Response:**
```json
{
  "name": "Anthive API",
  "version": "2.0.0",
  "databases_count": 10,
  "documentation": {
    "swagger": "/docs",
    "redoc": "/redoc",
    "openapi": "/openapi.json"
  },
  "endpoints": { ... }
}
```

#### `GET /health`
Health check + server-speed signal for load balancers and client-side
server selection (e.g. vark choosing the fastest of multiple anthive
endpoints).

**Response:**
```json
{
  "status": "healthy",
  "databases": 98,
  "database_errors": 0,
  "uptime_seconds": 3600,
  "mean_response_ms": 126.2,
  "p50_response_ms":  81.1,
  "n_samples": 31
}
```

**Speed fields** — computed from the last 1024 non-probe requests served
by this server:

- `mean_response_ms` — arithmetic mean. Skewed by plot renders.
- `p50_response_ms` — median. More stable; recommended for server picks.
- `n_samples` — how many real requests fed the stats. `null` (with both
  `*_response_ms` also `null`) until the first non-probe request lands.

`/health` and `/metrics` are themselves excluded from the timing samples
so probe traffic can't make a server look artificially fast.

**Status Codes:**
- 200: Healthy
- 503: Unhealthy (no databases or fatal init error)

#### `GET /metrics`
Server performance metrics.

**Response:**
```json
{
  "uptime_seconds": 3600,
  "requests": {
    "total":      31,
    "errors":     0,
    "error_rate": 0.0
  },
  "latency_ms": {
    "n_samples": 31,
    "mean":      126.2,
    "p50":       81.1,
    "p95":       227.4,
    "p99":       476.3,
    "max":       476.3
  },
  "connection_pool": {
    "total_connections": 99,
    "active_databases":  98,
    "in_use":            0,
    "cache_hits":        39,
    "cache_misses":      99,
    "hit_rate":          0.28
  },
  "caches": {
    "databases":             98,
    "metadata_cached":       98,
    "gene_lists_cached":     0,
    "metadata_stats_cached": 0
  }
}
```

`latency_ms.*` and `requests.*` are computed from the same ring buffer
as `/health` — same exclusions, same window (1024 samples).

### 2. Database Discovery

#### `GET /databases`
List all available databases.

**Query Parameters:**
- `refresh` (bool): Force refresh from disk (default: false)

**Response:**
```json
{
  "databases": [
    {
      "id": "Blackburn2026/seurat_obj_final",
      "path": "Blackburn2026/seurat_obj_final.duckdb",
      "group": "Blackburn2026",
      "title": "Seurat Object Final",
      "n_cells": 50000,
      "n_genes": 20000,
      "layers": ["X", "counts", "normalized"],
      "embeddings": ["Umap", "Pca"],
      "version": "0.1.0",
      "sha256": "abc123...",
      "checked_date": "2026-02-24",
      "group_metadata": {
        "group": "Blackburn2026",
        "experiment": "na",
        "authors": ["Author1", "Author2"],
        "year": 2026
      }
    }
  ],
  "total": 10,
  "page": 1,
  "page_size": 100,
  "pages": 1
}
```

#### `GET /databases/{db_id}/info`
Get detailed database information.

**Path Parameters:**
- `db_id` (string): Database ID (path with forward slashes)

**Response:**
```json
{
  "id": "Blackburn2026/seurat_obj_final",
  "n_cells": 50000,
  "n_genes": 20000,
  "layers": [
    {"id": "X", "name": "X", "dtype": "float32"},
    {"id": "counts", "name": "counts", "dtype": "int32"}
  ],
  "embeddings": [
    {"id": "Umap", "name": "UMAP", "n_dims": 2},
    {"id": "Pca", "name": "PCA", "n_dims": 50}
  ],
  "metadata_fields": {
    "numerical": ["n_genes", "n_counts", "percent_mito"],
    "categorical": ["cell_type", "tissue", "donor"]
  },
  "version": "0.1.0",
  "sha256": "abc123...",
  "checked_date": "2026-02-24"
}
```

### 3. Gene Operations

#### `GET /databases/{db_id}/genes`
Search for genes.

**Query Parameters:**
- `q` (string): Search query (substring match)
- `case_sensitive` (bool): Case-sensitive search (default: false)
- `limit` (int): Max results (default: 100, max: 1000)

**Response:**
```json
{
  "query": "CD4",
  "count": 3,
  "genes": ["CD4", "CD40", "CD40LG"]
}
```

#### `GET /databases/{db_id}/genes/{gene_id}`
Get gene information.

**Response:**
```json
{
  "gene_id": "CD4",
  "gene_name": "CD4",
  "exists": true,
  "layers": ["X", "counts"],
  "metadata": {
    "highly_variable": true,
    "mean_counts": 123.45
  }
}
```

### 4. Cell Data Retrieval

#### `GET /databases/{db_id}/cells`
Get cell data with expression and metadata.

**Query Parameters:**
- `genes` (string): Comma-separated gene names
- `metadata` (string): Comma-separated metadata fields, "*" for all, empty for none
- `layer` (string): Layer name (default: "X")
- `filter` (array): Filters in format `field:value` or `field:min,max`
- `limit` (int): Max cells (optional, no default - returns all matching cells)
- `offset` (int): Skip first N cells (optional)
- `format` (string): Output format: json, csv, parquet (default: json)

**Examples:**
```
GET /databases/my_db/cells?genes=CD4,CD8&metadata=cell_type,tissue&layer=X&limit=100
GET /databases/my_db/cells?genes=CD4&filter=cell_type:T-cell&filter=n_counts:1000,5000
GET /databases/my_db/cells?metadata=*&format=csv
```

**Response (JSON format):**
```json
{
  "n_cells": 100,
  "n_columns": 5,
  "columns": ["cell_name", "CD4", "CD8", "cell_type", "tissue"],
  "data": [
    {"cell_name": "cell_1", "CD4": 1.2, "CD8": 0.5, "cell_type": "T-cell", "tissue": "blood"},
    ...
  ],
  "truncated": false
}
```

**Response (CSV/Parquet):**
Returns file download with appropriate Content-Type and Content-Disposition headers.

### 5. Embedding Data

#### `GET /databases/{db_id}/embeddings`
List available embeddings.

**Response:**
```json
{
  "embeddings": [
    {"id": "Umap", "name": "UMAP", "n_dims": 2},
    {"id": "Pca", "name": "PCA", "n_dims": 50}
  ]
}
```

#### `GET /databases/{db_id}/embeddings/{embedding_id}`
Get embedding coordinates.

**Query Parameters:**
- `n_dims` (int): Number of dimensions (default: 2, 0 = all)
- `limit` (int): Max cells
- `offset` (int): Skip first N cells

**Response:**
```json
{
  "embedding_id": "Umap",
  "n_cells": 50000,
  "n_dims": 2,
  "columns": ["cell_name", "dim_0", "dim_1"],
  "data": [
    {"cell_name": "cell_1", "dim_0": 1.2, "dim_1": -0.5},
    ...
  ]
}
```

### 6. Metadata Operations

#### `GET /databases/{db_id}/metadata/fields`
List all metadata fields.

**Response:**
```json
{
  "numerical": [
    {"field": "n_genes", "min": 100, "max": 5000, "mean": 1234.5},
    {"field": "n_counts", "min": 500, "max": 50000, "mean": 10000.0}
  ],
  "categorical": [
    {"field": "cell_type", "n_unique": 10, "values": ["T-cell", "B-cell", ...]},
    {"field": "tissue", "n_unique": 5, "values": ["blood", "spleen", ...]}
  ]
}
```

#### `GET /databases/{db_id}/metadata/categorical`
Search categorical metadata values.

**Query Parameters:**
- `field` (string): Metadata field name
- `q` (string): Search query
- `case_sensitive` (bool): Case-sensitive (default: false)
- `limit` (int): Max results

**Response:**
```json
{
  "field": "cell_type",
  "query": "T",
  "count": 3,
  "values": ["T-cell", "T-reg", "T-helper"]
}
```

#### `GET /databases/{db_id}/metadata/numerical`
Get numerical metadata statistics.

**Query Parameters:**
- `field` (string): Metadata field name
- `min` (float): Min value filter
- `max` (float): Max value filter

**Response:**
```json
{
  "field": "n_counts",
  "min": 500,
  "max": 50000,
  "mean": 10000.0,
  "median": 8500.0,
  "std": 5000.0,
  "q25": 5000.0,
  "q75": 15000.0
}
```

### 7. Layer Operations

#### `GET /databases/{db_id}/layers`
List available layers.

**Response:**
```json
{
  "layers": [
    {"id": "X", "name": "X", "dtype": "float32"},
    {"id": "counts", "name": "Raw Counts", "dtype": "int32"}
  ]
}
```

### 8. SQL Queries

#### `POST /databases/{db_id}/query/sql`
Execute raw SQL query (requires authentication if token set).

**Request Body:**
```json
{
  "query": "SELECT cell_name, n_genes FROM obsnum LIMIT 10",
  "limit": 1000
}
```

**Response:**
```json
{
  "query": "SELECT cell_name, n_genes FROM obsnum LIMIT 10",
  "n_rows": 10,
  "n_columns": 2,
  "columns": ["cell_name", "n_genes"],
  "dtypes": {"cell_name": "object", "n_genes": "int64"},
  "data": [...]
}
```

### 9. Admin Operations

#### `POST /admin/rescan`
Force rescan of data folder and reload metadata cache.

**Response:**
```json
{
  "status": "success",
  "previous_count": 8,
  "current_count": 10,
  "added": ["new_db1", "new_db2"],
  "removed": [],
  "databases": ["db1", "db2", ...]
}
```

## Authentication

Authentication is handled by the reverse proxy (nginx, caddy, etc.) using HTTP Basic Authentication. The API itself does not implement authentication - it assumes requests reaching it are already authenticated.

## Configuration

### Environment Variables

- `ANTHIVE_DATA_FOLDER`: Path to data folder (default: ./data)
- `ANTHIVE_TOKEN`: Bearer token for authentication (optional)
- `ANTHIVE_HOST`: Server host (default: 127.0.0.1)
- `ANTHIVE_PORT`: Server port (default: 8000)
- `ANTHIVE_CACHE_TTL`: Cache TTL in seconds (default: 3600)
- `ANTHIVE_MAX_PAGE_SIZE`: Max pagination size (default: 1000)
- `ANTHIVE_ENABLE_METRICS`: Enable /metrics endpoint (default: false)
- `ANTHIVE_RATE_LIMIT`: Requests per minute (default: 0 = unlimited)

### Command Line

```bash
ant serve --data-folder ./data --host 0.0.0.0 --port 8080 --token secret
```

## Error Handling

### Error Response Format

```json
{
  "detail": "Error message",
  "error_type": "DatabaseNotFoundError",
  "database_id": "my_db"
}
```

### Status Codes

- **200**: Success
- **400**: Bad Request (invalid parameters)
- **401**: Unauthorized (invalid/missing token)
- **404**: Not Found (database, gene, etc.)
- **429**: Too Many Requests (rate limit)
- **500**: Internal Server Error
- **503**: Service Unavailable (database connection issues)

### Error Types

- `DatabaseNotFoundError`: Database ID doesn't exist
- `GeneNotFoundError`: Gene doesn't exist
- `InvalidQueryError`: SQL query syntax error
- `InvalidParameterError`: Invalid query parameter
- `MetadataFieldNotFoundError`: Metadata field doesn't exist

## Performance Considerations

### Connection Pooling

- One connection pool per database
- Connections reused across requests
- Configurable pool size (default: 5 per database)
- Automatic connection cleanup

### Caching

- Database list cached (TTL configurable)
- Gene lists cached per database
- Metadata fields cached per database
- Cache invalidation on admin rescan

### Pagination

- Default page size: 100
- Max page size: 1000 (configurable)
- Use offset/limit for large result sets
- Consider streaming for very large datasets

### Rate Limiting

- Per-IP limiting (optional)
- Per-token limiting (optional)
- Configurable limits
- 429 status code with Retry-After header

## CORS Configuration

Default: Allow all origins (development)

Production: Configure specific origins:
```python
allow_origins=["https://your-app.com", "https://dashboard.your-app.com"]
```

## Output Formats

### JSON (default)
```
Content-Type: application/json
```

### CSV
```
Content-Type: text/csv
Content-Disposition: attachment; filename=cells.csv
```

### Parquet
```
Content-Type: application/octet-stream
Content-Disposition: attachment; filename=cells.parquet
```

## Example Workflows

### 1. Basic Discovery

```bash
# List databases
curl http://localhost:8080/databases

# Get database info
curl http://localhost:8080/databases/my_db/info

# Search genes
curl "http://localhost:8080/databases/my_db/genes?q=CD4&limit=10"
```

### 2. Get Cell Data

```bash
# Get expression for specific genes
curl "http://localhost:8080/databases/my_db/cells?genes=CD4,CD8&limit=100"

# Get with metadata and filters
curl "http://localhost:8080/databases/my_db/cells?genes=CD4&metadata=cell_type&filter=cell_type:T-cell"

# Export to CSV
curl "http://localhost:8080/databases/my_db/cells?genes=CD4,CD8&format=csv" > cells.csv
```

### 3. Explore Embeddings

```bash
# List embeddings
curl http://localhost:8080/databases/my_db/embeddings

# Get UMAP coordinates
curl "http://localhost:8080/databases/my_db/embeddings/Umap?n_dims=2&limit=1000"
```

### 4. Custom SQL (with auth)

```bash
# Execute SQL query
curl -X POST http://localhost:8080/databases/my_db/query/sql \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT * FROM cells LIMIT 10"}'
```

## Implementation Priority

### Phase 1 (MVP)
1. Basic server setup with FastAPI
2. Database discovery and listing
3. Gene search
4. Cell data retrieval (basic)
5. Metadata operations
6. Read from new .md format

### Phase 2 (Enhanced)
1. Connection pooling
2. Caching layer
3. Pagination
4. Multiple output formats
5. Embedding access

### Phase 3 (Production)
1. Rate limiting
2. Health/metrics endpoints
3. Response compression
4. Better error handling
5. Streaming support

## Testing Strategy

### Unit Tests
- Endpoint response formats
- Query parsing
- Error handling
- Authentication

### Integration Tests
- Full request/response cycles
- Database operations
- Concurrent requests
- Large result sets

### Performance Tests
- Load testing (1000+ concurrent requests)
- Query performance
- Memory usage
- Connection pooling efficiency
