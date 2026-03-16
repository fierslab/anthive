# ant md - Markdown Metadata Management

Command-line tool for managing YAML metadata in markdown files.

## Usage

```bash
ant md <command> [arguments] FILE
```

## Commands

### `set` - Set metadata fields

Set one or more metadata fields in a markdown file.

```bash
ant md set key=value [key=value ...] FILE
```

**Examples:**
```bash
# Set single field
ant md set experiment='Sierksma 2025' index.md

# Set multiple fields
ant md set experiment='Sierksma 2025' group=kidney year=2025 index.md

# Set different types
ant md set year=2025 index.md                    # number
ant md set published=true index.md               # boolean
ant md set authors='["John", "Jane"]' index.md   # array (as string)
```

**Value types:**
- Strings: `key='value'` or `key="value"`
- Numbers: `key=123` or `key=3.14`
- Booleans: `key=true` or `key=false`
- Null: `key=null`
- Arrays/objects: `key='["a","b"]'` (as JSON string)

### `get` - Get metadata fields

Retrieve one or more metadata fields from a markdown file.

```bash
ant md get key [key ...] FILE
```

**Examples:**
```bash
# Get single field
ant md get experiment index.md

# Get multiple fields
ant md get experiment group year index.md
```

**Output:**
```
experiment: Sierksma 2025
group: kidney
year: 2025
```

### `show` - Show all metadata

Display all metadata fields in a markdown file.

```bash
ant md show FILE
```

**Example:**
```bash
ant md show index.md
```

**Output:**
```
Metadata in index.md:

  title: Kidney Study
  experiment: Sierksma 2025
  group: kidney
  year: 2025
  authors: ["John Doe", "Jane Smith"]
```

### `delete` - Delete metadata fields

Remove one or more metadata fields from a markdown file.

```bash
ant md delete key [key ...] FILE
```

**Examples:**
```bash
# Delete single field
ant md delete old_field index.md

# Delete multiple fields
ant md delete old_field1 old_field2 index.md
```

## Markdown File Format

The tool works with markdown files that have YAML preambles:

```markdown
---
title: My Document
year: 2025
experiment: Study Name
---

# My Document

Content goes here...
```

## Use Cases

### Update Collection Metadata

```bash
# Update index.md for a collection
ant md set experiment='Sierksma 2025' group=kidney authors='["Sierksma M", "et al"]' data/h5adstore/Sierksma2025/index.md
```

### Batch Update

```bash
# Update multiple files
for file in data/h5adstore/*/index.md; do
    ant md set year=2025 "$file"
done
```

### Query Metadata

```bash
# Check what's in a file
ant md show data/h5adstore/Sierksma2025/index.md

# Get specific fields
ant md get experiment authors data/h5adstore/Sierksma2025/index.md
```

### Clean Up

```bash
# Remove deprecated fields
ant md delete old_field deprecated_field index.md
```

## Integration with ant-check

The `ant check` command reads these metadata fields from `.md` files:

**Dataset metadata** (e.g., `dataset.md`):
- `title` - Dataset name
- `version` - Anthive version
- `sha256` - File checksum
- `year` - Publication year
- `date` - Last checked date
- `obsnum_include` - Numerical observation columns
- `obscat_include` - Categorical observation columns
- `obs_exclude` - Excluded observation columns
- `layers_include` - Available layers
- `obsm_include` - Available embeddings

**Collection metadata** (e.g., `index.md`):
- `group` - Collection/study name
- `experiment` - Experiment description
- `authors` - List of authors
- `year` - Publication year

You can use `ant md` to manually update these fields when needed.

## Tips

### Quote Values with Spaces

```bash
# Correct
ant md set experiment='Sierksma 2025' index.md

# Also correct
ant md set experiment="Sierksma 2025" index.md

# Wrong (will break)
ant md set experiment=Sierksma 2025 index.md
```

### Set Complex Values

For arrays or objects, pass them as JSON strings:

```bash
ant md set authors='["John Doe", "Jane Smith", "Bob Johnson"]' index.md
```

### Check Before Setting

```bash
# Show current values
ant md show index.md

# Set new values
ant md set experiment='New Study' index.md

# Verify
ant md show index.md
```

### Backup Before Batch Updates

```bash
# Backup
cp index.md index.md.backup

# Update
ant md set year=2025 index.md

# Restore if needed
mv index.md.backup index.md
```

## Error Handling

The tool provides clear error messages:

```bash
# File not found
$ ant md show nonexistent.md
Error: File not found: nonexistent.md

# Invalid syntax
$ ant md set invalid index.md
Error: Invalid assignment: invalid
Expected format: key=value

# Unknown command
$ ant md invalid-command
Error: Unknown command: invalid-command
Available commands: set, get, show, delete
```

## Exit Codes

- `0` - Success
- `1` - Error (file not found, invalid syntax, etc.)
- `2` - Usage error (wrong number of arguments)

## License

BSD-3-Clause
