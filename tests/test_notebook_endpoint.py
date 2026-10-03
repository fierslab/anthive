"""Live-API tests for the /plot/{geom}/notebook endpoint (API 2.11.0+).

Verifies that the plot-to-notebook generator returns a valid
JupyterLite-compatible .ipynb with the new 8-cell layout (markdown intro,
auth cell, params cell, render cell, data-pull intro + cell, de-novo
intro + snippet cell). Also verifies:

- BASE_URL inside the notebook is portable (https://… via X-Forwarded-Host),
- the caption text from the underlying plot appears in cell 0,
- PLOT_PARAMS / RENDER_PARAMS are split correctly (biology vs visual),
- the auth cell is generic (no Authelia / basic-auth phrasing in the cell),
- every supported geom (incl. the 4 DE geoms) emits a de-novo snippet
  that AST-parses cleanly,
- non-empty params dicts render multi-line, empty dicts stay single-line.

Skipped when ANTHIVE_TEST_URL is unset or the server isn't reachable —
same pattern as test_plot_handlers_smoke.py.
"""
from __future__ import annotations

import ast
import json
import os
import re
from typing import Optional

import pytest

try:
    import requests
except ImportError:
    pytest.skip("requests not installed", allow_module_level=True)


_DEFAULT_URL = os.environ.get(
    'ANTHIVE_TEST_URL', 'https://localhost:8501/api',
)
_USER = os.environ.get('ANTHIVE_TEST_USER', '')
_PW = os.environ.get('ANTHIVE_TEST_PASSWORD', '')


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


# API 2.11.0+ cell order. Indices used throughout the rest of the file.
_INTRO_MD = 0
_AUTH_CODE = 1
_PARAMS_CODE = 2
_RENDER_CODE = 3
_DATA_MD = 4
_DATA_CODE = 5
_DENOVO_MD = 6
_DENOVO_CODE = 7
_EXPECTED_TYPES = [
    'markdown', 'code', 'code', 'code',
    'markdown', 'code', 'markdown', 'code',
]


def test_notebook_cell_order_8_cells():
    """API 2.11.0+: notebook is exactly 8 cells with the documented types."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"

    ct = r.headers.get('content-type', '')
    assert 'application/x-ipynb+json' in ct, f"content-type={ct!r}"

    cd = r.headers.get('content-disposition', '')
    assert '.ipynb' in cd, f"content-disposition={cd!r}"

    nb = r.json()
    assert nb.get('nbformat') == 4, f"nbformat={nb.get('nbformat')!r}"
    cells = nb.get('cells')
    assert isinstance(cells, list) and len(cells) == 8, (
        f"expected 8 cells, got {len(cells) if isinstance(cells, list) else cells!r}"
    )

    cell_types = [c.get('cell_type') for c in cells]
    assert cell_types == _EXPECTED_TYPES, cell_types


def test_notebook_caption_in_intro_cell():
    """Caption from /plot/volcano flows verbatim into the intro markdown cell."""
    nb_resp = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert nb_resp.status_code == 200

    plot_resp = _get(
        f"/databases/{V6A}/plot/volcano",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    plot_caption = plot_resp.headers.get('X-Plot-Caption', '')
    assert plot_caption, "no X-Plot-Caption on the bare /plot URL"

    nb = nb_resp.json()
    md_source = nb['cells'][_INTRO_MD]['source']
    needle = plot_caption[:60]
    assert needle in md_source, (
        f"caption snippet missing from intro cell.\n"
        f"  needle = {needle!r}\n"
        f"  source = {md_source[:400]!r}"
    )


def test_notebook_base_url_is_portable():
    """BASE_URL inside the auth cell is an http(s) URL ending in /api."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200
    code = r.json()['cells'][_AUTH_CODE]['source']
    assert 'BASE_URL' in code, code
    assert '/api' in code, f"expected /api in BASE_URL line, got: {code[:300]}"
    assert ('https://' in code) or ('http://' in code), code


def test_notebook_auth_cell_is_generic():
    """The auth cell mentions neither 'Authelia' nor a specific password.

    Generic phrasing is enforced — the same cell works for public,
    JupyterLite/cookie, vark-config, and explicit-credential cases.
    """
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200
    code = r.json()['cells'][_AUTH_CODE]['source']

    assert 'Authelia' not in code, code
    # No hard-coded password from the test fixture.
    assert not _PW or _PW not in code, "test password leaked into the auth cell"

    # The active line works for all deployment cases.
    assert 'c = AnthiveClient(BASE_URL)' in code, code

    # Documentation URLs for pyvark + vark.
    assert 'codeberg.org/mfiers/pyvark' in code, code
    assert 'codeberg.org/mfiers/vark' in code, code

    # The X-Anthive-Auth-Case header is still emitted as telemetry.
    case = r.headers.get('x-anthive-auth-case', '')
    assert case in ('authelia', 'basic', 'public', 'unknown'), case


