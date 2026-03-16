# Performance Optimization

## Memory Management

Anthive is designed to handle massive h5ad files (millions of cells, tens of thousands of genes) without running out of memory.

### Chunk-Based Processing

The primary strategy is **chunked melting** of expression layers.

#### Problem
Loading an entire expression matrix for millions of cells into memory at once can require 10-100+ GB RAM.

#### Solution
Process the data in chunks of cells, controlled by the `melt_chunk_size` configuration parameter.

#### Process Flow

1. Load chunk of cells (e.g., cells 0-10000)
2. Melt expression data for this chunk
3. Remove zeros (sparse storage)
4. Write to DuckDB
5. Free memory
6. Repeat for next chunk

#### Pseudocode

```python
chunk_size = config['melt_chunk_size']
num_cells = adata.n_obs

for chunk_start in range(0, num_cells, chunk_size):
    chunk_end = min(chunk_start + chunk_size, num_cells)

    # Load chunk
    chunk_data = adata.X[chunk_start:chunk_end, :]

    # Melt (convert to long format)
    melted = melt_with_indices(chunk_data, chunk_start)

    # Remove zeros
    melted = melted[melted['value'] != 0]

    # Write to database
    duckdb_conn.execute(
        "INSERT INTO __layer__X VALUES (?, ?, ?)",
        melted
    )

    # Explicitly free memory
    del chunk_data, melted
    gc.collect()
```

### Chunk Size Tuning

The `melt_chunk_size` parameter controls the memory/speed tradeoff:

| Chunk Size | Memory Usage | Speed | Recommended For |
|------------|--------------|-------|-----------------|
| 1,000 | ~100-500 MB | Slowest | Systems with <8GB RAM |
| 5,000 | ~500 MB - 2 GB | Slow | Systems with 8GB RAM |
| 10,000 | ~1-5 GB | Moderate | Default, systems with 16GB RAM |
| 20,000 | ~2-10 GB | Fast | Systems with 32GB+ RAM |
| 50,000 | ~5-25 GB | Fastest | High-memory servers (64GB+) |

**Note**: Actual memory usage depends on:
- Number of genes (more genes = more memory per cell)
- Data sparsity (dense data uses more memory)
- Number of layers being processed

### Optimal Chunk Size Determination

The optimal chunk size balances:
1. **Overhead**: Very small chunks have high per-chunk overhead
2. **Memory**: Chunks must fit in available RAM
3. **Database writes**: Larger batches are more efficient

**Recommendation**: Start with default (10,000). If you encounter out-of-memory errors, reduce. If you have abundant RAM and want faster processing, increase.

## Database Write Optimization

### Batch Inserts

Write data in batches rather than row-by-row:

```python
# Good: batch insert
duckdb_conn.executemany(
    "INSERT INTO __layer__X VALUES (?, ?, ?)",
    chunk_data
)

# Bad: row-by-row
for row in chunk_data:
    duckdb_conn.execute(
        "INSERT INTO __layer__X VALUES (?, ?, ?)",
        row
    )
```

### Indexing Strategy

Create indices AFTER bulk loading data, not before:

```python
# Load all data first
for chunk in chunks:
    insert_chunk(chunk)

# Then create indices
duckdb_conn.execute("CREATE INDEX idx_cell ON __layer__X(cell_id)")
duckdb_conn.execute("CREATE INDEX idx_gene ON __layer__X(gene_id)")
```

## Expected Performance

### Conversion Time

For a typical scRNA-seq dataset:
- **Small** (10k cells, 20k genes): 30 seconds - 2 minutes
- **Medium** (100k cells, 30k genes): 5-15 minutes
- **Large** (1M cells, 30k genes): 30-60 minutes
- **Very large** (5M+ cells): 2-4 hours

Factors affecting speed:
- Data sparsity (sparser = faster due to less data to write)
- Number of layers
- Disk I/O speed (SSD vs HDD makes a big difference)
- CPU speed

### Storage Compression

Expected DuckDB file size relative to h5ad:
- **Sparse data** (>95% zeros): 10-30% of h5ad size
- **Dense data**: 60-90% of h5ad size

Integer indexing and omitting zeros provide dramatic space savings.

## Monitoring Progress

During conversion, log:
- Current chunk being processed
- Percentage complete
- Estimated time remaining (based on chunk processing rate)

Example output:
```
Processing layer 'X'...
Chunk 0-10000 (10%)... 15s
Chunk 10000-20000 (20%)... 14s
Chunk 20000-30000 (30%)... 15s
Estimated time remaining: 90s
```

## Handling Large Datasets

For extremely large datasets (>10M cells):
1. Ensure sufficient disk space (at least 2x h5ad file size)
2. Reduce `melt_chunk_size` to avoid OOM errors
3. Run on machines with fast storage (NVMe SSD strongly recommended)
4. Consider running overnight for multi-hour conversions
5. Monitor system resources during conversion

## Future Optimizations

Potential areas for improvement:
- Parallel chunk processing (multi-threading)
- Compression of DuckDB tables
- Incremental updates (only convert changed portions)
- Memory-mapped file access for very large h5ad files
