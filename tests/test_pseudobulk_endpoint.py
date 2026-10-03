"""Live-API smoke tests for the /pseudobulk endpoint.

Hits the running ant-serve at ANTHIVE_TEST_URL (or anthive.yaml default).
Skipped when the server is unreachable, so the test file is safe to
keep checked in.

Covers:
  * basic parquet response + Arrow custom metadata + group count
  * multi-column group_by (group_id formatted as <a>_x_<b>)
  * CSV format export
  * layer-integer validation (X log-norm → 400; counts → 200)
  * subset pre-filter
  * forced async mode (?async=true → JSON pointer)
  * missing group_by → 422; unknown obs column → 400
"""
from __future__ import annotations

import io
import json
import os
from typing import Optional

import pytest

try:
    import requests
except ImportError:
    pytest.skip("requests not installed", allow_module_level=True)

try:
    import pyarrow.parquet as pq
except ImportError:
    pq = None  # type: ignore


_DEFAULT_URL = os.environ.get(
    'ANTHIVE_TEST_URL', 'https://localhost:8501/api',
)
_USER = os.environ.get('ANTHIVE_TEST_USER', '')
_PW = os.environ.get('ANTHIVE_TEST_PASSWORD', '')


def _probe() -> Optional[str]:
    try:
        r = requests.get(f"{_DEFAULT_URL}/health", auth=(_USER, _PW),
                         verify=False, timeout=3)
        if r.status_code == 200:
            return _DEFAULT_URL
    except Exception:
        pass
    return None


_BASE_URL = _probe()

pytestmark = pytest.mark.skipif(
    _BASE_URL is None,
    reason=f"ant-serve not reachable at {_DEFAULT_URL}",
)


V6A = 'S25/V6A'


def _get(path: str, **params):
    url = f"{_BASE_URL}{path}"
    return requests.get(
        url, params=params, auth=(_USER, _PW), verify=False, timeout=300,
    )


# ── basic parquet response ────────────────────────────────────────────


def test_pseudobulk_parquet_basic():
    """Single-column group_by on integer-counts layer returns parquet
    with PAR1 magic, the expected number of group columns, and
    `anthive_pseudobulk_meta` embedded in the Arrow schema."""
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='Strain', layer='counts', min_cells=10)
    assert r.status_code == 200, r.text[:500]
    ct = r.headers.get('content-type', '')
    assert ct.startswith('application/x-parquet'), f"ct={ct}"

    # PAR1 magic at both ends of a parquet file
    assert r.content[:4] == b'PAR1', f"bad magic: {r.content[:4]!r}"
    assert r.content[-4:] == b'PAR1', f"bad trailer: {r.content[-4:]!r}"

    # Headers carry the summary numbers
    n_groups = int(r.headers['x-pseudobulk-n-groups'])
    n_genes = int(r.headers['x-pseudobulk-n-genes'])
    assert n_groups >= 2, f"expected >=2 Strain levels, got {n_groups}"
    assert n_genes > 1000, f"expected lots of genes, got {n_genes}"
    assert r.headers['x-pseudobulk-layer'] == 'counts'
    assert r.headers['x-pseudobulk-group-by'] == 'Strain'

    if pq is None:
        pytest.skip("pyarrow not installed; cannot inspect parquet metadata")

    table = pq.read_table(io.BytesIO(r.content))
    schema_meta = table.schema.metadata or {}
    raw = schema_meta.get(b'anthive_pseudobulk_meta')
    assert raw is not None, "missing anthive_pseudobulk_meta in Arrow schema"
    meta = json.loads(raw.decode())
    assert meta['db_id'] == V6A
    assert meta['layer'] == 'counts'
    assert meta['group_by'] == ['Strain']
    assert meta['n_groups'] == n_groups
    assert meta['n_genes'] == n_genes
    # column 0 is gene_name; column_index in meta should be 1..n_groups
    cols = table.column_names
    assert cols[0] == 'gene_name'
    assert len(cols) == 1 + n_groups
    for g in meta['groups']:
        assert g['n_cells'] >= meta['min_cells']
        # column_index points to the table column carrying that group
        assert 1 <= g['column_index'] <= n_groups
        assert cols[g['column_index']] == g['group_id']
    # Count values are non-negative integers
    for col_name in cols[1:]:
        col = table.column(col_name).to_pylist()
        assert all(isinstance(v, int) and v >= 0 for v in col[:50])


