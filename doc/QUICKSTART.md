# Anthive Quick Start

## Setup

Set up the anthive environment in your current shell:

```bash
cd /data/1/users/mark/anthive4
source sourceme.sh
```

This adds `./bin/` to your PATH and enables bash tab completion.

### Make it Permanent (Optional)

Add to your `~/.bashrc`:

```bash
# Anthive environment
source /data/1/users/mark/anthive4/sourceme.sh
```

## Basic Workflow

### 1. Check an h5ad file

```bash
ant h5ad-check dataset.h5ad
```

This validates the h5ad file and generates metadata in `dataset.md`.

### 2. Convert to DuckDB

```bash
ant convert dataset.h5ad
```

This creates `dataset.duckdb` from the h5ad file.

### 3. Verify the database

```bash
# Quick check (structure only)
ant duckdb-check dataset.h5ad

# Deep check (validates actual data, slower)
ant duckdb-check dataset.h5ad --deep
```

### 4. Start the REST API

```bash
ant serve --data-folder ./data --port 8080
```

Browse to http://localhost:8080/docs for the API documentation.

### 5. Validate the REST API

```bash
# Basic endpoint checks
ant api-check http://localhost:8080 mydb

# Deep data integrity checks
ant api-check http://localhost:8080 mydb --deep dataset.h5ad
```

## Tab Completion Examples

```bash
# Complete subcommands
ant [TAB]
→ browse build-all check convert db duckdb-check h5ad-check md serve

# Complete .h5ad files
ant check [TAB]
→ dataset1.h5ad  dataset2.h5ad  dataset3.h5ad

# Complete .duckdb files
ant db structure [TAB]
→ dataset1.duckdb  dataset2.duckdb

# Complete flags
ant serve --[TAB]
→ --data-folder  --host  --port  --reload
```

## Command Reference

### File Checking
- `ant h5ad-check <file.h5ad>` - Validate h5ad file, generate metadata
- `ant duckdb-check <file.h5ad>` - Validate DuckDB database
- `ant duckdb-check <file.h5ad> --deep` - Deep data integrity checks
- `ant check <file.h5ad>` - Run both h5ad and duckdb checks

### Conversion
- `ant convert <file.h5ad>` - Convert h5ad to DuckDB

### Database Inspection
- `ant db structure <file.duckdb>` - Show database structure
- `ant db schema <file.duckdb> <table>` - Show table schema
- `ant db query <file.duckdb> "SQL"` - Execute SQL query

### Metadata Management
- `ant md get <file.h5ad> <key>` - Get metadata value
- `ant md set <file.h5ad> <key> <value>` - Set metadata value
- `ant md delete <file.h5ad> <key>` - Delete metadata key
- `ant md list <file.h5ad>` - List all metadata

### Services
- `ant serve [--data-folder DIR]` - Start REST API server
- `ant browse [--data-folder DIR]` - Start web browser interface
- `ant api-check <URL> <db_id>` - Validate REST API endpoints
- `ant api-check <URL> <db_id> --deep <file.h5ad>` - Deep API checks

### Batch Processing
- `ant build-all [--data-folder DIR]` - Process entire datastore

## Help

```bash
# General help
ant --help

# Command-specific help
ant <command> --help
```

## Documentation

- `/doc/CHECK_TOOLS.md` - Detailed guide to check tools
- `/doc/API_CHECK.md` - REST API validation guide
- `/doc/METADATA_STRUCTURE.md` - Metadata format documentation
- `/bash_completion/README.md` - Bash completion setup
