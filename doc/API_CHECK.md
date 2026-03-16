# API Check Tool

## Overview

`ant-api-check` validates REST API endpoints and optionally performs deep data integrity checks by comparing API responses with the original h5ad file.

## Usage

```bash
# Basic endpoint checks (fast)
ant api-check http://localhost:8080 mydb

# Deep data integrity checks (slower, loads h5ad)
ant api-check http://localhost:8080 mydb --deep dataset.h5ad

# Custom timeout
ant api-check http://localhost:8080 mydb --timeout 60
```

## What It Checks

### Basic Checks (Always Performed)

**[1/3] Basic Endpoints:**
- `GET /` - API root and version
- `GET /health` - Health check status
- `GET /metrics` - Performance metrics

**[2/3] Database Endpoints:**
- `GET /databases/{db_id}/info` - Database metadata
- `GET /databases/{db_id}/genes` - Gene search
- `GET /databases/{db_id}/layers` - Available layers
- `GET /databases/{db_id}/embeddings` - Available embeddings
- `GET /databases/{db_id}/cells` - Cell data retrieval

### Deep Checks (--deep flag)

Loads the h5ad file and compares API responses with actual data:

**Layer Expression Values:**
- Samples ~50 random genes from each layer
- Requests expression data via API for each gene
- Compares with h5ad values for 100 cells per gene
- Reports any mismatches

**Obs Table Values:**
- Samples ~50 random cells
- Requests metadata via API
- Compares numerical and categorical columns
- Handles NaN values correctly

**Embedding Coordinates:**
- Samples ~50 random cells
- Requests embedding data via API
- Compares coordinates for each embedding
- Checks all dimensions

## Output Examples

### Basic Check (Success)

```
======================================================================
API CHECK: http://localhost:8080
Database: mydb
======================================================================

[1/3] Testing basic endpoints...
  ✓ Root endpoint: API version 2.0.0
  ✓ Health check: healthy
  ✓ Metrics endpoint

[2/3] Testing database endpoints for 'mydb'...
  ✓ Database info: 10,000 cells, 2,000 genes
  ✓ Gene search: returned 10 genes
  ✓ Layers endpoint: 2 layers
  ✓ Embeddings endpoint: 2 embeddings
  ✓ Cells endpoint: returned 10 cells

======================================================================
Tests: 10 passed, 0 failed
======================================================================
✓ API CHECK COMPLETED SUCCESSFULLY
======================================================================

API is healthy and data matches h5ad file.
```

### Deep Check (Success)

```
======================================================================
DEEP DATA INTEGRITY CHECKS
======================================================================

Loading h5ad file: dataset.h5ad
  ✓ Loaded (10,000 cells × 2,000 genes)

[Deep Check] Verifying layer expression values via API...
  ✓ Layer X: 50 genes checked, all match
  ✓ Layer counts: 50 genes checked, all match

[Deep Check] Verifying obs data via API...
  ✓ Obs data: 150 values checked, all match

[Deep Check] Verifying embeddings via API...
  ✓ Embedding X_pca: 50 cells checked, all match
  ✓ Embedding X_umap: 50 cells checked, all match

======================================================================
Tests: 15 passed, 0 failed
======================================================================
✓ API CHECK COMPLETED SUCCESSFULLY
======================================================================

API is healthy and data matches h5ad file.
```

### Check with Errors

```
Check Summary:

✗ Endpoint failures:
  - GET /databases/mydb/info: HTTP 404

✗ Data integrity mismatches:
  - Layer X, gene 'CD4', cell 'AAACCTGAGCAGGTCA': h5ad=2.500000, api=2.499995
  - Obs: cell 'AAACCTGAGCAGGTCA', col 'n_counts': h5ad=1500, api=1501
  ... and 5 more mismatches

======================================================================
Tests: 8 passed, 3 failed
======================================================================
✗ API CHECK FAILED - Errors found
======================================================================
```

## Performance Monitoring

The tool automatically tracks response times and warns about slow endpoints:

```
⚠ Performance warnings:
  - GET /databases/mydb/cells: slow response (6.2s)
```

Responses over 5 seconds trigger a warning.

## Exit Codes

- **0**: Success (all checks passed)
- **1**: Failure (endpoint failures or data mismatches)
- **130**: Interrupted by user (Ctrl+C)

## Integration with Testing

### CI/CD Pipeline

```bash
#!/bin/bash
# Start API server
ant serve --data-folder ./data --port 8080 &
API_PID=$!

# Wait for server to start
sleep 5

# Run API checks
ant api-check http://localhost:8080 mydb --deep ./data/mydb.h5ad

# Capture exit code
EXIT_CODE=$?

# Stop server
kill $API_PID

exit $EXIT_CODE
```

### Smoke Testing

```bash
# Quick smoke test (no deep checks)
ant api-check http://production-server:8080 mydb
```

### Full Integration Test

```bash
# Full data integrity validation
ant api-check http://staging-server:8080 mydb --deep /data/mydb.h5ad
```

## Comparison with duckdb-check

| Feature | duckdb-check | api-check |
|---------|--------------|-----------|
| **Tests** | Database structure | API endpoints |
| **Data Source** | DuckDB file | REST API |
| **Deep Checks** | Direct DB access | Via API calls |
| **Use Case** | After conversion | After deployment |
| **Speed** | Faster (direct access) | Slower (HTTP overhead) |
| **Tests** | DB integrity | End-to-end stack |

## When to Use

### Use `ant api-check` when:
- ✓ Testing deployed API server
- ✓ Validating end-to-end data flow
- ✓ Testing API after code changes
- ✓ Smoke testing production deployments
- ✓ CI/CD integration tests
- ✓ Monitoring API health

### Use `ant duckdb-check` when:
- ✓ Validating database after conversion
- ✓ Testing database structure
- ✓ Debugging conversion issues
- ✓ Faster local validation

## Troubleshooting

### Connection Refused

```
✗ Endpoint failures:
  - GET /: Connection refused
```

**Solution:** Ensure the API server is running:
```bash
ant serve --data-folder ./data --port 8080
```

### Timeout Errors

```
✗ Endpoint failures:
  - GET /databases/mydb/cells: timeout after 30s
```

**Solution:** Increase timeout:
```bash
ant api-check http://localhost:8080 mydb --timeout 120
```

### Database Not Found

```
✗ Endpoint failures:
  - GET /databases/mydb/info: HTTP 404
```

**Solution:** Check database ID and ensure it's loaded:
```bash
# List available databases
curl http://localhost:8080/databases

# Rescan data folder
curl -X POST http://localhost:8080/admin/rescan
```

### Data Mismatches

Small floating point differences are normal due to:
- Floating point precision in serialization
- Rounding in API responses

The tool uses tolerances:
- `rtol=1e-5` (relative tolerance)
- `atol=1e-8` (absolute tolerance)

If you see systematic mismatches, investigate:
1. Database conversion issues
2. API serialization bugs
3. Client deserialization issues

## Advanced Usage

### Custom Sampling

Edit the script to change `num_samples` parameter:

```python
deep_check_layers_api(..., num_samples=100)  # Check more genes
```

### Testing Specific Layers

Modify the script to test only specific layers or embeddings.

### Load Testing

For load testing, use a dedicated tool like `locust` or `k6` instead of this tool.

## Future Enhancements

Possible additions:
- Concurrent request testing
- Response schema validation
- Performance benchmarking
- Authentication testing
- Rate limit checking
- WebSocket endpoint testing