def test_notebook_jupyterlite_detection_via_sys_modules():
    """The auth cell uses ``"pyodide" in sys.modules`` (not try/except)
    to detect JupyterLite before calling micropip.install("pyvark").
    """
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200
    code = r.json()['cells'][_AUTH_CODE]['source']

    assert '"pyodide" in sys.modules' in code, code
    assert 'await micropip.install("pyvark")' in code, code
    # No misleading try/except → micropip pattern.
    assert 'except ImportError' not in code, code


# ── params: PLOT_PARAMS vs RENDER_PARAMS split ────────────────────────────


def _params_cell(nb: dict) -> str:
    return nb['cells'][_PARAMS_CODE]['source']


def test_notebook_uses_dict_call_syntax():
    """PLOT_PARAMS + RENDER_PARAMS are emitted as ``dict(...)``, not ``{...}``."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200
    code = _params_cell(r.json())
    assert re.search(r'PLOT_PARAMS\s*=\s*dict\(', code), code
    assert re.search(r'RENDER_PARAMS\s*=\s*dict\(', code), code


@pytest.mark.parametrize("key,expected_bucket", [
    ('study', 'PLOT_PARAMS'),
    ('term', 'PLOT_PARAMS'),
    ('contrast', 'PLOT_PARAMS'),
    ('padj_threshold', 'PLOT_PARAMS'),  # biology: hypothesis-level threshold
    ('n_label', 'RENDER_PARAMS'),       # visual: label count
    ('log2fc_clip', 'RENDER_PARAMS'),   # visual: colour-scale clip
])
def test_notebook_param_split_biology_vs_render(key: str, expected_bucket: str):
    """Specific params land in the expected bucket."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
        padj_threshold=0.01,
        n_label=5,
        log2fc_clip=3,
    )
    assert r.status_code == 200
    code = _params_cell(r.json())

    # Find which dict() block the key lands in.
    m_plot = re.search(r'PLOT_PARAMS\s*=\s*dict\((?P<body>[^)]*)\)', code, re.DOTALL)
    m_render = re.search(r'RENDER_PARAMS\s*=\s*dict\((?P<body>[^)]*)\)', code, re.DOTALL)
    assert m_plot and m_render, code

    plot_body = m_plot.group('body')
    render_body = m_render.group('body')

    in_plot = f"{key}=" in plot_body
    in_render = f"{key}=" in render_body

    if expected_bucket == 'PLOT_PARAMS':
        assert in_plot, f"{key!r} expected in PLOT_PARAMS, code:\n{code}"
        assert not in_render, f"{key!r} unexpectedly in RENDER_PARAMS, code:\n{code}"
    else:
        assert in_render, f"{key!r} expected in RENDER_PARAMS, code:\n{code}"
        assert not in_plot, f"{key!r} unexpectedly in PLOT_PARAMS, code:\n{code}"


# ── de-novo snippet: AST-parses per geom ──────────────────────────────────


@pytest.mark.parametrize("geom,extra", [
    ('scatter', {'x': 'X_umap', 'color': 'P2RY12'}),
    ('hexbin', {'x': 'X_umap'}),
    ('violin', {'x': 'donor_sex', 'y': 'P2RY12'}),
    ('box', {'x': 'donor_sex', 'y': 'P2RY12'}),
    ('bar', {'x': 'donor_sex'}),
    ('histogram', {'x': 'P2RY12'}),
    ('ecdf', {'x': 'P2RY12'}),
    ('kde', {'x': 'P2RY12'}),
    ('kde2d', {'x': 'P2RY12', 'y': 'CX3CR1'}),
    ('heatmap', {'genes': 'P2RY12,CX3CR1', 'group_by': 'donor_sex'}),
    ('rolling', {'x': 'P2RY12', 'y': 'CX3CR1'}),
    ('volcano', {'study': 'DE_interpretable_glmm_vs_grandmean',
                 'term': 'Sex_donor', 'contrast': 'F'}),
    ('ma',      {'study': 'DE_interpretable_glmm_vs_grandmean',
                 'term': 'Sex_donor', 'contrast': 'F'}),
    ('forest',  {'study': 'DE_interpretable_glmm_vs_grandmean',
                 'term': 'Sex_donor', 'gene': 'P2RY12'}),
    ('de_heatmap', {'study': 'DE_interpretable_glmm_vs_grandmean',
                    'term': 'Sex_donor'}),
])
def test_denovo_snippet_ast_parses(geom: str, extra: dict):
    """Every supported geom emits a de-novo snippet that AST-parses cleanly.

    API 2.11.1+: all 15 geoms (including the 4 DE geoms) ship real
    matplotlib code; the older "stub pointing at lib/plotting.py"
    branch is gone.
    """
    r = _get(f"/databases/{V6A}/plot/{geom}/notebook", **extra)
    # 4xx OK if a geom-specific param is missing; the AST check is only
    # meaningful when the notebook actually built.
    if r.status_code != 200:
        pytest.skip(f"server rejected {geom} with {r.status_code}: {r.text[:200]}")

    nb = r.json()
    snippet = nb['cells'][_DENOVO_CODE]['source']
    try:
        ast.parse(snippet)
    except SyntaxError as exc:
        pytest.fail(f"de-novo snippet for {geom!r} doesn't parse: {exc}\n---\n{snippet}")

    # Sanity: every snippet imports matplotlib.
    assert 'matplotlib' in snippet, f"{geom!r} snippet missing matplotlib:\n{snippet}"


