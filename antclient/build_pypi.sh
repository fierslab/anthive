#!/bin/bash
# Build script for PyPI packaging

set -e  # Exit on error

echo "=== Anthive Client - PyPI Build Script ==="
echo ""

# Check we're in the right directory
if [ ! -f "antclient.py" ]; then
    echo "Error: antclient.py not found. Run from antclient/ directory."
    exit 1
fi

# 1. Copy README from doc/
echo "1. Copying README from ../doc/ANTCLIENT_README.md..."
if [ -f "../doc/ANTCLIENT_README.md" ]; then
    cp ../doc/ANTCLIENT_README.md README.md
    echo "   ✓ README.md created"
else
    echo "   ⚠ Warning: ../doc/ANTCLIENT_README.md not found"
fi

# 2. Clean previous builds
echo ""
echo "2. Cleaning previous builds..."
rm -rf build dist *.egg-info
echo "   ✓ Cleaned"

# 3. Build
echo ""
echo "3. Building distribution packages..."
uvx --from build pyproject-build
echo "   ✓ Built"

# 4. Check
echo ""
echo "4. Checking distribution..."
uvx twine check dist/*
echo "   ✓ Checked"

# 5. Show results
echo ""
echo "=== Build Complete ==="
echo ""
echo "Created files:"
ls -lh dist/
echo ""
echo "Next steps:"
echo "  Test PyPI: uvx twine upload --repository testpypi dist/*"
echo "  Production: uvx twine upload dist/*"
echo ""