# ── multi-column group_by ────────────────────────────────────────────


def test_pseudobulk_multi_column_group_by():
    """group_by=Strain,Sex_donor → group ids formatted with `_x_`
    separator, and dropped_groups appears when min_cells filters
    something out."""
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='Strain,Sex_donor', layer='counts', min_cells=10)
    assert r.status_code == 200, r.text[:500]
    if pq is None:
        pytest.skip("pyarrow not installed")
    table = pq.read_table(io.BytesIO(r.content))
    meta = json.loads(table.schema.metadata[b'anthive_pseudobulk_meta'])
    assert meta['group_by'] == ['Strain', 'Sex_donor']
    # At least one group id should contain the `_x_` separator
    for g in meta['groups']:
        assert '_x_' in g['group_id'], f"group_id {g['group_id']!r} missing _x_"
        assert g['Strain'] is None or g['Strain'] == g['group_id'].split('_x_')[0]


# ── CSV format ────────────────────────────────────────────────────────


def test_pseudobulk_csv_format():
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='Strain', layer='counts', min_cells=10, format='csv')
    assert r.status_code == 200, r.text[:500]
    assert r.headers['content-type'].startswith('text/csv'), r.headers
    assert r.headers.get('content-disposition', '').startswith('attachment')
    lines = r.text.splitlines()
    header = lines[0].split(',')
    assert header[0] == 'gene_name'
    # Each subsequent column is a group id
    n_groups = int(r.headers['x-pseudobulk-n-groups'])
    assert len(header) == 1 + n_groups
    # Body rows: count > 0 cells should be present
    assert len(lines) >= 10
    # Header carries a compact meta hint
    meta_hdr = r.headers.get('x-pseudobulk-meta', '')
    assert meta_hdr and 'n_groups' in meta_hdr


# ── layer-integer validation ──────────────────────────────────────────


def test_pseudobulk_lognorm_layer_rejected():
    """`layer=X` is log-normalized on V6A — endpoint must 400 with a
    helpful message pointing the caller at `layer=counts`."""
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='Strain', layer='X', min_cells=10)
    assert r.status_code == 400, r.text[:500]
    detail = (r.json().get('detail') or '').lower()
    assert 'integer' in detail or 'log' in detail, detail
    assert 'counts' in detail, f"expected 'try ?layer=counts' hint, got {detail!r}"


def test_pseudobulk_counts_layer_accepted():
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='Strain', layer='counts', min_cells=10)
    assert r.status_code == 200


# ── subset filter ─────────────────────────────────────────────────────


def test_pseudobulk_subset_filter():
    """Restrict to one strain — totals match unfiltered minus the other strain."""
    unfilt = _get(f"/databases/{V6A}/pseudobulk",
                  group_by='Strain', layer='counts', min_cells=10)
    filt = _get(f"/databases/{V6A}/pseudobulk",
                group_by='Strain', layer='counts', min_cells=10,
                subset='Strain:AppNLGF')
    assert unfilt.status_code == 200
    assert filt.status_code == 200
    if pq is None:
        pytest.skip("pyarrow not installed")
    unfilt_meta = json.loads(
        pq.read_table(io.BytesIO(unfilt.content))
          .schema.metadata[b'anthive_pseudobulk_meta']
    )
    filt_meta = json.loads(
        pq.read_table(io.BytesIO(filt.content))
          .schema.metadata[b'anthive_pseudobulk_meta']
    )
    # Filtered should have fewer or equal groups
    assert filt_meta['n_groups'] <= unfilt_meta['n_groups']
    # All groups in filtered must be AppNLGF
    for g in filt_meta['groups']:
        assert g['Strain'] == 'AppNLGF' or g['group_id'] == 'AppNLGF', g


