"""Smoke tests for AnthiveClient — round-trip against a live ant-serve.

These are skipped unless the ``ANTHIVE_TEST_URL`` env var is set, so they
don't break offline test runs or CI runs without a server.

Run with::

    ANTHIVE_TEST_URL=http://localhost:8080/api \\
        uv run --with pytest --with requests --no-project \\
        python -m pytest tests/test_smoke.py -v

The tests are read-only and don't depend on any specific dataset — they
discover what's available and probe a representative slice. A dataset
with DE data ingested is preferred but optional (the DE checks
skip themselves when nothing is found).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import antclient
from antclient import AnthiveClient


# ──────────────────────────────────────────────────────────────────────────────


_BASE_URL = os.environ.get('ANTHIVE_TEST_URL')

pytestmark = pytest.mark.skipif(
    not _BASE_URL,
    reason='set ANTHIVE_TEST_URL to a running ant-serve to enable smoke tests',
)


@pytest.fixture(scope='module')
def client():
    auth = None
    user = os.environ.get('ANTHIVE_TEST_USER')
    password = os.environ.get('ANTHIVE_TEST_PASSWORD')
    if user and password:
        auth = (user, password)
    return AnthiveClient(_BASE_URL, auth=auth)


@pytest.fixture(scope='module')
def db_with_de(client):
    """First dataset that has at least one DE study (or skip)."""
    for db in client.get_databases():
        try:
            studies = client.list_de_studies(db['id'])
        except Exception:
            continue
        if studies:
            return db['id'], studies[0]
    pytest.skip('no dataset on this server has DE data ingested')


# ── core surfaces ─────────────────────────────────────────────────────────────


def test_health(client):
    h = client.get_health()
    assert h.get('status') == 'healthy'
    assert h.get('databases', 0) >= 0


def test_version(client):
    """``get_version`` returns the structured 2.3+ shape OR the synthesised
    fallback shape from /health on older servers."""
    v = client.get_version()
    assert 'version' in v
    assert v['version']
    # The keys are the same in both real and fallback shapes:
    for key in ('plot_geoms', 'plot_output_formats', 'deprecated_endpoints'):
        assert key in v


def test_changelog_on_2_3(client):
    """``get_changelog`` returns the markdown body — only on 2.3+."""
    v = client.get_version()
    if v.get('changelog_url') is None:
        pytest.skip('pre-2.3 server — no /changelog endpoint')
    md = client.get_changelog()
    assert 'Anthive REST API' in md or 'Changelog' in md.lower()


def test_databases_listed(client):
    dbs = client.get_databases()
    assert isinstance(dbs, list)


# ── DE data flow ──────────────────────────────────────────────────────────────


def test_list_de_studies(client, db_with_de):
    db_id, _study = db_with_de
    studies = client.list_de_studies(db_id)
    assert isinstance(studies, list)
    assert len(studies) >= 1
    s = studies[0]
    for key in ('study_id', 'method', 'n_rows', 'n_terms', 'n_contrasts'):
        assert key in s


def test_get_de_study(client, db_with_de):
    db_id, study = db_with_de
    detail = client.get_de_study(db_id, study['study_id'])
    assert detail['study_id'] == study['study_id']
    assert isinstance(detail.get('terms'), list)
    assert len(detail['terms']) >= 1


def test_list_de_contrasts(client, db_with_de):
    db_id, study = db_with_de
    detail = client.get_de_study(db_id, study['study_id'])
    term = detail['terms'][0]['term']
    contrasts = client.list_de_contrasts(db_id, study['study_id'], term)
    assert isinstance(contrasts, list)
    assert len(contrasts) >= 1
    for key in ('contrast', 'n_genes', 'n_sig_padj_lt_0.05'):
        assert key in contrasts[0]


def test_get_de_rows(client, db_with_de):
    db_id, study = db_with_de
    detail = client.get_de_study(db_id, study['study_id'])
    term = detail['terms'][0]['term']
    contrasts = client.list_de_contrasts(db_id, study['study_id'], term)
    contrast = contrasts[0]['contrast']
    rows = client.get_de_rows(db_id, study['study_id'], term, contrast,
                              limit=5)
    assert isinstance(rows, list)
    if rows:
        for key in ('gene_name', 'pvalue', 'padj'):
            assert key in rows[0]


def test_get_de_rows_dataframe(client, db_with_de):
    """``format='dataframe'`` returns a DataFrame when pandas is available."""
    try:
        import pandas as pd   # noqa: F401
    except ImportError:
        pytest.skip('pandas not installed in the test env')
    db_id, study = db_with_de
    detail = client.get_de_study(db_id, study['study_id'])
    term = detail['terms'][0]['term']
    contrasts = client.list_de_contrasts(db_id, study['study_id'], term)
    contrast = contrasts[0]['contrast']
    df = client.get_de_rows(db_id, study['study_id'], term, contrast,
                            limit=3, format='dataframe')
    import pandas as pd
    assert isinstance(df, pd.DataFrame)


def test_get_de_by_gene(client, db_with_de):
    """Find any gene from a known contrast and check ``get_de_by_gene``."""
    db_id, study = db_with_de
    detail = client.get_de_study(db_id, study['study_id'])
    term = detail['terms'][0]['term']
    contrasts = client.list_de_contrasts(db_id, study['study_id'], term)
    rows = client.get_de_rows(db_id, study['study_id'], term,
                              contrasts[0]['contrast'], limit=1)
    if not rows:
        pytest.skip(f"no rows in {study['study_id']}/{term}/{contrasts[0]['contrast']}")
    gene = rows[0]['gene_name']
    out = client.get_de_by_gene(db_id, gene)
    assert isinstance(out, list)
    assert len(out) >= 1


def test_get_de_by_gene_unknown(client, db_with_de):
    """An unknown gene returns an empty list, not a 500."""
    db_id, _ = db_with_de
    out = client.get_de_by_gene(db_id, 'NOPE_THIS_GENE_DOES_NOT_EXIST')
    assert out == []


# ── plot endpoint smoke ───────────────────────────────────────────────────────


def test_get_plot_known_geom(client):
    """Pick the first dataset that has at least one embedding; render a tiny
    scatter to confirm the per-geom URL flow works."""
    for db in client.get_databases():
        info = client.get_database_info(db['id'])
        if info.get('embeddings'):
            result = client.get_plot(db['id'], 'scatter', dpi=72, width=4, height=3)
            assert result['format'] == 'png'
            assert result['bytes']
            return
    pytest.skip('no dataset with embeddings on this server')
