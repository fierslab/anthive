# NaN Handling in Anthive Client

## Summary

The client now intelligently handles NaN (missing) values when converting to pandas DataFrames, distinguishing between gene expression data and metadata.

## The Problem

In single-cell data:
- **Gene expression NaN** = Gene not detected = Should be **0**
- **Metadata NaN** = Missing information = Should stay **NaN**

The database stores sparse matrices where zeros are omitted. When querying specific genes that weren't detected in certain cells, the API returns `null` (NaN in pandas), which should be filled with 0 for biological correctness.

## The Solution

### Default Behavior (fill_na=True)

When `format='dataframe'` and `fill_na=True` (default):

1. **Gene columns**: NaN filled with 0
2. **Metadata columns**: NaN preserved
3. **Embedding columns (dim_*)**: NaN preserved (though shouldn't occur)

### Smart Column Detection

The client automatically identifies gene columns vs metadata columns:

**Excluded from filling (kept as NaN):**
- `cell_name`, `cell_id`, `cell_type`, `celltype`, `tissue`, `sample`, etc.
- Columns starting with: `dim_*` (embeddings)
- Columns starting with: `n_*`, `percent*`, `pct_*`, `log*`, `total_*` (metrics)
- Other common metadata fields

**Filled with 0:**
- All other numeric (float) columns are assumed to be gene expression

## Usage Examples

### Get Cells - Default (fill NaN with 0)

```python
# Gene expression columns have NaN → 0
df = client.get_cells(
    "mydb",
    genes=["CD4", "CD8"],
    metadata=["cell_type"],
    format='dataframe'
)

# Result:
# - CD4 column: NaN filled with 0 (gene not detected)
# - CD8 column: NaN filled with 0 (gene not detected)
# - cell_type: NaN preserved (missing metadata)
```

### Get Cells - Preserve NaN

```python
# Keep NaN as-is (not recommended for gene data)
df = client.get_cells(
    "mydb",
    genes=["CD4", "CD8"],
    metadata=["cell_type"],
    format='dataframe',
    fill_na=False  # Preserve all NaN
)

# Result:
# - CD4 column: NaN preserved (not biologically meaningful!)
# - CD8 column: NaN preserved (not biologically meaningful!)
# - cell_type: NaN preserved
```

### SQL Queries - Same Behavior

```python
# Default: fill gene columns with 0
df = client.execute_sql(
    "mydb",
    "SELECT cell_name, CD4, CD8 FROM layer__X LIMIT 100",
    format='dataframe'
)

# Disable filling
df = client.execute_sql(
    "mydb",
    "SELECT cell_name, CD4, CD8 FROM layer__X LIMIT 100",
    format='dataframe',
    fill_na=False
)
```

### JSON Format - No Filling

```python
# JSON always returns null as-is (not filled)
result = client.get_cells(
    "mydb",
    genes=["CD4"],
    format='json'
)

# result['data'] contains:
# [{'cell_name': 'cell1', 'CD4': null}, ...]  # null stays null
```

## Why This Matters

### Biological Correctness

```python
# INCORRECT (without fill_na):
df = client.get_cells("mydb", genes=["CD4"], format='dataframe', fill_na=False)
df['CD4'].mean()  # NaN propagates - result is NaN!

# CORRECT (with fill_na=True, default):
df = client.get_cells("mydb", genes=["CD4"], format='dataframe')
df['CD4'].mean()  # Works! Undetected genes = 0
```

### Analysis Ready

```python
# Works immediately without manual fillna
df = client.get_cells("mydb", genes=["CD4", "CD8"], format='dataframe')

# Can directly analyze
df[['CD4', 'CD8']].corr()  # Works!
df['CD4'].sum()            # Works!
df.groupby('cell_type')['CD4'].mean()  # Works!
```

### Metadata Preserved

```python
df = client.get_cells(
    "mydb",
    genes=["CD4"],
    metadata=["donor", "batch"],
    format='dataframe'
)

# Gene columns: NaN → 0 (correct)
# Metadata columns: NaN preserved (correct - truly missing)
df[df['donor'].isna()]  # Can find cells with missing donor info
```

## Implementation Details

The `_to_dataframe()` helper function:

1. Converts API response to DataFrame
2. If `fill_na_genes=True`:
   - Identifies numeric columns
   - Excludes known metadata patterns
   - Fills remaining numeric columns with 0
3. Returns analysis-ready DataFrame

## Backward Compatibility

### Default Behavior Changed

**Before:**
- All NaN preserved
- Required manual `df.fillna(0)` for gene columns

**After (v1.0.0):**
- Gene NaN automatically filled with 0
- Metadata NaN preserved
- Use `fill_na=False` for old behavior

### Migration

Old code that manually filled:
```python
# Old approach
df = client.get_cells(..., format='dataframe')
df[gene_cols] = df[gene_cols].fillna(0)  # Manual filling

# New approach (automatic)
df = client.get_cells(..., format='dataframe')
# Already filled! No manual step needed
```

## Best Practices

### ✅ Recommended

```python
# Let the client handle it (default)
df = client.get_cells("mydb", genes=["CD4"], format='dataframe')
```

### ⚠️ Use with Caution

```python
# Only if you need to distinguish NaN from 0
df = client.get_cells("mydb", genes=["CD4"], format='dataframe', fill_na=False)
# Then manually handle based on your use case
```

### ❌ Not Recommended

```python
# Don't manually fill all columns
df = client.get_cells("mydb", genes=["CD4"], metadata=["*"], format='dataframe', fill_na=False)
df = df.fillna(0)  # BAD! Fills metadata too (e.g., missing donor → 0)
```

## Edge Cases

### Custom Gene Names

If your genes have names matching metadata patterns, they won't be filled:

```python
# Gene named "n_reads" won't be filled (matches n_* pattern)
# Workaround: manually fill after
df = client.get_cells("mydb", genes=["n_reads"], format='dataframe')
df['n_reads'] = df['n_reads'].fillna(0)
```

### All Metadata Query

```python
# All metadata fields - nothing to fill
df = client.get_cells("mydb", metadata="*", format='dataframe')
# No gene columns, so fill_na has no effect
```

### Mixed Queries

```python
# Both genes and metadata
df = client.get_cells(
    "mydb",
    genes=["CD4", "CD8"],
    metadata=["cell_type", "n_counts"],
    format='dataframe'
)

# Result:
# - CD4, CD8: NaN → 0 (genes)
# - cell_type: NaN preserved (categorical metadata)
# - n_counts: NaN preserved (matches n_* pattern)
```

## Summary

| Data Type | NaN Meaning | Default Behavior | Rationale |
|-----------|-------------|------------------|-----------|
| Gene expression | Not detected | Fill with 0 | Biological correctness |
| Metadata (categorical) | Missing info | Preserve NaN | Distinguish unknown from 0 |
| Metadata (numerical) | Missing info | Preserve NaN | Distinguish unknown from 0 |
| Embeddings (dim_*) | Should not occur | Preserve NaN | No biological meaning |

**Default behavior (fill_na=True) is correct for 99% of use cases.**

---

**Date:** 2026-02-24
**Version:** 1.0.0
**Status:** ✅ Implemented and Tested