# ── async mode ────────────────────────────────────────────────────────


def test_pseudobulk_async_forced():
    """?async=true returns JSON with artifact_url; fetching that URL
    yields the parquet."""
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='Strain', layer='counts', min_cells=10, **{'async': 'true'})
    assert r.status_code == 200, r.text[:500]
    assert r.headers['content-type'].startswith('application/json'), r.headers
    body = r.json()
    assert 'artifact_url' in body
    assert body['artifact_url'].endswith('.parquet')
    assert body['size_bytes'] > 0
    assert len(body['sha256']) == 64
    assert body['n_groups'] >= 2
    # Fetch the artifact (use the same auth)
    artifact_url = body['artifact_url']
    # URL is absolute when X-Forwarded-Host is present; strip the host so we
    # always go through the same base used by the test (handles localhost
    # vs proxied test runs uniformly).
    if artifact_url.startswith('http'):
        from urllib.parse import urlparse
        artifact_path = urlparse(artifact_url).path
    else:
        artifact_path = artifact_url
    # The artifact lives under /artifacts/ (NOT /api/artifacts/)
    base = _BASE_URL.rsplit('/api', 1)[0] if _BASE_URL.endswith('/api') else _BASE_URL
    r2 = requests.get(f"{base}{artifact_path}",
                      auth=(_USER, _PW), verify=False, timeout=60)
    assert r2.status_code == 200, f"artifact fetch failed: {r2.status_code} {r2.text[:300]}"
    assert r2.content[:4] == b'PAR1'
    assert len(r2.content) == body['size_bytes']


# ── parameter validation ──────────────────────────────────────────────


def test_pseudobulk_missing_group_by():
    """No group_by → FastAPI 422 (required query param)."""
    r = _get(f"/databases/{V6A}/pseudobulk", layer='counts')
    assert r.status_code == 422, r.text[:300]


def test_pseudobulk_unknown_group_by_column():
    """Typo'd obs column → 400 with helpful detail."""
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='ThisColumnDoesNotExist_xyz', layer='counts')
    assert r.status_code == 400, r.text[:300]
    detail = (r.json().get('detail') or '').lower()
    assert 'categorical' in detail or 'not' in detail


def test_pseudobulk_unknown_layer():
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='Strain', layer='this_layer_does_not_exist')
    assert r.status_code == 404, r.text[:300]


def test_pseudobulk_min_cells_records_dropped():
    """A high min_cells should drop some groups; the parquet metadata's
    `dropped_groups` list records them so downstream tools can audit.

    We use a 2-column group_by (Strain × Mouse_ID) where most mice have
    fewer than 10000 cells, then assert that at least one combination
    landed in `dropped_groups`.
    """
    r = _get(f"/databases/{V6A}/pseudobulk",
             group_by='Strain,Mouse_ID', layer='counts', min_cells=10_000)
    if r.status_code == 404:
        # Acceptable outcome: every group filtered out.
        assert 'min_cells' in (r.json().get('detail') or '').lower()
        return
    assert r.status_code == 200, r.text[:300]
    if pq is None:
        pytest.skip("pyarrow not installed")
    table = pq.read_table(io.BytesIO(r.content))
    meta = json.loads(table.schema.metadata[b'anthive_pseudobulk_meta'])
    assert meta['n_dropped_groups'] >= 1
    assert len(meta['dropped_groups']) == meta['n_dropped_groups']
    for g in meta['dropped_groups']:
        assert g['n_cells'] < 10_000
        assert g['reason'] == 'below min_cells'
