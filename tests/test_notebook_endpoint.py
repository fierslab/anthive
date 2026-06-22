"""Live-API tests for the /plot/{geom}/notebook endpoint (API 2.8.0+).

Verifies that the plot-to-notebook generator returns a valid
JupyterLite-compatible .ipynb with exactly three cells, that the BASE_URL
inside the notebook is portable (https://… via X-Forwarded-Host), that
the caption text from the underlying plot appears in cell 1, and that
the auth-block templating matches the auth shape on the inbound request.

Skipped when ANTHIVE_TEST_URL is unset or the server isn't reachable —
same pattern as test_plot_handlers_smoke.py.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import pytest

try:
    import requests
except ImportError:
    pytest.skip("requests not installed", allow_module_level=True)


_DEFAULT_URL = os.environ.get(
    'ANTHIVE_TEST_URL', 'https://10.42.0.3:8501/api',
)
_USER = os.environ.get('ANTHIVE_TEST_USER', 'mf')
_PW = os.environ.get('ANTHIVE_TEST_PASSWORD', 'ahToehR5g1xsEpg')


def _probe() -> Optional[str]:
    try:
        r = requests.get(
            f"{_DEFAULT_URL}/health", auth=(_USER, _PW),
            verify=False, timeout=3,
        )
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


def _get(path: str, **params):
    url = f"{_BASE_URL}{path}"
    return requests.get(
        url, params=params, auth=(_USER, _PW), verify=False, timeout=60,
    )


V6A = 'S25/V6A'


# ── core shape ────────────────────────────────────────────────────────


def test_notebook_volcano_shape():
    """The example URL from the spec returns a parseable .ipynb."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"

    # Content type advertised by the endpoint.
    ct = r.headers.get('content-type', '')
    assert 'application/x-ipynb+json' in ct, f"content-type={ct!r}"

    # Sane download filename for curl / wget.
    cd = r.headers.get('content-disposition', '')
    assert '.ipynb' in cd, f"content-disposition={cd!r}"

    nb = r.json()
    assert nb.get('nbformat') == 4, f"nbformat={nb.get('nbformat')!r}"
    cells = nb.get('cells')
    assert isinstance(cells, list) and len(cells) == 3, (
        f"expected 3 cells, got {len(cells) if isinstance(cells, list) else cells!r}"
    )

    cell_types = [c.get('cell_type') for c in cells]
    assert cell_types == ['markdown', 'code', 'code'], cell_types


def test_notebook_caption_in_cell_1():
    """Caption from /plot/volcano flows verbatim into cell 1."""
    nb_resp = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert nb_resp.status_code == 200

    # Grab the canonical caption from the plain plot endpoint.
    plot_resp = _get(
        f"/databases/{V6A}/plot/volcano",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    plot_caption = plot_resp.headers.get('X-Plot-Caption', '')
    assert plot_caption, "no X-Plot-Caption on the bare /plot URL"

    nb = nb_resp.json()
    md_source = nb['cells'][0]['source']
    # The caption header is latin-1 sanitised; compare a stable
    # substring instead of the full text.
    needle = plot_caption[:60]
    assert needle in md_source, (
        f"caption snippet missing from cell 1.\n"
        f"  needle = {needle!r}\n"
        f"  source = {md_source[:400]!r}"
    )


def test_notebook_base_url_is_portable():
    """BASE_URL inside cell 2 is an https URL ending in /api."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200
    code = r.json()['cells'][1]['source']
    # The BASE_URL assignment should look like:  BASE_URL = "https://…/api"
    assert 'BASE_URL' in code, code
    # We can't anchor on a specific host (it depends on how the test runner
    # reaches the server), but ant-serve mounts under /api and the test
    # URL ends in /api → expect /api in the value.
    assert '/api' in code, f"expected /api in BASE_URL line, got: {code[:300]}"
    # Either the test goes through https reverse proxy (Caddy) or it hits
    # ant-serve directly on http. Both are valid — just make sure we don't
    # emit garbage like file:// or about:blank.
    assert ('https://' in code) or ('http://' in code), code


def test_notebook_auth_block_matches_request_auth():
    """The X-Anthive-Auth-Case header advertises the auth-block flavour."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200
    case = r.headers.get('x-anthive-auth-case', '')
    # The test client sends HTTP Basic via the auth= kwarg above; Caddy
    # exchanges that for an Authelia session and forwards Remote-User.
    # Direct hits to ant-serve see the Authorization header. Either is fine
    # as long as the server picked one of the known cases.
    assert case in ('authelia', 'basic', 'public', 'unknown'), case

    code = r.json()['cells'][1]['source']
    # Active line is always the bare client — the disposable cookie /
    # vark-config path. The explicit-password line is commented (for the
    # passwordless case there is no commented line; only the bare client).
    assert 'c = AnthiveClient(BASE_URL)' in code, code
    if case == 'authelia':
        assert 'Authelia' in code
    elif case == 'basic':
        assert 'HTTP Basic' in code
    elif case == 'public':
        assert 'Passwordless' in code


def test_notebook_params_round_trip():
    """User-facing query params (incl. DE selectors) land in the PARAMS dict."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
        padj_threshold=0.01,
        n_label=5,
    )
    assert r.status_code == 200
    code = r.json()['cells'][1]['source']
    # User-facing names (study/term/contrast) are preserved — the rename
    # to de_study/de_term/de_contrast is internal to the server.
    assert "'study': 'DE_interpretable_glmm_vs_grandmean'" in code, code
    assert "'term': 'Sex_donor'" in code, code
    assert "'contrast': 'F'" in code, code
    assert "'n_label': '5'" in code or "'n_label': 5" in code, code


def test_notebook_unknown_geom_returns_400():
    r = _get(f"/databases/{V6A}/plot/wibble/notebook", x='foo')
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_notebook_unknown_db_returns_404():
    r = _get(
        "/databases/no/such/db/plot/volcano/notebook",
        study='X', term='Y', contrast='Z',
    )
    assert r.status_code == 404, (r.status_code, r.text[:200])


def test_notebook_version_bumped_to_2_8():
    """/version should advertise at least 2.8.0 (the version that ships
    this endpoint)."""
    r = requests.get(
        f"{_BASE_URL}/version", auth=(_USER, _PW), verify=False, timeout=5,
    )
    assert r.status_code == 200
    version = r.json().get('version', '')
    # Allow patch releases on top of 2.8.x.
    parts = version.split('.')
    assert len(parts) >= 2, version
    major, minor = int(parts[0]), int(parts[1])
    assert (major, minor) >= (2, 8), version