# ── params: multi-line dict() formatting (API 2.11.1+) ─────────────────────


def test_notebook_plot_params_dict_is_multiline():
    """Non-empty PLOT_PARAMS renders one kwarg per line, 4-space indented."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200
    code = _params_cell(r.json())

    m = re.search(
        r'PLOT_PARAMS\s*=\s*dict\((?P<body>.*?)\n\)', code, re.DOTALL,
    )
    assert m, f"PLOT_PARAMS dict() not found in:\n{code}"
    body = m.group('body')
    # Each kwarg lives on its own 4-space-indented line.
    assert '\n    ' in body, (
        f"expected multi-line 4-space-indented kwargs; body was:\n{body!r}"
    )
    # And a trailing comma after the last kwarg (sits just before the
    # closing newline + paren that re.search consumed).
    assert body.rstrip().endswith(','), (
        f"expected trailing comma on last kwarg; body was:\n{body!r}"
    )


def test_notebook_empty_render_params_is_oneline():
    """Empty RENDER_PARAMS stays on a single line as ``dict()``."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200
    code = _params_cell(r.json())

    # Find the RENDER_PARAMS assignment and check the value is exactly
    # ``dict()`` with no embedded newline.
    m = re.search(r'^RENDER_PARAMS\s*=\s*(.+)$', code, re.MULTILINE)
    assert m, f"RENDER_PARAMS line not found in:\n{code}"
    value = m.group(1).strip()
    assert value == 'dict()', (
        f"expected RENDER_PARAMS = dict() on one line, got: {value!r}\n"
        f"full cell:\n{code}"
    )


# ── back-compat (kept from older tests) ───────────────────────────────────


def test_notebook_params_round_trip():
    """User-facing query params land in either PLOT_PARAMS or RENDER_PARAMS."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
        padj_threshold=0.01,
        n_label=5,
    )
    assert r.status_code == 200
    code = _params_cell(r.json())

    # The internal de_study/de_term/de_contrast rename is invisible to the
    # notebook reader — we keep the user-facing names.
    assert "study='DE_interpretable_glmm_vs_grandmean'" in code, code
    assert "term='Sex_donor'" in code, code
    assert "contrast='F'" in code, code
    # Coerced or raw — either form is fine.
    assert ("n_label=5" in code) or ("n_label='5'" in code), code


def test_notebook_unknown_geom_returns_400():
    r = _get(f"/databases/{V6A}/plot/wibble/notebook", x='foo')
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_notebook_unknown_db_returns_404():
    r = _get(
        "/databases/no/such/db/plot/volcano/notebook",
        study='X', term='Y', contrast='Z',
    )
    assert r.status_code == 404, (r.status_code, r.text[:200])


def test_notebook_version_bumped_to_2_11():
    """/version should advertise at least 2.11.0 (the version that ships
    the 8-cell notebook layout + PLOT/RENDER split)."""
    r = requests.get(
        f"{_BASE_URL}/version", auth=(_USER, _PW), verify=False, timeout=5,
    )
    assert r.status_code == 200
    version = r.json().get('version', '')
    parts = version.split('.')
    assert len(parts) >= 2, version
    major, minor = int(parts[0]), int(parts[1])
    assert (major, minor) >= (2, 11), version


def test_notebook_carries_biology_title_and_filename_headers():
    """API 2.9.0+ (kept): notebook carries X-Plot-Title and X-Plot-Filename."""
    r = _get(
        f"/databases/{V6A}/plot/volcano/notebook",
        study='DE_interpretable_glmm_vs_grandmean',
        term='Sex_donor',
        contrast='F',
    )
    assert r.status_code == 200

    title = r.headers.get('x-plot-title', '')
    filename = r.headers.get('x-plot-filename', '')
    assert title, "X-Plot-Title header missing"
    assert filename, "X-Plot-Filename header missing"
    assert 'Sex_donor' in title, title
    assert 'F' in title, title
    assert 'volcano' in title.lower(), title

    assert re.fullmatch(r'[A-Za-z0-9_\-]+', filename), filename
    assert 'volcano' in filename
    assert 'Sex_donor' in filename

    cd = r.headers.get('content-disposition', '')
    assert f'{filename}.ipynb' in cd, cd

    nb = r.json()
    h1_source = nb['cells'][_INTRO_MD]['source']
    assert title in h1_source, h1_source[:200]
