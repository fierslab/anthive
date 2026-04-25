"""Shared fixtures for anthive tests."""

import sys
from pathlib import Path

import pytest

# Ensure lib/ is importable (mirrors runtime PYTHONPATH=lib)
_lib_dir = str(Path(__file__).parent.parent / "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)


@pytest.fixture
def sample_md_content():
    """Valid YAML-frontmatter markdown string."""
    return (
        "---\n"
        "title: Test Dataset\n"
        "version: '1.0'\n"
        "annotation:\n"
        "  organism: human\n"
        "  sex: female\n"
        "---\n"
        "\n"
        "Some body text.\n"
    )


@pytest.fixture
def anthive_root():
    """Return the real anthive repo root (for reading anthive.default.yaml)."""
    return Path(__file__).parent.parent
