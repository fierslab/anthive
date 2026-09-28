"""The release version lives in two places; they must match."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_lib_version_matches_api_version():
    lib_version = re.search(r'^__version__ = "([^"]+)"', (REPO / 'lib/__init__.py').read_text(), re.M).group(1)
    api_version = re.search(r'^API_VERSION = "([^"]+)"', (REPO / 'bin/api/ant-serve').read_text(), re.M).group(1)
    assert lib_version == api_version
