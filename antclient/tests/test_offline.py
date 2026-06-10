"""Offline tests for AnthiveClient — no network, no server required.

These cover the pure-Python contract of the client: version string,
preset / geom validation, URL construction, and the local error paths.
The integration / round-trip tests live in ``test_smoke.py`` and need
a running ``ant-serve``.

Run with::

    cd antclient
    uv run --with pytest --with requests --no-project python -m pytest tests/test_offline.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `antclient` importable when running from the repo without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import antclient
from antclient import AnthiveClient


# ──────────────────────────────────────────────────────────────────────────────


def test_version_string():
    """``__version__`` is a sensible semver."""
    parts = antclient.__version__.split('.')
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts[:3]), antclient.__version__


def test_plot_geoms_set():
    """The locally-known plot geoms include every one the server ships."""
    assert isinstance(AnthiveClient.PLOT_GEOMS, frozenset)
    # Must include the full set we shipped on the server side
    expected = {
        'scatter', 'hexbin',
        'violin', 'box', 'bar',
        'histogram', 'ecdf', 'kde',
        'heatmap', 'rolling',
        'volcano', 'ma', 'forest',
    }
    missing = expected - AnthiveClient.PLOT_GEOMS
    assert not missing, f"PLOT_GEOMS is missing: {missing}"


def test_base_url_normalisation():
    """Trailing slash on base_url is stripped."""
    c1 = AnthiveClient('http://x.test/api')
    c2 = AnthiveClient('http://x.test/api/')
    assert c1.base_url == c2.base_url == 'http://x.test/api'


def test_default_base_url():
    """No base_url falls back to localhost:8080."""
    c = AnthiveClient(None)
    assert c.base_url == 'http://localhost:8080'


def test_auth_constructor_passes_through():
    """``auth=(user, password)`` is stored verbatim."""
    c = AnthiveClient('http://x', auth=('bds', 'astrocyte'))
    assert c.auth == ('bds', 'astrocyte')


def test_get_plot_rejects_unknown_geom():
    """Local validation before any HTTP — saves a round-trip."""
    c = AnthiveClient('http://x')
    with pytest.raises(ValueError, match="Unknown geom 'bogus_geom'"):
        c.get_plot('some_db', 'bogus_geom')


def test_get_plot_accepts_known_geom_without_network(monkeypatch):
    """A known geom routes to ``/plot/{geom}`` — verify URL by mocking _get_raw."""
    captured = {}

    class FakeResponse:
        content = b''
        headers = {'X-Plot-Format': 'png', 'X-Plot-Caption': '',
                   'X-Source-URL': '', 'X-Plot-Cache': 'MISS',
                   'Content-Type': 'image/png'}

    c = AnthiveClient('http://x')

    def fake_get_raw(path, params=None):
        captured['path'] = path
        captured['params'] = params
        return FakeResponse()

    monkeypatch.setattr(c, '_get_raw', fake_get_raw)
    result = c.get_plot('grp/file', 'volcano',
                        study='STUDY1', term='term1', contrast='c1')
    assert captured['path'] == '/databases/grp/file/plot/volcano'
    assert captured['params'].get('study') == 'STUDY1'
    assert captured['params'].get('term') == 'term1'
    assert captured['params'].get('contrast') == 'c1'
    assert result['format'] == 'png'


def test_de_methods_exist():
    """The 1.7.0 DE wrappers are on the class — protects against an accidental
    removal during refactors.
    """
    expected = {
        'list_de_studies', 'get_de_study', 'list_de_contrasts',
        'get_de_rows', 'get_de_by_gene',
    }
    missing = expected - set(dir(AnthiveClient))
    assert not missing, f"missing DE methods: {missing}"


def test_version_method_exists():
    """``get_version`` + ``get_changelog`` are on the class."""
    assert hasattr(AnthiveClient, 'get_version')
    assert hasattr(AnthiveClient, 'get_changelog')
