"""The release version lives in two places; they must match."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_release_date_matches_the_changelog_heading():
    """API_VERSION_RELEASED is None while the changelog says unreleased, else the heading's date."""
    serve = (REPO / 'bin/api/ant-serve').read_text()
    api_version = re.search(r'^API_VERSION = "([^"]+)"', serve, re.M).group(1)
    released = re.search(r'^API_VERSION_RELEASED(?:: [^=]+)? = (None|"([^"]+)")', serve, re.M)
    heading = re.search(rf'^## {re.escape(api_version)} \(([^)]+)\)', (REPO / 'doc/API_CHANGELOG.md').read_text(), re.M)
    assert heading, f'no changelog heading for {api_version}'
    if heading.group(1) == 'unreleased':
        assert released.group(1) == 'None'
    else:
        assert released.group(2) == heading.group(1)


def test_lib_version_matches_api_version():
    lib_version = re.search(r'^__version__ = "([^"]+)"', (REPO / 'lib/__init__.py').read_text(), re.M).group(1)
    api_version = re.search(r'^API_VERSION = "([^"]+)"', (REPO / 'bin/api/ant-serve').read_text(), re.M).group(1)
    assert lib_version == api_version
