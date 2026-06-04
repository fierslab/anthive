# Anthive Client - Installation and PyPI Packaging Guide

## Quick Test (Before Publishing)

### Test the Client Locally

```bash
# Start the Anthive server (in one terminal)
cd /data/1/users/mark/anthive4
./bin/api/ant-serve --data-folder /data/1/project/h5adstore

# Test the client (in another terminal)
cd /data/1/users/mark/anthive4
python3 example_client.py
```

### Test Installation from Source

```bash
# Create test environment
python3 -m venv test_env
source test_env/bin/activate

# Install from source
pip install -e .

# Test import
python3 -c "from antclient import AnthiveClient; print('OK')"

# Run example
python3 example_client.py

# Cleanup
deactivate
rm -rf test_env
```

## Building for PyPI

### Prerequisites

```bash
# Install build tools
pip install build twine

# Create PyPI account (if not already done)
# - Test PyPI: https://test.pypi.org/account/register/
# - Production PyPI: https://pypi.org/account/register/

# Generate API tokens
# - Test PyPI: https://test.pypi.org/manage/account/#api-tokens
# - Production PyPI: https://pypi.org/manage/account/#api-tokens

# Configure credentials in ~/.pypirc
cat > ~/.pypirc << 'EOF'
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-YOUR-PRODUCTION-TOKEN-HERE

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-YOUR-TEST-TOKEN-HERE
EOF

chmod 600 ~/.pypirc
```

### Version Management

**IMPORTANT**: Update version in **THREE** locations:

1. `antclient.py` - Line with `__version__ = "1.0.0"`
2. `setup.py` - Line with `version="1.0.0"`
3. `pyproject.toml` - Line with `version = "1.0.0"`

### Build Process

```bash
# Clean previous builds
rm -rf build dist *.egg-info

# Build distribution packages
python -m build

# This creates:
# - dist/antclient-1.0.0-py3-none-any.whl (wheel)
# - dist/antclient-1.0.0.tar.gz (source)

# Verify build
twine check dist/*
```

### Upload to Test PyPI (Recommended First)

```bash
# Upload to Test PyPI
twine upload --repository testpypi dist/*

# Check package page
# https://test.pypi.org/project/antclient/

# Test installation in clean environment
python3 -m venv test_pypi_env
source test_pypi_env/bin/activate

# Install from Test PyPI (need --extra-index-url for dependencies)
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            antclient

# Verify
python3 -c "from antclient import AnthiveClient; print(AnthiveClient.__doc__)"

# Cleanup
deactivate
rm -rf test_pypi_env
```

### Upload to Production PyPI

```bash
# Upload to Production PyPI
twine upload dist/*

# Check package page
# https://pypi.org/project/antclient/

# Test installation
python3 -m venv prod_test_env
source prod_test_env/bin/activate

pip install antclient
python3 -c "from antclient import AnthiveClient; print('Installed:', AnthiveClient.__version__)"

# With pandas
pip install antclient[pandas]

# Cleanup
deactivate
rm -rf prod_test_env
```

### Post-Upload Verification

1. **Check PyPI Package Page**:
   - Description renders correctly (from ANTCLIENT_README.md)
   - Metadata is complete
   - Links work
   - Classifiers correct

2. **Test in Fresh Environment**:
   ```bash
   pip install antclient
   python3 example_client.py
   ```

3. **Update Repository**:
   ```bash
   git tag v1.0.0
   git push --tags
   ```

## Single File Usage (Pyodide/Webapp)

The client is a single file (`antclient.py`) for easy integration:

```bash
# Copy to your project
cp antclient.py /path/to/your/webapp/

# Or create symlink
ln -s /data/1/users/mark/anthive4/antclient.py /path/to/your/webapp/
```

In Pyodide/browser:

```python
# antclient.py should be in the same directory
from antclient import AnthiveClient

# Auto-detects browser origin
client = AnthiveClient()

# Or specify explicitly
client = AnthiveClient("https://api.example.com")
```

## Troubleshooting

### "File already exists" Error

PyPI doesn't allow re-uploading same version. Bump version:

1. Update `__version__` in `antclient.py`
2. Update `version` in `setup.py`
3. Update `version` in `pyproject.toml`
4. Rebuild: `rm -rf dist build *.egg-info && python -m build`
5. Upload again

### "Invalid authentication credentials"

Check `~/.pypirc`:
- Username should be literal string `__token__`
- Password should start with `pypi-`
- File permissions: `chmod 600 ~/.pypirc`

### "Description failed to render"

Validate README:
```bash
twine check dist/*
```

Fix any markdown issues and rebuild.

### Import Error After Installation

Verify package structure:
```bash
pip show antclient
# Check "Location:" to find install directory
ls -la <location>/antclient.py  # Should exist
```

## Version Update Workflow

```bash
# 1. Update version in all 3 files
# antclient.py, setup.py, pyproject.toml

# 2. Update CHANGELOG.md

# 3. Clean and rebuild
rm -rf build dist *.egg-info
python -m build

# 4. Check
twine check dist/*

# 5. Upload to Test PyPI first
twine upload --repository testpypi dist/*

# 6. Test
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            antclient

# 7. If OK, upload to Production
twine upload dist/*

# 8. Git tag
git tag v1.0.1
git push --tags
```

## Quick Reference

```bash
# Complete workflow
rm -rf build dist *.egg-info
python -m build
twine check dist/*
twine upload --repository testpypi dist/*  # Test first
twine upload dist/*                         # Production

# Verify
pip install antclient
python -c "from antclient import AnthiveClient; print(AnthiveClient.__version__)"
```

## Development Tools

```bash
# Install dev dependencies
pip install -e .[dev]

# Code formatting
black antclient.py

# Linting
flake8 antclient.py

# Type checking (if mypy installed)
mypy antclient.py
```

## Files for PyPI

- ✅ `antclient.py` - Main client (single file)
- ✅ `setup.py` - Setup configuration
- ✅ `pyproject.toml` - Modern Python packaging
- ✅ `ANTCLIENT_README.md` - User documentation (rename to README.md for PyPI)
- ✅ `LICENSE` - MIT License
- ✅ `CHANGELOG.md` - Version history
- ✅ `MANIFEST.in` - Include additional files

## Pre-release Checklist

- [ ] Version updated in 3 files
- [ ] CHANGELOG.md updated
- [ ] README.md comprehensive
- [ ] LICENSE file present
- [ ] Example script works
- [ ] `twine check dist/*` passes
- [ ] Test PyPI upload works
- [ ] Test installation from Test PyPI
- [ ] All imports work
- [ ] Example script runs successfully
