# Anthive Client - Development Guide

## Directory Structure

```
anthive4/
├── antclient/              # Client package
│   ├── antclient.py       # Main client module
│   ├── setup.py           # Setup configuration
│   ├── pyproject.toml     # Modern packaging
│   ├── MANIFEST.in        # Package includes
│   ├── LICENSE            # MIT License
│   ├── CHANGELOG.md       # Version history
│   ├── example_client.py  # Example usage
│   └── README_DEV.md      # This file
│
├── doc/                    # All documentation
│   ├── ANTCLIENT_README.md    # User documentation
│   ├── ANTCLIENT_INSTALL.md   # PyPI packaging guide
│   ├── ANTCLIENT_COMPLETE.md  # Implementation summary
│   ├── ANTCLIENT_NAN_HANDLING.md  # NaN handling docs
│   ├── REST_API_SPEC.md       # API specification
│   ├── PHASE2_COMPLETE.md     # Phase 2 features
│   └── ...                    # Other docs
│
└── README.md               # Main project README

```

## Building for PyPI

### Prerequisites

Only `uv` is required (no separate `pip install` needed):

```bash
# uv must be installed: https://docs.astral.sh/uv/getting-started/installation/
which uv
```

### Build Process

```bash
cd antclient

# 1. Copy README from doc/ for PyPI
cp ../doc/ANTCLIENT_README.md README.md

# 2. Clean previous builds
rm -rf build dist *.egg-info

# 3. Build
uvx --from build pyproject-build

# 4. Check
uvx twine check dist/*

# 5. Upload to Test PyPI
uvx twine upload --repository testpypi dist/*

# 6. Upload to Production PyPI
uvx twine upload dist/*
```

### Quick Build Script

```bash
cd antclient
./build_pypi.sh
```

## Testing Locally

### Test Import

```bash
cd /data/1/users/mark/anthive4
python3 -c "import sys; sys.path.insert(0, 'antclient'); import antclient; print(antclient.__version__)"
```

### Test with Example

```bash
# Start server (in one terminal)
cd /data/1/users/mark/anthive4
./bin/ant-serve --data-folder /data/1/project/h5adstore

# Run example (in another terminal)
cd /data/1/users/mark/anthive4
python3 -c "import sys; sys.path.insert(0, 'antclient')" -c "exec(open('antclient/example_client.py').read())"
```

### Install from Source

```bash
cd antclient
pip install -e .
```

## Symlinking for Pyodide

The single-file design allows easy symlinking:

```bash
ln -s /data/1/users/mark/anthive4/antclient/antclient.py /path/to/webapp/antclient.py
```

## Documentation

All documentation is in `../doc/`:

- **ANTCLIENT_README.md** - User guide (→ copy to README.md for PyPI)
- **ANTCLIENT_INSTALL.md** - PyPI packaging instructions
- **ANTCLIENT_COMPLETE.md** - Implementation summary
- **ANTCLIENT_NAN_HANDLING.md** - NaN handling behavior
- **REST_API_SPEC.md** - REST API reference

## Version Management

Update version in **TWO** places:

1. `antclient.py` - Line with `__version__ = "1.0.0"`
2. `pyproject.toml` - Line with `version = "1.0.0"`

setup.py reads version from antclient.py automatically.

## Files

### Core
- **antclient.py** - Single-file client (~700 lines)
- **example_client.py** - Runnable example

### Packaging
- **setup.py** - Setup configuration
- **pyproject.toml** - Modern packaging metadata
- **MANIFEST.in** - File inclusion rules
- **LICENSE** - MIT License
- **CHANGELOG.md** - Version history

### Development
- **README_DEV.md** - This file
- **README.md** - Created during build (from ../doc/ANTCLIENT_README.md)
