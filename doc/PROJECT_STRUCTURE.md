# Anthive4 Project Structure

Clean, organized project structure with separate directories for client, documentation, and core tools.

## Directory Layout

```
anthive4/
├── README.md                    # Main project README
│
├── antclient/                   # Python client package
│   ├── antclient.py            # Single-file client (~700 lines)
│   ├── setup.py                # Setup configuration
│   ├── pyproject.toml          # Modern packaging metadata
│   ├── MANIFEST.in             # Package file inclusion
│   ├── LICENSE                 # MIT License
│   ├── CHANGELOG.md            # Version history
│   ├── example_client.py       # Runnable example
│   ├── README_DEV.md           # Developer guide
│   └── build_pypi.sh           # PyPI build script
│
├── bin/                         # Command-line tools
│   ├── ant                     # Main wrapper
│   ├── ant-check               # Validation tool
│   ├── ant-convert             # Conversion tool
│   ├── ant-serve               # REST API server (Phase 2)
│   └── ant-serve.phase1        # Phase 1 backup
│
├── lib/                         # Shared Python libraries
│   ├── anthive_db.py           # Database access layer
│   ├── checksum.py             # SHA256 utilities
│   ├── config.py               # Configuration management
│   ├── h5ad_utils.py           # H5AD file operations
│   ├── layer_stats.py          # Statistics calculation
│   └── metadata.py             # Metadata file handling
│
├── doc/                         # All documentation
│   ├── README.md               # Documentation index
│   │
│   ├── CONFIG.md               # Configuration reference
│   ├── DATABASE_SCHEMA.md      # DuckDB schema details
│   ├── ERROR_HANDLING.md       # Error handling philosophy
│   ├── METADATA.md             # Metadata file formats
│   ├── PERFORMANCE.md          # Performance tuning
│   ├── CHECK_PROCESS.md        # Check command workflow
│   ├── CONVERT_PROCESS.md      # Convert command workflow
│   │
│   ├── REST_API_SPEC.md        # API specification v2.0
│   ├── REST_API_COMPLETE.md    # Phase 1 implementation
│   ├── PHASE2_COMPLETE.md      # Phase 2 enhancements
│   │
│   ├── ANTCLIENT_README.md     # Client user guide
│   ├── ANTCLIENT_INSTALL.md    # PyPI packaging guide
│   ├── ANTCLIENT_COMPLETE.md   # Client implementation
│   └── ANTCLIENT_NAN_HANDLING.md  # NaN behavior
│
├── data/                        # Data directory (gitignored)
│   └── h5adstore/              # Symlink to data storage
│
├── anthive.yaml                 # Optional configuration
└── anthive.default.yaml         # Default configuration

```

## Quick Navigation

### For Users

**Getting Started:**
- `README.md` - Project overview
- `doc/README.md` - Documentation index

**Using the Tools:**
- `bin/ant check` - Validate h5ad files
- `bin/ant convert` - Convert to DuckDB
- `bin/ant serve` - Start REST API

**API Client:**
- `antclient/antclient.py` - Python client (single file)
- `antclient/example_client.py` - Usage examples
- `doc/ANTCLIENT_README.md` - Client documentation

### For Developers

**Core Development:**
- `lib/` - Shared libraries
- `doc/DATABASE_SCHEMA.md` - Schema reference
- `doc/PERFORMANCE.md` - Optimization guide

**API Development:**
- `doc/REST_API_SPEC.md` - API specification
- `doc/PHASE2_COMPLETE.md` - Phase 2 features
- `bin/ant-serve` - Server implementation

**Client Development:**
- `antclient/README_DEV.md` - Client dev guide
- `antclient/build_pypi.sh` - Build script
- `doc/ANTCLIENT_COMPLETE.md` - Implementation details

## File Categories

### Core Tools (bin/)
- **ant** - Main command wrapper
- **ant-check** - H5AD validation
- **ant-convert** - DuckDB conversion
- **ant-serve** - REST API server

### Python Libraries (lib/)
- **anthive_db.py** - Database queries
- **config.py** - Configuration
- **h5ad_utils.py** - H5AD operations
- **metadata.py** - Metadata handling
- **checksum.py** - File integrity
- **layer_stats.py** - Statistics

### Client Package (antclient/)
- **antclient.py** - Main client (single file)
- **setup.py, pyproject.toml** - Packaging
- **example_client.py** - Examples
- **build_pypi.sh** - Build automation

### Documentation (doc/)

**Tool Documentation:**
- CONFIG.md - Configuration
- DATABASE_SCHEMA.md - Schema
- METADATA.md - Metadata formats
- CHECK_PROCESS.md - Validation
- CONVERT_PROCESS.md - Conversion

**API Documentation:**
- REST_API_SPEC.md - API v2.0 spec
- REST_API_COMPLETE.md - Phase 1
- PHASE2_COMPLETE.md - Phase 2

**Client Documentation:**
- ANTCLIENT_README.md - User guide
- ANTCLIENT_INSTALL.md - PyPI guide
- ANTCLIENT_COMPLETE.md - Implementation
- ANTCLIENT_NAN_HANDLING.md - NaN behavior

## Design Principles

### Clean Root
- Only README.md in root
- All other docs in doc/
- Clear separation of concerns

### Single-File Client
- `antclient.py` - One file, easy to symlink
- Perfect for Pyodide/browser integration
- Self-contained, minimal dependencies

### Modular Libraries
- Shared code in lib/
- Reusable across tools
- Clear responsibilities

### Comprehensive Documentation
- Everything in doc/
- Easy to find and navigate
- Organized by topic

## Usage Examples

### Install Client from Source

```bash
cd antclient
pip install -e .
```

### Build for PyPI

```bash
cd antclient
./build_pypi.sh
```

### Symlink Client for Webapp

```bash
ln -s /data/1/users/mark/anthive4/antclient/antclient.py /path/to/webapp/
```

### Run Tools

```bash
# Check h5ad file
./bin/ant check mydata.h5ad

# Convert to DuckDB
./bin/ant convert mydata.h5ad

# Start API server
./bin/ant serve --data-folder ./data
```

### Use Client

```python
# Option 1: Install from source
from antclient import AnthiveClient

# Option 2: Direct import (if in path)
import sys
sys.path.insert(0, '/data/1/users/mark/anthive4/antclient')
from antclient import AnthiveClient

# Connect and query
client = AnthiveClient("http://localhost:8080")
df = client.get_cells("mydb", genes=["CD4"], format='dataframe')
```

## Benefits

### For Users
- ✅ Clean root directory
- ✅ Easy to find documentation
- ✅ Clear package boundaries
- ✅ Simple client deployment

### For Developers
- ✅ Modular codebase
- ✅ Reusable libraries
- ✅ Clear dependencies
- ✅ Easy to test and maintain

### For Deployment
- ✅ Single-file client (Pyodide ready)
- ✅ Standard Python packaging
- ✅ PyPI ready
- ✅ Docker friendly

## Summary

The project is organized into clear, logical sections:

1. **Root** - Main README only
2. **antclient/** - Complete Python client package
3. **bin/** - Command-line tools
4. **lib/** - Shared libraries
5. **doc/** - All documentation

This structure makes it easy to:
- Find what you need
- Package for PyPI
- Deploy the client
- Maintain the codebase
- Onboard new developers

---

**Last Updated:** 2026-02-24
**Status:** ✅ Organized and Clean
