# Error Handling Philosophy

Anthive follows a **fail-fast** approach: it is better to crash with a clear error message than to leave corrupt or partially-processed files.

## General Principles

1. **No partial states**: If a process fails, clean up completely
2. **Clear error messages**: Always explain what went wrong and why
3. **Exit with non-zero code**: Ensure failures are detectable in scripts
4. **No silent failures**: Every error must be visible to the user

## Specific Error Cases

### Corrupted H5AD Files

**Detection**:
- File cannot be opened by scanpy/anndata
- Required fields (.X, .obs, .var) are missing
- Data structures are malformed

**Response**:
1. Print detailed error message showing what is corrupted
2. Exit with error code 1
3. Do NOT create or modify any .md or .duckdb files

### Conversion Failures

**Scenarios**:
- Out of memory during melting
- DuckDB write errors
- Disk space exhaustion
- Data type conversion errors

**Response**:
1. Print detailed error message with context
2. **Delete the partially-created .duckdb file**
3. Exit with error code 1
4. Leave the source .h5ad file untouched

**Cleanup logic**:
```python
# Pseudocode
try:
    convert_h5ad_to_duckdb(h5ad_path, duckdb_path)
except Exception as error:
    if os.path.exists(duckdb_path):
        os.remove(duckdb_path)
    print(f"CONVERSION FAILED: {error}")
    sys.exit(1)
```

### Invalid Configuration

**Detection**:
- anthive.yaml contains invalid YAML syntax
- Parameter values are out of range
- Required parameters are wrong type

**Response**:
1. Print specific configuration error (field name, expected vs actual)
2. Reference correct values in anthive.default.yaml
3. Exit with error code 1
4. Do NOT proceed with default values when config is explicitly wrong

### Checksum Mismatches

**Detection**:
- Re-running `ant check` on an h5ad file
- Existing .md file has sha256 field
- Calculated checksum differs from stored checksum

**Response**:
1. Print warning that h5ad file has changed since last check
2. Update checksum and version in .md file
3. Continue with check (this is a warning, not an error)
4. Log the change date

## Error Message Format

All error messages should follow this format:

```
ERROR: [Component] Brief description
Details: Specific information about what failed
Location: File path or context where error occurred
Action: What was cleaned up or what user should do
```

Example:
```
ERROR: [ant-convert] Failed to melt layer 'counts'
Details: Out of memory while processing chunk 45000-55000
Location: /data/dataset1.h5ad
Action: Removed partial file /data/dataset1.duckdb
Suggestion: Try reducing melt_chunk_size in anthive.yaml
```

## Exit Codes

- `0`: Success
- `1`: General error (corrupt file, conversion failure, invalid config)
- `2`: Invalid command line arguments
- `3`: File not found
