"""
Web browser routes for Anthive

Provides HTML interface for browsing single-cell datasets.
Data is sourced entirely from the ant-serve REST API.
"""

import base64
import html as html_lib
import re
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote as url_quote, unquote, urlencode

import markdown
import requests
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pathlib import Path

# lib/ is one level up from web/
_lib_dir = Path(__file__).parent.parent / 'lib'
if str(_lib_dir) not in sys.path:
    sys.path.insert(0, str(_lib_dir))
from facet_aliases import canonical_facet  # noqa: E402
import help_content  # noqa: E402
import celltype_groups  # noqa: E402

from canonical_columns import is_canonical_column, strip_ant_prefix  # noqa: E402

router = APIRouter()
templates_dir = Path(__file__).parent / 'templates'
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.filters['urlencode'] = url_quote


_DEFAULT_ISSUE_TRACKER_URL = 'https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/issues'
_issue_tracker_cfg: list = []      # [value] once read from anthive.yaml


def _issue_tracker_url() -> str:
    """User-menu "Report an issue" target ('' hides the entry). The env var
    wins and is read per render; the config value is read once."""
    import os  # noqa: PLC0415
    if 'ANTHIVE_ISSUE_TRACKER_URL' in os.environ:
        return os.environ['ANTHIVE_ISSUE_TRACKER_URL'].strip()
    if not _issue_tracker_cfg:
        try:
            from config import get_issue_tracker_url, load_config  # noqa: PLC0415
            _issue_tracker_cfg.append(get_issue_tracker_url(load_config()))
        except Exception:  # config unavailable (tests import this module bare)
            _issue_tracker_cfg.append(_DEFAULT_ISSUE_TRACKER_URL)
    return _issue_tracker_cfg[0]


templates.env.globals['issue_tracker_url'] = _issue_tracker_url

_DEFAULT_DOCS_BASE_URL = 'https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/blob/main/doc'
_docs_base_cfg: list = []          # [value] once read from anthive.yaml


def _docs_base_url() -> str:
    """Folder of the detailed docs the help pages link to (no trailing /)."""
    if not _docs_base_cfg:
        try:
            from config import get_docs_base_url, load_config  # noqa: PLC0415
            _docs_base_cfg.append(get_docs_base_url(load_config()))
        except Exception:  # config unavailable (tests import this module bare)
            _docs_base_cfg.append(_DEFAULT_DOCS_BASE_URL)
    return _docs_base_cfg[0]


templates.env.globals['docs_base_url'] = _docs_base_url


def _help_topic_html(topic_id: str) -> Markup:
    """Body of a help topic (and its child subsections) for the help page."""
    return Markup(help_content.topic_html(topic_id, _docs_base_url()))


def _help_glossary_html() -> Markup:
    """The glossary entries of ``lib/help_content`` as ``<dt>/<dd>`` pairs."""
    return Markup(help_content.glossary_html(_docs_base_url()))


templates.env.globals['help_topic_html'] = _help_topic_html
templates.env.globals['help_glossary_html'] = _help_glossary_html


# ── Stack version for the top-bar badge ──────────────────────────────────────
#
# The badge shows the running API's version (GET /health). It is fetched in a
# background thread and cached, so a page render never waits on the API.

def _read_web_version() -> str:
    try:
        text = (_lib_dir / '__init__.py').read_text()
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
        return match.group(1) if match else ''
    except OSError:
        return ''


_WEB_VERSION = _read_web_version()
_VERSION_TTL_OK = 120.0
_VERSION_TTL_FAIL = 15.0
_version_state: Dict[str, Any] = {'api': None, 'ok': False, 'checked': 0.0, 'refreshing': False}
_version_lock = threading.Lock()


def _refresh_api_version(api_url: str) -> None:
    api_version, ok = None, False
    try:
        response = requests.get(f"{api_url.rstrip('/')}/health", timeout=3)
        response.raise_for_status()
        api_version = (response.json() or {}).get('api_version') or None
        ok = bool(api_version)
    except Exception:
        pass
    with _version_lock:
        _version_state.update(api=api_version if ok else _version_state['api'], ok=ok,
                              checked=time.monotonic(), refreshing=False)


def _stack_version(request: Request) -> Dict[str, str]:
    """Version badge data: shown text, tooltip and whether the API answered."""
    now = time.monotonic()
    with _version_lock:
        ttl = _VERSION_TTL_OK if _version_state['ok'] else _VERSION_TTL_FAIL
        stale = now - _version_state['checked'] > ttl
        start = stale and not _version_state['refreshing']
        if start:
            _version_state['refreshing'] = True
        api_version, ok, checked = _version_state['api'], _version_state['ok'], _version_state['checked']
    if start:
        try:
            api_url = request.app.state.api_url
            threading.Thread(target=_refresh_api_version, args=(api_url,), daemon=True).start()
        except Exception:
            with _version_lock:
                _version_state['refreshing'] = False
    web = _WEB_VERSION or 'unknown'
    if ok and api_version:
        title = f"Anthive version {api_version} (API)"
        title += f", web {web}" if web != api_version else ", web the same"
        return {'shown': api_version, 'title': title + ". Click for the changelog.", 'ok': 'yes'}
    reason = 'API unreachable' if checked else 'API version not checked yet'
    return {'shown': web, 'title': f"Anthive web {web} ({reason}). Click for the changelog.", 'ok': ''}


templates.env.globals['stack_version'] = _stack_version


def _obs_col_info(name: str) -> dict:
    """Return display metadata for an obs column name.

    Used as a Jinja global so templates can decide how to render a column
    label without importing Python logic.

    Args:
        name: The raw obs column name (e.g. ``'ant_braak'`` or ``'cell_type'``).

    Returns:
        A dict with keys:
        - ``is_canonical`` (bool): True when the name matches the ``ant_<tag>`` pattern.
        - ``tag`` (str | None): The bare tag without prefix (``'braak'``), or None.
        - ``display_name`` (str): Label to show — stripped prefix for canonical cols,
          unchanged otherwise.
    """
    canonical = is_canonical_column(name)
    return {
        'is_canonical': canonical,
        'tag': strip_ant_prefix(name) if canonical else None,
        'display_name': strip_ant_prefix(name) if canonical else name,
    }


templates.env.globals['obs_col_info'] = _obs_col_info

# The (white) anthive logo, base64-inlined once at import so the About page
# stays self-contained — no static/ dir or asset route (see web/CLAUDE.md).
# It's a grayscale+alpha PNG (white line-art), meant for a dark backdrop.
try:
    _LOGO_DATA_URI = 'data:image/png;base64,' + base64.b64encode(
        (Path(__file__).parent / 'anthive_logo.png').read_bytes()
    ).decode('ascii')
except Exception:  # asset missing — page still renders, just without the mark
    _LOGO_DATA_URI = ''

# Tiny dark ant-head mark for the topbar (every page). Registered as a Jinja
# global so base.html can use it without each route threading it through.
try:
    templates.env.globals['topbar_logo'] = 'data:image/png;base64,' + base64.b64encode(
        (Path(__file__).parent / 'topbar_logo.png').read_bytes()
    ).decode('ascii')
except Exception:
    templates.env.globals['topbar_logo'] = ''

# Chat-assistant mascot (RGBA PNG), shown in the chat window's empty state when
# a fresh conversation opens. Base64-inlined once, same self-contained pattern.
try:
    templates.env.globals['chat_logo'] = 'data:image/png;base64,' + base64.b64encode(
        (Path(__file__).parent / 'chat_logo.png').read_bytes()
    ).decode('ascii')
except Exception:
    templates.env.globals['chat_logo'] = ''

# Favicons / PWA manifest live under web/favicons/ (tracked — NOT the gitignored
# webapp/ build dir) and are served by the app at their conventional root paths.
# The reverse proxy routes unmatched root paths here (see config/Caddyfile,
# config/nginx.conf), so these resolve at /favicon.ico etc.
_FAVICON_DIR = Path(__file__).parent / 'favicons'
_FAVICON_FILES = {
    'favicon.ico': 'image/x-icon',
    'favicon.svg': 'image/svg+xml',
    'favicon-96x96.png': 'image/png',
    'apple-touch-icon.png': 'image/png',
    'web-app-manifest-192x192.png': 'image/png',
    'web-app-manifest-512x512.png': 'image/png',
    'site.webmanifest': 'application/manifest+json',
}


def _make_favicon_route(filename: str, media_type: str):
    async def _serve():
        return FileResponse(_FAVICON_DIR / filename, media_type=media_type)
    return _serve


for _fav_name, _fav_media in _FAVICON_FILES.items():
    router.add_api_route(f"/{_fav_name}", _make_favicon_route(_fav_name, _fav_media),
                         methods=["GET"], include_in_schema=False)


# ── API helpers ────────────────────────────────────────────────────────────────

def _api_get(api_url: str, path: str, headers: Optional[Dict[str, str]] = None,
             timeout: float = 15) -> Any:
    """GET request to the ant-serve API"""
    url = f"{api_url.rstrip('/')}{path}"
    response = requests.get(url, timeout=timeout, headers=headers)
    response.raise_for_status()
    return response.json()


# Identity headers the proxy sets from the SSO session. Forwarded on the few
# server-side fetches whose answer depends on who asks (for example the
# facet mapping, which shows obs_hidden author columns to admins only).
_IDENTITY_HEADERS = ('Remote-User', 'Remote-Groups')


def _api_get_as_user(request: Request, api_url: str, path: str,
                     timeout: Optional[float] = None) -> Any:
    """``_api_get`` on behalf of the signed-in user (identity headers kept)."""
    headers = {name: request.headers[name] for name in _IDENTITY_HEADERS
               if request.headers.get(name)}
    kwargs: Dict[str, Any] = {}
    if headers:
        kwargs['headers'] = headers
    if timeout is not None:
        kwargs['timeout'] = timeout
    return _api_get(api_url, path, **kwargs)


def _api_url(request: Request) -> str:
    return request.app.state.api_url


# TTL for the genesets-availability probe: cache True for 60 s, False for 5 s.
# This means a startup race self-heals within 5 s on the next request without
# requiring a browse restart.
_GENESETS_TTL_TRUE = 60.0
_GENESETS_TTL_FALSE = 5.0

# Max distinct values before we suppress the Categorical-tab plot links and
# top-N breakdown for a column. High-cardinality categoricals (e.g. ant_donor
# with ~460 donors, barcode with n_cells values, ant_author_cluster with
# hundreds of dataset-specific labels) produce useless bar charts and don't
# fit the page. The column stays filterable through the search box; only the
# inline plot affordance and top-N cell go away above this threshold.
CATEGORICAL_PLOT_MAX_DISTINCT = 50


def _probe_genesets_once(api_url: str) -> bool:
    """Probe /genesets on ant-serve; return True iff it responds 200.

    Args:
        api_url: Base URL for the ant-serve REST API.

    Returns:
        True when the geneset store is reachable and configured.
    """
    try:
        resp = requests.get(f"{api_url.rstrip('/')}/genesets", timeout=3)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def refresh_genesets_available(app_state) -> bool:
    """Update ``app_state.genesets_available`` with a TTL-cached probe.

    Caches True for 60 s and False for only 5 s so a startup race (browse
    boots before ant-serve is ready) self-heals on the next page load.

    Args:
        app_state: The FastAPI ``app.state`` object; reads and writes
            ``genesets_available``, ``_genesets_probe_ts``, and ``api_url``.

    Returns:
        The (possibly refreshed) boolean availability value.
    """
    now = time.monotonic()
    last_ts: float = getattr(app_state, '_genesets_probe_ts', 0.0)
    current: bool = getattr(app_state, 'genesets_available', False)

    ttl = _GENESETS_TTL_TRUE if current else _GENESETS_TTL_FALSE
    if now - last_ts >= ttl:
        current = _probe_genesets_once(app_state.api_url)
        app_state.genesets_available = current
        app_state._genesets_probe_ts = now

    return current


def _reports(request: Request) -> list:
    return getattr(request.app.state, 'reports', [])


# On-disk root of the .md / .h5ad.mus / .h5ad files. Used to read frontmatter
# fields ant-serve doesn't currently expose (publication_status) and to stat
# dataset files for the on-disk-mtime fallback in _dataset_date. Also serves
# per-collection static/ downloads (see _group_static_files below).
try:
    from config import get_data_folder as _get_data_folder
    _H5ADSTORE_ROOT = _get_data_folder()
except Exception:  # config unavailable (tests import this module bare)
    _H5ADSTORE_ROOT = Path(__file__).parent.parent / 'data' / 'h5adstore'


# ── misc helpers ───────────────────────────────────────────────────────────────

def _as_list(value) -> list:
    """Normalise a scalar-or-list field to a Python list."""
    if not value:
        return []
    return value if isinstance(value, list) else [value]


# Embeddings that are scVI / PCA / other latent spaces produce dense-blob
# scatter plots and are not useful for the visual chip row on the dataset
# page. Filter them out. Keep UMAP / tSNE / FDG / diffmap / pymde / spatial /
# custom names.
_LATENT_EMBEDDING_RE = re.compile(
    r'^(x_)?(pca|scvi|scanvi|harmony|bbknn|latent|hvg|counts_pca)($|_)',
    re.IGNORECASE,
)


def _is_visual_embedding(name: str) -> bool:
    """True when this obsm key is a meaningful 2D layout to plot; False for
    latent spaces (X_scVI, X_pca, scvi_latents, latent_z, X_harmony, …)."""
    if not name:
        return False
    return _LATENT_EMBEDDING_RE.match(str(name)) is None


# When a microglia dataset has no precomputed top_genes, fall back to this
# curated list of canonical microglia markers so the Explore chip row still
# has something clickable. The template labels the section as "generic"
# markers so we're not implying they're this dataset's own signature.
_MICROGLIA_MARKER_FALLBACK = [
    "CX3CR1", "P2RY12", "TMEM119", "C1QA", "C1QB", "C1QC", "CSF1R",
    "ITGAM", "AIF1", "TREM2", "TYROBP", "HEXB", "CST3", "CTSD",
    "APOE", "SPP1", "GPNMB", "IL1B", "TNF", "FCGR3A",
]

_ASTROCYTE_MARKER_FALLBACK = [
    "GFAP", "AQP4", "S100B", "ALDH1L1", "SLC1A2", "SLC1A3", "GJA1",
    "SOX9", "VIM", "GLUL", "FGFR3", "MLC1", "CLU", "APOE", "SPARCL1",
    "CD44", "MFGE8", "MERTK", "HES5", "AGT",
]

_NEURON_MARKER_FALLBACK = [
    "RBFOX3", "SNAP25", "SYP", "MAP2", "NEFL", "NEFM", "NEFH", "TUBB3",
    "SYT1", "VAMP2", "GRIA1", "GRIN1", "SLC17A7", "GAD1", "GAD2", "DLG4",
    "CAMK2A", "HOMER1", "NEUROD2", "STMN2",
]

_MICROGLIA_CELLTYPE_TERMS = {"microglial cell", "microglia"}
_ASTROCYTE_CELLTYPE_TERMS = {"astrocyte", "astrocytes"}
_NEURON_CELLTYPE_TERMS   = {"neuron", "neurons", "gabaergic neuron",
                            "glutamatergic neuron", "cortical neuron"}


def _resolve_gene_panel(top_genes, cell_type_term):
    """Return (genes: List[str], source: str) for the Genes tab.

    Args:
        top_genes: Curated marker list (may be empty/None).
        cell_type_term: Either a single cell-type string, a list of strings
            (from `.mus` files that store comma-separated types), or None.
            The value is coerced to strings and each candidate is checked
            against the microglia / astrocyte / neuron term sets — the first
            matching entry picks the fallback source.

    Returns:
        Tuple of (list of gene symbols, source label). ``source`` ∈
        ``{'precomputed', 'microglia_fallback', 'astrocyte_fallback',
        'neuron_fallback', 'none'}``.
    """
    if top_genes:
        return list(top_genes), 'precomputed'

    # `.mus` files may carry cell_type as a JSON list (comma-separated types
    # in CellxGene-imported datasets). Normalise to an iterable of strings.
    if cell_type_term is None:
        candidates: List[str] = []
    elif isinstance(cell_type_term, list):
        candidates = [str(entry) for entry in cell_type_term if entry]
    else:
        candidates = [str(cell_type_term)]

    for term in candidates:
        lowered = term.strip().lower()
        if not lowered:
            continue
        if lowered in _MICROGLIA_CELLTYPE_TERMS:
            return list(_MICROGLIA_MARKER_FALLBACK), 'microglia_fallback'
        if lowered in _ASTROCYTE_CELLTYPE_TERMS:
            return list(_ASTROCYTE_MARKER_FALLBACK), 'astrocyte_fallback'
        if lowered in _NEURON_CELLTYPE_TERMS:
            return list(_NEURON_MARKER_FALLBACK), 'neuron_fallback'
    return [], 'none'


# Embed a live plot thumbnail in markdown with a compact scheme:
#     ![caption](plot:GEOM?param=value&...)
# On a dataset page the target dataset is implicit; in collection markdown (or
# to reference another dataset) add ``&db=<group>/<file>``. The rendered markdown
# is post-processed here: the <img> becomes a clickable thumbnail whose src is
# the plot PNG endpoint (small width/height/dpi injected if absent) wrapped in a
# link to the full interactive plot page. Authors get the token from the plot
# page's "copy thumbnail" button, so they never hand-write it.
_PLOT_IMG_RE = re.compile(r'<img\b[^>]*\bsrc="plot:([^"]*)"[^>]*>', re.IGNORECASE)
_PLOT_ALT_RE = re.compile(r'\balt="([^"]*)"', re.IGNORECASE)
_PLOT_THUMB_DEFAULTS = {'width': '4', 'height': '3', 'dpi': '80'}


def _expand_plot_thumbnails(html_text: Optional[str], default_db: Optional[str] = None) -> Optional[str]:
    """Rewrite ``<img src="plot:GEOM?QUERY">`` into a clickable plot thumbnail.

    No-op for images without the ``plot:`` scheme, and leaves the tag untouched
    if no dataset can be resolved (no ``db=`` and no ``default_db``)."""
    if not html_text or 'src="plot:' not in html_text:
        return html_text

    def _sub(match: "re.Match") -> str:
        tag = match.group(0)
        raw = html_lib.unescape(match.group(1))          # 'GEOM?QUERY' (&amp; → &)
        alt_match = _PLOT_ALT_RE.search(tag)
        alt = alt_match.group(1) if alt_match else ''
        geom, _, query_string = raw.partition('?')
        geom = geom.strip()
        if not geom:
            return tag
        params = dict(parse_qsl(query_string, keep_blank_values=False))
        db_id = (params.pop('db', '') or default_db or '').strip()
        params.pop('format', None)
        if '/' not in db_id:
            # unresolved dataset (collection markdown without &db=…) → guide, not a broken img
            return ('<span class="muted" style="font-size:12px">[plot thumbnail needs '
                    '<code>&amp;db=group/file</code>]</span>')
        group, file = db_id.split('/', 1)
        group_q, file_q, geom_q = url_quote(group, safe=''), url_quote(file, safe=''), url_quote(geom, safe='')
        # interactive-page link keeps the author's params verbatim
        page_query = urlencode(params)
        # thumbnail image: author's params + small-size defaults + png
        img_params = dict(params)
        for key, value in _PLOT_THUMB_DEFAULTS.items():
            img_params.setdefault(key, value)
        img_params['format'] = 'png'
        src = f"/api/databases/{group_q}/{file_q}/plot/{geom_q}?{urlencode(img_params)}"
        href = f"/browse/{group_q}/{file_q}/plot/{geom_q}" + (f"?{page_query}" if page_query else '')
        esc = lambda value: html_lib.escape(value, quote=True)
        return (f'<a href="{esc(href)}" class="md-plot-link" title="open interactive plot">'
                f'<img class="md-plot-thumb" src="{esc(src)}" alt="{esc(alt)}" loading="lazy"></a>')

    return _PLOT_IMG_RE.sub(_sub, html_text)


# Module-score column classification.
#
# Score sidecar columns show up in /info's numerical obs list under one of
# these name conventions (see lib/anthive_db._score_col_name):
#   scanpy/geneset : "Sierksma2025::WGCNA::Brown_Lysosome"
#   starcat        : "starcat__MICROGLIA_V1__DAM_pos"
#   ucell          : "ucell__Sierksma2025/WGCNA__Brown_Lysosome"
#   aucell         : "aucell__Sierksma2025/WGCNA__Brown_Lysosome"  (legacy,
#                    folded into ucell on read since 2026-08-26 — the parquet
#                    layer still tolerates it but the UI never surfaces it)
#
# _classify_score_column parses one column name; _group_module_scores
# collapses a full list into a per-module structure exposing which methods
# actually scored it on this dataset, so the dataset page can render a
# unified "module scores" table with per-method chips.

_STARCAT_PREFIX = 'starcat__'
_AUCELL_PREFIX = 'aucell__'
_UCELL_PREFIX = 'ucell__'


def _classify_score_column(col: str) -> Optional[Dict[str, str]]:
    """Return {method, catalog, module} for a score column, else None.

    ``catalog`` is the human-facing catalog id (``Sierksma2025/WGCNA`` or
    ``MICROGLIA_V1``); ``module`` is the module / program name.

    Legacy ``aucell__`` columns are silently folded into the ``ucell`` method
    so the UI only ever surfaces one rank-based method label.
    """
    if col.startswith(_STARCAT_PREFIX):
        rest = col[len(_STARCAT_PREFIX):]
        catalog, sep, module = rest.rpartition('__')
        if not sep or not catalog or not module:
            return None
        return {'method': 'starcat', 'catalog': catalog, 'module': module}
    if col.startswith(_UCELL_PREFIX):
        rest = col[len(_UCELL_PREFIX):]
        catalog, sep, module = rest.rpartition('__')
        if not sep or not catalog or not module:
            return None
        return {'method': 'ucell', 'catalog': catalog, 'module': module}
    if col.startswith(_AUCELL_PREFIX):
        rest = col[len(_AUCELL_PREFIX):]
        catalog, sep, module = rest.rpartition('__')
        if not sep or not catalog or not module:
            return None
        # Legacy aucell rows fold into ucell for display. The raw column
        # name is still what the caller uses to plot, so we keep it as-is
        # if a consumer walks back to it, but the method label is ucell.
        return {'method': 'ucell', 'catalog': catalog, 'module': module}
    # scanpy / geneset: "<study>::<experiment>::<module>"
    parts = col.split('::')
    if len(parts) == 3 and all(p for p in parts):
        return {
            'method': 'scanpy',
            'catalog': f"{parts[0]}/{parts[1]}",
            'module': parts[2],
        }
    return None


def _group_module_scores(numerical_cols: List[str]) -> Dict[str, Any]:
    """Group score columns by (catalog, module).

    Returns a dict with:
      - ``modules``: list of {catalog, module, key, methods}. ``methods`` maps
        one of ``scanpy|starcat|ucell`` to the actual obs-column name.
      - ``non_score_cols``: list of column names that are NOT module scores
        (native numerical obs, n_counts, pseudotime, and so on).
      - ``method_counts``: {method: int} number of modules scored by that method.
    """
    by_key: Dict[tuple, Dict[str, Any]] = {}
    non_score: List[str] = []
    for col in numerical_cols:
        info = _classify_score_column(col)
        if info is None:
            non_score.append(col)
            continue
        key = (info['catalog'], info['module'])
        entry = by_key.setdefault(key, {
            'catalog': info['catalog'],
            'module':  info['module'],
            'key':     f"{info['catalog']}/{info['module']}",
            'methods': {},
        })
        entry['methods'][info['method']] = col

    # Deterministic order: catalog then module.
    modules = sorted(by_key.values(), key=lambda m: (m['catalog'].lower(), m['module'].lower()))
    counts = {
        'scanpy':  sum(1 for m in modules if 'scanpy'  in m['methods']),
        'starcat': sum(1 for m in modules if 'starcat' in m['methods']),
        'ucell':   sum(1 for m in modules if 'ucell'   in m['methods']),
    }
    return {
        'modules':        modules,
        'non_score_cols': non_score,
        'method_counts':  counts,
    }


def _accession_url(accession: str) -> Optional[str]:
    """Return a browse URL for a known accession prefix, else None."""
    s = str(accession).upper()
    if s.startswith(('GSE', 'GSM', 'GPL')):
        return f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}"
    if s.startswith(('PRJNA', 'PRJEB')):
        return f"https://www.ncbi.nlm.nih.gov/bioproject/{accession}"
    if s.startswith(('E-MTAB', 'E-GEOD')):
        return f"https://www.ebi.ac.uk/biostudies/arrayexpress/studies/{accession}"
    return None


def format_number(value: float) -> str:
    """Format a number for display."""
    if value == 0:
        return "0"
    abs_value = abs(value)
    if abs_value >= 1e6 or (abs_value < 1e-3 and abs_value != 0):
        return f"{value:.2e}"
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}.0"
    if abs_value >= 100:
        return f"{value:,.1f}"
    elif abs_value >= 10:
        return f"{value:,.2f}"
    elif abs_value >= 1:
        return f"{value:,.3f}"
    return f"{value:,.4f}"


# ── metadata summary helpers ───────────────────────────────────────────────────

_ORGANISM_ABBREV = {
    'human': 'Hs', 'homo sapiens': 'Hs',
    'mouse': 'Mm', 'mus musculus': 'Mm',
    'rat': 'Rn', 'rattus norvegicus': 'Rn',
    'zebrafish': 'Dr', 'danio rerio': 'Dr',
    'drosophila': 'Dm', 'drosophila melanogaster': 'Dm',
    'macaque': 'Mf', 'rhesus macaque': 'Mf', 'macaca fascicularis': 'Mf',
    'c. elegans': 'Ce', 'caenorhabditis elegans': 'Ce',
}

_TECHNOLOGY_ABBREV = {
    '10x_chromium': '10x', '10x': '10x', '10x chromium': '10x',
    '10x_chromium_3p_v2': '10x v2', '10x_chromium_3p_v3': '10x v3',
    '10x_chromium_5p': '10x 5p', '10x_multiome': 'multi',
    '10x_visium': 'vis', '10x_xenium': 'xen',
    '10x_scrna': '10x', '10x_snrna': '10x-sn',
    'smart-seq2': 'SS2', 'smartseq2': 'SS2', 'smart_seq2': 'SS2',
    'smart-seq3': 'SS3', 'cel-seq2': 'CEL2',
    'parse_evercode': 'Parse', 'bd_rhapsody': 'BD',
    'mars-seq': 'MARS', 'microwell-seq': 'µwell',
    'visium': 'vis',
    'xenium': 'xen',
    'multiome': 'mult',
    'snrna-seq': 'snRNA', 'snrna_seq': 'snRNA', 'snrna-seq (10x)': 'snRNA',
    'scrna-seq': 'scRNA', 'scrna_seq': 'scRNA',
    'slide-seq': 'slide', 'slide_seq': 'slide', 'slide-tag': 'slide',
    'slide-seqv2': 'slide2', 'slide-tags': 'slide',
    'proteomics': 'prot', 'mass-spectrometry': 'MS',
    'novast': 'NovaST',
    'bulk_rna-seq': 'bulk',
    'merfish': 'MER', 'cosmx': 'CosMx', 'stereo-seq': 'Stereo',
}

# Cell-type abbreviations — match the mus joint vocab (Cell Ontology labels).
# Keys are lower-cased to match _summarize_meta_field's lookup.
_CELLTYPE_ABBREV = {
    'microglial cell': 'MG',
    'macrophage': 'MΦ',
    'dendritic cell': 'DC',
    'monocyte': 'Mono',
    'astrocyte': 'Astro',
    'oligodendrocyte': 'Oligo',
    'oligodendrocyte precursor cell': 'OPC',
    'neuron': 'Neu',
    'excitatory neuron': 'ExN',
    'inhibitory neuron': 'InN',
    'endothelial cell': 'Endo',
    'pericyte': 'Peri',
    'fibroblast': 'Fibro',
    'ependymal cell': 'Epend',
    'vascular leptomeningeal cell': 'VLMC',
    'myeloid cell': 'Myel',
    'glial cell': 'Glia',
    'mixed': 'mix',
}

# Tissue / brain-region abbreviations — UBERON-aligned mus vocab.
_TISSUE_ABBREV = {
    'brain': 'brain',
    'cortex': 'ctx',
    'prefrontal cortex': 'PFC',
    'dorsolateral prefrontal cortex': 'DLPFC',
    'motor cortex': 'M1',
    'occipital cortex': 'OC',
    'temporal cortex': 'TC',
    'parietal cortex': 'PC',
    'middle temporal gyrus': 'MTG',
    'angular gyrus': 'AG',
    'entorhinal cortex': 'EC',
    'midtemporal cortex': 'midTC',
    'occipitotemporal cortex': 'OTC',
    'hippocampus': 'Hipp',
    'striatum': 'Str',
    'basal ganglia': 'BG',
    'caudate nucleus': 'caud',
    'thalamus': 'Thal',
    'hypothalamus': 'HyTh',
    'cerebellum': 'Cb',
    'cerebellar microglia aging': 'Cb',  # quirk
    'midbrain': 'MB',
    'brainstem': 'BS',
    'substantia nigra': 'SN',
    'facial nucleus': 'fNuc',
    'olfactory bulb': 'OB',
    'corpus callosum': 'CC',
    'white matter': 'WM',
    'white matter of frontal lobe': 'WMf',
    'white matter of occipital lobe': 'WMo',
    'white matter of parietal lobe': 'WMp',
    'white matter of temporal lobe': 'WMt',
    'white matter of cerebellum': 'WMc',
    'cervical spinal cord white matter': 'cSC-WM',
    'subventricular zone': 'SVZ',
    'spinal cord': 'SpC',
    'cortical layer 2/3': 'L2/3',
    'grey matter': 'GM',
    'tumor': 'tum',
    # Non-CNS
    'blood': 'blood',
    'bone marrow': 'BM',
    'lung': 'lung',
    'liver': 'liver',
    'kidney': 'kid',
    'heart': 'heart',
    'retina': 'retina',
    'skin': 'skin',
    'csf': 'CSF',
    # Brodmann
    'brodmann (1909) area 4':  'BA4',
    'brodmann (1909) area 7':  'BA7',
    'brodmann (1909) area 9':  'BA9',
    'brodmann (1909) area 17': 'BA17',
}

# Disease abbreviations — MONDO-aligned mus vocab.
_DISEASE_ABBREV = {
    'normal': 'norm',
    'alzheimer disease': 'AD',
    'parkinson disease': 'PD',
    'amyotrophic lateral sclerosis': 'ALS',
    'multiple sclerosis': 'MS',
    'huntington disease': 'HD',
    'frontotemporal dementia': 'FTD',
    'down syndrome': 'DS',
    'autism spectrum disorder': 'ASD',
    'schizophrenia': 'SCZ',
    'glioma': 'glioma',
    'glioblastoma': 'GBM',
    'covid-19': 'COVID',
    'viral encephalitis': 'VE',
    'hippocampal sclerosis': 'HS',
    'focal cortical dysplasia': 'FCD',
    "leukoencephalopathy, diffuse hereditary, with spheroids 1": 'LDHS1',
}

# Suspension type — distinguishes whole-cell vs nuclei vs non-cellular
_SUSPENSION_ABBREV = {
    'cell': 'cell',
    'nucleus': 'nucl',
    'na': '—',
}

# Access — small icon: 🔒 internal (lab-private), 🌐 public (shareable).
_ACCESS_ABBREV = {
    'internal': '🔒',
    'public': '🌐',
}

# Source — internal/external (lab-generated vs imported from elsewhere).
# Emoji fallback: 🧪 (test tube) for lab-generated; ↓ (arrow) for imported.
# Matches the inline-SVG glyph used in `_ACCESS_SVG_INTERNAL` — a lab beaker,
# NOT a house (which read as a "home" link).
_SOURCE_ABBREV = {
    'internal': '🧪',
    'external': '↓',
}


# ── unified access + publication-status icon ─────────────────────────────────
# One glyph replaces the old two-icon (access + source) system. Shape encodes
# access (public → globe, internal → lab beaker / Erlenmeyer flask); colour
# encodes publication_status (unpublished → muted red, published → muted
# green). The "internal" glyph used to be a house; that was read as a link to
# the home page, so it's now a lab beaker to signal "lab-generated" data.
# Container width is fixed so row alignment survives regardless of glyph.
#
# publication_status is NOT exposed by ant-serve (2026-08) — read it directly
# from the collection's ``data/h5adstore/<group>/index.md`` YAML frontmatter.
# One-line grep-parse; cached at import so we don't stat 60 files per request.

# Muted palette, subdued not saturated.
_PUB_COLOR_PUBLISHED   = '#335C67'    # deep teal (palette; matches topbar)
_PUB_COLOR_UNPUBLISHED = '#9E2A2B'    # muted red (palette)

# Inline SVG icons — a filled disc in the status colour with the access glyph
# drawn in WHITE on top. The disc uses fill:currentColor so callers still set
# the status colour via the outer span's `style="color:…"` (same contract as
# before). Colour-emoji fonts would override `color:` — this design keeps
# emoji-safe rendering.
# 16×16 viewBox; ap-icon container is 1em wide so alignment is stable.
_ACCESS_SVG_PUBLIC = (
    '<svg viewBox="0 0 16 16" width="1em" height="1em" '
    'aria-hidden="true">'
    # filled colored disc (fills the viewBox)
    '<circle cx="8" cy="8" r="8" fill="currentColor"/>'
    # globe glyph in white on top
    '<g fill="none" stroke="white" stroke-width="1.2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="8" cy="8" r="4.6"/>'
    '<ellipse cx="8" cy="8" rx="2.1" ry="4.6"/>'
    '<path d="M3.4 8 h9.2 M4.2 5.4 h7.6 M4.2 10.6 h7.6"/>'
    '</g>'
    '</svg>'
)
_ACCESS_SVG_INTERNAL = (
    '<svg viewBox="0 0 16 16" width="1em" height="1em" '
    'aria-hidden="true">'
    # filled colored disc
    '<circle cx="8" cy="8" r="8" fill="currentColor"/>'
    # Erlenmeyer (conical) lab flask in white on top:
    #   - short horizontal cap across the neck top
    #   - two vertical neck lines
    #   - triangular body flaring out to a wide base
    #   - a meniscus line partway up the body suggesting liquid level
    '<g fill="none" stroke="white" stroke-width="1.2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M6.2 3.6 h3.6"/>'
    '<path d="M6.6 3.6 v3.4 L4.0 12.4 h8.0 L9.4 7.0 V3.6"/>'
    '<path d="M5.3 9.6 h5.4"/>'
    '</g>'
    '</svg>'
)
_ACCESS_SVG = {'public': _ACCESS_SVG_PUBLIC, 'internal': _ACCESS_SVG_INTERNAL}


def _access_pub_icon(access: Optional[str], publication_status: Optional[str]) -> Dict[str, str]:
    """Return {svg, color, tooltip, access, publication} for the unified icon.

    Missing ``access`` defaults to ``internal`` (the safer default — a dataset
    without explicit access is not shareable). Missing ``publication_status``
    means "published" (unpublished must be explicitly set).

    The rendered glyph is inline SVG with ``fill: currentColor`` so CSS
    ``color:`` applies — color-emoji fonts would override it, which is the
    bug this replaces.
    """
    acc = (access or 'internal').lower()
    if acc not in _ACCESS_SVG:
        acc = 'internal'
    pub_raw = (publication_status or '').lower()
    unpublished = pub_raw == 'unpublished'
    svg = _ACCESS_SVG[acc]
    color = _PUB_COLOR_UNPUBLISHED if unpublished else _PUB_COLOR_PUBLISHED
    access_word = 'external' if acc == 'public' else 'internal'
    pub_word = 'unpublished' if unpublished else 'published'
    return {
        'svg': svg,
        # Retain 'glyph' for backward compatibility — same SVG string.
        'glyph': svg,
        'color': color,
        'access': access_word,
        'publication': pub_word,
        'tooltip': f'{access_word} · {pub_word}',
    }


# ── technology classification ──────────────────────────────────────────────
# Map the many technology strings to a small set of top-level classes shown
# as a "Tech" column on the landing / folder pages. Keep this deliberately
# coarse — the fine-grained tech name is still in the abbrev chip and the
# tooltip; this is for grouping/filtering only.

_TECH_CLASS_MAP: Dict[str, str] = {
    # sc / sn RNA — single-cell / single-nucleus RNA
    '10x_chromium': 'sc/sn RNA',
    '10x': 'sc/sn RNA',
    '10x_chromium_3p_v2': 'sc/sn RNA',
    '10x_chromium_3p_v3': 'sc/sn RNA',
    '10x_chromium_5p': 'sc/sn RNA',
    '10x_multiome': 'sc/sn RNA',
    '10x_scrna': 'sc/sn RNA',
    '10x_snrna': 'sc/sn RNA',
    'smart-seq2': 'sc/sn RNA',
    'smart-seq3': 'sc/sn RNA',
    'cel-seq2': 'sc/sn RNA',
    'parse_evercode': 'sc/sn RNA',
    'bd_rhapsody': 'sc/sn RNA',
    'mars-seq': 'sc/sn RNA',
    'microwell-seq': 'sc/sn RNA',
    'drop-seq': 'sc/sn RNA',
    'snrna-seq': 'sc/sn RNA',
    'scrna-seq': 'sc/sn RNA',
    'snrna-seq (10x)': 'sc/sn RNA',
    'multiome': 'sc/sn RNA',
    # bulk
    'bulk_rna-seq': 'bulk RNA',
    'bulk': 'bulk RNA',
    # proteomics
    'mass-spectrometry': 'proteomics',
    'proteomics': 'proteomics',
    # spatial
    '10x_visium': 'spatial',
    '10x_xenium': 'spatial',
    'visium': 'spatial',
    'xenium': 'spatial',
    'merfish': 'spatial',
    'cosmx': 'spatial',
    'stereo-seq': 'spatial',
    'slide-seq': 'spatial',
    'slide-seqv2': 'spatial',
    'slide-tag': 'spatial',
    'slide-tags': 'spatial',
    'novast': 'spatial',
}


def _tech_class(technology) -> str:
    """Return a coarse top-level class for a technology value.

    Accepts a string, a list of strings, or None. If a list, the first
    class encountered is returned; when the list mixes classes 'mixed' is
    returned. Unknown technologies fall back to 'other'. Empty → ''.
    """
    if technology is None:
        return ''
    if isinstance(technology, list):
        seen: List[str] = []
        for value in technology:
            single = _tech_class(value)
            if single and single not in seen:
                seen.append(single)
        if not seen:
            return ''
        if len(seen) == 1:
            return seen[0]
        return 'mixed'
    text = str(technology).strip().lower()
    if not text:
        return ''
    return _TECH_CLASS_MAP.get(text, 'other')


# Organism display — small mono-letter chip. `Hs` for human, `Mm` for mouse,
# etc. Multi-organism datasets get one chip per organism. Colour scheme uses
# CSS classes (.org-chip.hs / .mm / …) defined in the templates.
_ORGANISM_CHIP: Dict[str, Dict[str, str]] = {
    'human':        {'abbrev': 'Hs', 'cls': 'hs'},
    'homo sapiens': {'abbrev': 'Hs', 'cls': 'hs'},
    'mouse':        {'abbrev': 'Mm', 'cls': 'mm'},
    'mus musculus': {'abbrev': 'Mm', 'cls': 'mm'},
    'rat':          {'abbrev': 'Rn', 'cls': 'rn'},
    'rattus norvegicus': {'abbrev': 'Rn', 'cls': 'rn'},
    'zebrafish':    {'abbrev': 'Dr', 'cls': 'dr'},
    'danio rerio':  {'abbrev': 'Dr', 'cls': 'dr'},
    'drosophila':   {'abbrev': 'Dm', 'cls': 'dm'},
    'drosophila melanogaster': {'abbrev': 'Dm', 'cls': 'dm'},
    'macaque':      {'abbrev': 'Mf', 'cls': 'mf'},
    'rhesus macaque': {'abbrev': 'Mf', 'cls': 'mf'},
    'macaca fascicularis': {'abbrev': 'Mf', 'cls': 'mf'},
    'c. elegans':   {'abbrev': 'Ce', 'cls': 'ce'},
    'caenorhabditis elegans': {'abbrev': 'Ce', 'cls': 'ce'},
}


def _organism_chips(organism) -> List[Dict[str, str]]:
    """Return a list of {abbrev, cls, full} chips for the organism field.

    Accepts a string or list. Unknown organisms get a passthrough chip with
    the raw value as abbrev and cls='other'. Multi-organism datasets return
    multiple chips, deduped in first-seen order.
    """
    if organism is None:
        return []
    if isinstance(organism, list):
        values: List = organism
    elif isinstance(organism, str):
        # Sometimes stored as "human | mouse" or "human, mouse"
        raw = organism.strip()
        if not raw:
            return []
        # Try common list delimiters first
        for sep in ('|', ','):
            if sep in raw:
                values = [v.strip() for v in raw.split(sep) if v.strip()]
                break
        else:
            values = [raw]
    else:
        return []
    out: List[Dict[str, str]] = []
    seen_abbrev: set = set()   # dedup by resulting abbrev (so 'human' and
                                # 'Homo sapiens' collapse to one Hs chip)
    for value in values:
        if not value:
            continue
        key = str(value).strip().lower()
        if not key:
            continue
        chip = _ORGANISM_CHIP.get(key)
        if chip:
            abbrev = chip['abbrev']
            cls = chip['cls']
        else:
            # Unknown — fall back to first 3 chars, class 'other'
            abbrev = str(value)[:3].strip() or '?'
            cls = 'other'
        if abbrev in seen_abbrev:
            continue
        seen_abbrev.add(abbrev)
        out.append({'abbrev': abbrev, 'cls': cls, 'full': str(value)})
    return out


# Cache the publication_status field parsed from data/h5adstore/<group>/index.md
# frontmatter. Loaded lazily on first read per group; each entry is either a
# string ('unpublished', 'published', …) or None (field absent). Cleared by
# calling ``_pub_status_cache.clear()`` if needed. Values are (mtime_ns, status)
# so the cache self-invalidates on file edits — same pattern as
# ``_paper_info_cache`` below.
_pub_status_cache: Dict[str, tuple] = {}

# Cache of paper_title / first_author / last_author parsed from index.md
# frontmatter, keyed by folder name. Value is (mtime_ns, info_dict) — mtime
# lets the cache self-invalidate when the .md is edited (via /admin/edit or a
# direct git pull), without cross-process coupling to ant-serve's PUT hook.
_paper_info_cache: Dict[str, tuple] = {}

# Sentinel values that mean "no paper metadata" for the three fields we care
# about. Case-insensitive.
_PAPER_EMPTY_SENTINELS = {'', 'na', 'n/a', 'none', 'null'}


def _paper_field_absent(value: Optional[str]) -> bool:
    """True when a paper-info field is missing / empty / a common placeholder."""
    if value is None:
        return True
    return str(value).strip().lower() in _PAPER_EMPTY_SENTINELS


def _paper_info_for_group(group_name: Optional[str]) -> Dict[str, str]:
    """Read ``paper_title`` / ``first_author`` / ``last_author`` from
    ``data/h5adstore/<group>/index.md`` frontmatter.

    Returns a dict with (possibly missing) keys ``paper_title``,
    ``first_author``, ``last_author`` — only fields that are present AND not
    an empty/na placeholder are included. Values are stripped strings with
    YAML soft-wrap continuation lines re-joined.

    Uses a tiny hand-rolled scanner (like ``_publication_status_for_group``)
    to avoid pulling in yaml on the landing hot path — but *does* support
    the folded-string continuation lines that longer paper titles are
    routinely wrapped across.
    """
    if not group_name:
        return {}
    path = _H5ADSTORE_ROOT / group_name / 'index.md'
    try:
        cur_mtime = path.stat().st_mtime_ns
    except OSError:
        cur_mtime = 0
    cached = _paper_info_cache.get(group_name)
    if cached and cached[0] == cur_mtime:
        return cached[1]
    result: Dict[str, str] = {}
    wanted = ('paper_title', 'first_author', 'last_author', 'journal')
    try:
        if path.is_file():
            with path.open('r', encoding='utf-8', errors='replace') as fh:
                in_fm = False
                lines: List[str] = []
                for lineno, line in enumerate(fh):
                    if line.startswith('---'):
                        if not in_fm:
                            in_fm = True
                            continue
                        break   # end of frontmatter
                    if in_fm:
                        lines.append(line.rstrip('\n'))
                    if lineno > 1000:   # bail on unreasonably long frontmatter
                        break
            # Walk through lines; when a top-level key we want appears,
            # gather its value plus any continuation lines (indented, not
            # a new key). Stop when a new top-level `key:` is seen.
            index = 0
            while index < len(lines):
                current = lines[index]
                stripped = current.lstrip()
                # Skip list items / continuation of a preceding non-wanted key
                if current and not current[0].isspace() and ':' in stripped:
                    key, _, first_value = stripped.partition(':')
                    key = key.strip().lower()
                    if key in wanted and key not in result:
                        chunks = [first_value.strip()]
                        cursor = index + 1
                        while cursor < len(lines):
                            follow = lines[cursor]
                            if not follow.strip():
                                break
                            # New top-level `key:` ends the value
                            if follow and not follow[0].isspace():
                                break
                            # Continuation line — indented text or list item.
                            # A list-of-authors under 'authors:' shouldn't
                            # reach us; we only look at first_author/last_author
                            # which are scalars, and paper_title which folds.
                            follow_stripped = follow.strip()
                            if follow_stripped.startswith('- '):
                                break   # actually a list — bail
                            chunks.append(follow_stripped)
                            cursor += 1
                        # Reassemble; strip surrounding quotes if any.
                        combined = ' '.join(c for c in chunks if c).strip()
                        if combined.startswith(("'", '"')) and combined.endswith(combined[0]):
                            combined = combined[1:-1].strip()
                        if not _paper_field_absent(combined):
                            result[key] = combined
                        index = cursor
                        continue
                index += 1
    except (OSError, UnicodeError):
        result = {}
    _paper_info_cache[group_name] = (cur_mtime, result)
    return result


def _publication_status_for_group(group_name: Optional[str]) -> Optional[str]:
    """Read ``publication_status`` from ``data/h5adstore/<group>/index.md``.

    ``group_name`` is the folder name (``collection.group`` from the API — the
    long-form like ``Blackburn2026``, NOT the short ``group_id``). Returns the
    frontmatter value (lower-cased) or None if the file/field is missing.
    """
    if not group_name:
        return None
    path = _H5ADSTORE_ROOT / group_name / 'index.md'
    try:
        cur_mtime = path.stat().st_mtime_ns
    except OSError:
        cur_mtime = 0
    cached = _pub_status_cache.get(group_name)
    if cached and cached[0] == cur_mtime:
        return cached[1]
    result: Optional[str] = None
    try:
        if path.is_file():
            # Minimal one-line YAML parse. The frontmatter is small and
            # well-formed; we only need one field. Avoids a yaml dep.
            with path.open('r', encoding='utf-8', errors='replace') as fh:
                in_fm = False
                for lineno, line in enumerate(fh):
                    if line.startswith('---'):
                        if not in_fm:
                            in_fm = True
                            continue
                        break   # end of frontmatter
                    if not in_fm:
                        continue
                    if line.lower().lstrip().startswith('publication_status:'):
                        _, _, value = line.partition(':')
                        result = value.strip().strip("'\"").lower() or None
                        break
                    if lineno > 500:   # frontmatter should be tiny
                        break
    except (OSError, UnicodeError):
        result = None
    _pub_status_cache[group_name] = (cur_mtime, result)
    return result


# ── canonical dataset date ──────────────────────────────────────────────────
# Preferred date shown next to a dataset row: the study's publication year
# when known (and NOT unpublished — otherwise we're claiming a fake publication
# date), else the file's on-disk mtime (upload date). Kept as a small helper so
# both landing + folder templates can render the same value.

import time as _time


def _dataset_date(dataset_info: Dict, group_name: Optional[str] = None,
                  publication_status: Optional[str] = None) -> Dict[str, Any]:
    """Return {iso, sort_ts} — the canonical date + a sortable epoch float.

    Rules (in order):
      1. ``year`` in the dataset .md AND publication_status != 'unpublished'
         → ISO date built from year/month/day (year alone → 'YYYY-01-01').
      2. otherwise → file mtime on disk (upload / last-modification date).
      3. neither available → empty iso, sort_ts = 0.
    """
    pub = (publication_status or '').lower()
    year = dataset_info.get('year')
    # normalize year: API sometimes hands it back as a string
    if isinstance(year, str) and year.isdigit():
        year_int: Optional[int] = int(year)
    elif isinstance(year, int):
        year_int = year
    else:
        year_int = None
    if year_int and pub != 'unpublished':
        month = dataset_info.get('month') or 1
        day = dataset_info.get('day') or 1
        try:
            month = int(month); day = int(day)
        except (TypeError, ValueError):
            month, day = 1, 1
        iso = f'{year_int:04d}-{month:02d}-{day:02d}'
        try:
            ts = _time.mktime((year_int, month, day, 0, 0, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            ts = 0.0
        return {'iso': iso, 'sort_ts': ts}

    # Fall back to on-disk mtime of the .duckdb (best proxy for "upload").
    path_str = dataset_info.get('path') or ''
    if path_str and group_name is not None:
        for candidate in (
            _H5ADSTORE_ROOT / path_str,
            _H5ADSTORE_ROOT / group_name / Path(path_str).name,
        ):
            try:
                if candidate.is_file():
                    mtime = candidate.stat().st_mtime
                    iso = _time.strftime('%Y-%m-%d', _time.localtime(mtime))
                    return {'iso': iso, 'sort_ts': mtime}
            except OSError:
                continue
    return {'iso': '', 'sort_ts': 0.0}


# ── brain-region tree sort ──────────────────────────────────────────────────
# Order tissues by their DFS position in the anthive ontology hierarchy
# (lib/_ontology_hierarchy.yaml). CSF + spinal cord sink to the bottom; anything
# not in the hierarchy sorts alphabetically after everything else. Multi-region
# datasets use their FIRST tissue's DFS position as the sort key.
_ONTOLOGY_PATH = Path(__file__).parent.parent / 'lib' / '_ontology_hierarchy.yaml'


def _load_tissue_hierarchy() -> Dict[str, Optional[str]]:
    """Parse the tissue: block out of ``_ontology_hierarchy.yaml``.

    Minimal parser (no PyYAML in the web scope): reads ``child: parent`` under
    the ``tissue:`` heading. Blank / comment / non-key lines are skipped.
    Returns ``{}`` when the file is absent.
    """
    out: Dict[str, Optional[str]] = {}
    try:
        if not _ONTOLOGY_PATH.is_file():
            return out
        with _ONTOLOGY_PATH.open('r', encoding='utf-8', errors='replace') as fh:
            section: Optional[str] = None
            for line in fh:
                # strip trailing comment
                comment_idx = line.find('#')
                if comment_idx >= 0:
                    line = line[:comment_idx]
                stripped = line.rstrip('\n')
                if not stripped.strip():
                    continue
                # top-level key: no indent, ends in ':'
                if not stripped.startswith(' ') and not stripped.startswith('\t'):
                    key = stripped.rstrip().rstrip(':').strip()
                    section = key if key in ('tissue', 'disease') else None
                    continue
                if section != 'tissue':
                    continue
                # child: parent line under `tissue:`
                content = stripped.strip()
                if ':' not in content:
                    continue
                child, _, parent = content.partition(':')
                child = child.strip().strip('"').strip("'")
                parent = parent.strip().strip('"').strip("'")
                if not child:
                    continue
                out[child.lower()] = parent.lower() if parent and parent.lower() != 'null' else None
    except (OSError, UnicodeError):
        return {}
    return out


# Bottom-drawer tissues (sink to the end but before "unknown"). Lower-case.
_TISSUE_TAIL = ('spinal cord', 'csf')


def _build_tissue_dfs_order() -> Dict[str, int]:
    """Compute a DFS ordering of the tissue hierarchy: {lower(term): rank}.

    Roots are visited in a stable order (brain first, other CNS/organs next,
    tail terms last). Children walked alphabetically for reproducibility.
    Unknown tissues get rank = LARGE_INT so they sort after everything.
    """
    hierarchy = _load_tissue_hierarchy()
    if not hierarchy:
        return {}
    # build parent → children map
    children: Dict[Optional[str], List[str]] = defaultdict(list)
    for child, parent in hierarchy.items():
        children[parent].append(child)
    for lst in children.values():
        lst.sort()   # deterministic
    order: Dict[str, int] = {}
    counter = [0]

    def visit(node: str) -> None:
        if node in order:
            return
        order[node] = counter[0]
        counter[0] += 1
        for child in children.get(node, ()):
            visit(child)

    # Roots (parent == None). Put 'brain' first (the vast majority of the
    # catalog is brain), then the rest alphabetically, tail terms last.
    roots = [r for r in children.get(None, []) if r not in _TISSUE_TAIL]
    if 'brain' in roots:
        roots.remove('brain')
        roots = ['brain'] + sorted(roots)
    else:
        roots = sorted(roots)
    for root in roots:
        visit(root)
    for tail in _TISSUE_TAIL:
        if tail in hierarchy:
            visit(tail)
    return order


_TISSUE_DFS_ORDER: Dict[str, int] = _build_tissue_dfs_order()
_TISSUE_UNKNOWN_RANK = 10_000_000   # sinks unknowns; alpha-sort applied after


def _tissue_sort_key(tissues) -> int:
    """Rank for the first tissue of a dataset by its DFS position in the
    ontology hierarchy. Unknown / missing → ``_TISSUE_UNKNOWN_RANK`` (so they
    sort after every known term; caller adds a secondary alpha sort)."""
    if not tissues:
        return _TISSUE_UNKNOWN_RANK
    first = tissues[0] if isinstance(tissues, list) else tissues
    if not first:
        return _TISSUE_UNKNOWN_RANK
    return _TISSUE_DFS_ORDER.get(str(first).lower(), _TISSUE_UNKNOWN_RANK)


def _tissue_descendants(node: str,
                        hierarchy: Optional[Dict[str, Optional[str]]] = None
                        ) -> set:
    """Return all transitive descendants of ``node`` in the tissue hierarchy
    (child → parent) — the set does NOT include ``node`` itself. Matching is
    case-insensitive on the ontology labels; the returned strings are the
    lower-cased ontology forms."""
    if hierarchy is None:
        hierarchy = _load_tissue_hierarchy()
    if not hierarchy:
        return set()
    target = str(node).lower()
    children: Dict[str, List[str]] = defaultdict(list)
    for child, parent in hierarchy.items():
        if parent:
            children[parent].append(child)
    out: set = set()
    stack = list(children.get(target, ()))
    while stack:
        term = stack.pop()
        if term in out:
            continue
        out.add(term)
        stack.extend(children.get(term, ()))
    return out


def build_tissue_tree(collections: List[Dict]) -> List[Dict]:
    """Return the tissue ontology as a nested tree annotated with per-node
    collection counts.

    Shape::

        [{"name": "brain", "count": 156, "children": [
            {"name": "forebrain", "count": 120, "children": [
                {"name": "cortex", "count": 89, "children": [
                    {"name": "dorsolateral prefrontal cortex", "count": 42,
                     "children": []},
                    ...
                ]}, ...
            ]}, ...
        ]},
         {"name": "tumor", "count": 1, "children": []},
         ...]

    ``count`` is the aggregate number of collections whose (any) tissue value
    falls under that subtree — including the node itself when a collection is
    labelled with the parent term. Collections with tissue values that aren't
    in the hierarchy at all get added as extra ROOT leaves (so a novel
    catalogue term is still filterable). The name field preserves the
    ontology's original casing (as read from the YAML), so the client can
    render user-facing labels; matching against ``data-tissue`` attributes on
    the client is case-insensitive.
    """
    hierarchy = _load_tissue_hierarchy()   # lower-case → lower-case | None
    # Build children map keyed by lower-case names.
    children_lc: Dict[Optional[str], List[str]] = defaultdict(list)
    for child, parent in hierarchy.items():
        children_lc[parent].append(child)
    for lst in children_lc.values():
        lst.sort()

    # Load the original casing of every ontology term (so the UI shows
    # "Brodmann (1909) area 17" rather than a lower-cased version).
    display_name: Dict[str, str] = _load_tissue_display_names()

    # Direct-hit collection counts keyed by lower-case tissue value.
    direct_lc: Dict[str, int] = defaultdict(int)
    catalog_lc_terms: set = set()
    for c in collections:
        full = (c.get('tissue') or {}).get('full') or ''
        if not full:
            continue
        vals = {v.strip().lower() for v in full.split(',') if v.strip()}
        for v in vals:
            direct_lc[v] += 1
            catalog_lc_terms.add(v)

    # Descendants map (lower-case → set of lower-case descendant terms).
    descendants_lc: Dict[str, set] = {}
    for term in hierarchy:
        descendants_lc[term] = _tissue_descendants(term, hierarchy)

    def subtree_count(term_lc: str) -> int:
        """Number of DISTINCT collections whose tissue is term or any descendant."""
        matched: set = set()
        subtree_terms = {term_lc} | descendants_lc.get(term_lc, set())
        for idx, c in enumerate(collections):
            full = (c.get('tissue') or {}).get('full') or ''
            if not full:
                continue
            vals = {v.strip().lower() for v in full.split(',') if v.strip()}
            if vals & subtree_terms:
                matched.add(idx)
        return len(matched)

    def render(term_lc: str) -> Dict:
        kids_lc = children_lc.get(term_lc, [])
        kids = [render(k) for k in kids_lc]
        return {
            'name': display_name.get(term_lc, term_lc),
            'name_lc': term_lc,
            'count': subtree_count(term_lc),
            'children': kids,
        }

    # Roots — same ordering the sort uses: brain first, other CNS/organs
    # alphabetically, tail terms (spinal cord, csf) last.
    roots_hier = [r for r in children_lc.get(None, []) if r not in _TISSUE_TAIL]
    if 'brain' in roots_hier:
        roots_hier.remove('brain')
        roots_hier = ['brain'] + sorted(roots_hier)
    else:
        roots_hier = sorted(roots_hier)
    tail_roots = [t for t in _TISSUE_TAIL if t in hierarchy]

    tree: List[Dict] = []
    for r in roots_hier + tail_roots:
        tree.append(render(r))

    # Orphan catalog terms not covered by the hierarchy — render as root leaves
    # so novel values still filter.
    hierarchy_lc = set(hierarchy.keys())
    orphans = sorted(catalog_lc_terms - hierarchy_lc)
    for orphan in orphans:
        tree.append({
            'name': orphan,
            'name_lc': orphan,
            'count': direct_lc.get(orphan, 0),
            'children': [],
        })
    return tree


def _load_tissue_display_names() -> Dict[str, str]:
    """Return {lower(term): original_casing_term} for every tissue label in
    the ontology YAML. Uses the same minimal parser as
    ``_load_tissue_hierarchy`` so we don't depend on PyYAML."""
    out: Dict[str, str] = {}
    try:
        if not _ONTOLOGY_PATH.is_file():
            return out
        with _ONTOLOGY_PATH.open('r', encoding='utf-8', errors='replace') as fh:
            section: Optional[str] = None
            for line in fh:
                comment_idx = line.find('#')
                if comment_idx >= 0:
                    line = line[:comment_idx]
                stripped = line.rstrip('\n')
                if not stripped.strip():
                    continue
                if not stripped.startswith(' ') and not stripped.startswith('\t'):
                    key = stripped.rstrip().rstrip(':').strip()
                    section = key if key in ('tissue', 'disease') else None
                    continue
                if section != 'tissue':
                    continue
                content = stripped.strip()
                if ':' not in content:
                    continue
                child, _, _parent = content.partition(':')
                child = child.strip().strip('"').strip("'")
                if not child:
                    continue
                out[child.lower()] = child
    except (OSError, UnicodeError):
        return {}
    return out


def _build_tissue_ancestors() -> Dict[str, List[str]]:
    """Return ``{term: [ancestor, …]}`` for every tissue in the ontology.

    The ancestor list runs from the immediate parent up to the root (exclusive
    of the term itself), in bottom-up order.  Terms are stored in their
    original (mixed-case) form from the YAML.  Lower-case matching is the
    caller's responsibility.

    Returns:
        Dict mapping each ontology label to its ordered ancestor list.
        Root terms map to ``[]``.
    """
    hierarchy = _load_tissue_hierarchy()   # lower-case child → lower-case parent | None
    display = _load_tissue_display_names()  # lower-case → original casing

    def display_of(term_lc: str) -> str:
        return display.get(term_lc, term_lc)

    out: Dict[str, List[str]] = {}
    for term_lc in hierarchy:
        ancestors: List[str] = []
        current = hierarchy[term_lc]
        while current is not None:
            ancestors.append(display_of(current))
            current = hierarchy.get(current)
        out[display_of(term_lc)] = ancestors
    return out


# Module-level cache: {display_term: [ancestor_display, ...]}
_TISSUE_ANCESTORS: Dict[str, List[str]] = _build_tissue_ancestors()


def _short_count(n: Optional[int]) -> str:
    """Humanize a cell-count: 999, 1.4k, 144k, 2.3M."""
    if not n:
        return '—'
    if n < 1_000:
        return f'{n}'
    if n < 100_000:
        return f'{n/1_000:.1f}k'
    if n < 1_000_000:
        return f'{int(round(n/1_000))}k'
    return f'{n/1_000_000:.1f}M'


def _filesize(n: Optional[int]) -> str:
    """Humanise a byte count for download listings: 1.2 KB, 340 MB, 4.1 GB."""
    if n is None:
        return ''
    if n < 1024:
        return f'{n} B'
    size = float(n)
    for unit in ('KB', 'MB', 'GB', 'TB'):
        size /= 1024
        if size < 1024 or unit == 'TB':
            return f'{size:.1f} {unit}' if size < 100 else f'{size:.0f} {unit}'
    return f'{size:.0f} TB'


# Register Jinja2 helpers — templates use {{ value|short_count }} / {{ format_count(n) }}
templates.env.filters['short_count'] = _short_count
templates.env.filters['filesize'] = _filesize
templates.env.globals['format_count'] = _short_count


def _summarize_meta_field(
    dbs: List[Dict],
    field: str,
    abbrev_map: Optional[Dict] = None,
    max_len: Optional[int] = None,
) -> Dict[str, str]:
    """Return {'abbrev': ..., 'full': ...} summarising a metadata field across datasets.

    Single unique value → abbreviated form + full value as tooltip.
    Multiple unique values → 'mix' + all values listed as tooltip.
    All null → empty strings (caller is expected to hide the cell/pill).
    """
    raw_values = [db.get(field) for db in dbs]
    flat: List[str] = []
    for raw_value in raw_values:
        if isinstance(raw_value, list):
            flat.extend(str(v) for v in raw_value if v)
        elif raw_value:
            flat.append(str(raw_value))

    unique = sorted(set(flat))

    if not unique:
        return {'abbrev': '', 'full': ''}

    if len(unique) == 1:
        full_value = unique[0]
        if abbrev_map:
            abbrev = abbrev_map.get(full_value.lower(), full_value[:6])
        elif max_len and len(full_value) > max_len:
            abbrev = full_value[:max_len] + '…'
        else:
            abbrev = full_value
        return {'abbrev': abbrev, 'full': full_value}

    return {'abbrev': 'mix', 'full': ', '.join(unique)}


# ── data builders ──────────────────────────────────────────────────────────────

def _all_databases(api_url: str) -> List[Dict]:
    """Fetch all databases from the API."""
    return _api_get(api_url, '/databases').get('databases', [])


def _modified_ts(db: Dict) -> str:
    """Best-available modification timestamp for a single dataset.

    Order of preference:
      1. mus `updated` (auto-stamped ISO 8601 on every metadata change)
      2. mus `created`
      3. .md `date` (manually set ISO date)
      4. .md `year` cast to ISO ('2026' → '2026-01-01')
      5. empty string

    Returned as an ISO-ish string so lexical sort works as chronological.
    """
    for key in ('updated', 'created', 'date'):
        v = db.get(key)
        if v:
            return str(v)
    y = db.get('year')
    if y:
        return f'{y}-01-01'
    return ''


# Level severity ordering — higher wins the "highest level" reduce on a group.
_CAVEAT_LEVEL_RANK: Dict[str, int] = {'low': 1, 'medium': 2, 'high': 3}


def _summarize_caveats(caveats: Optional[List[Dict]]) -> Dict[str, Any]:
    """Reduce a caveat list to (n, max_level) for group-index badges.

    Unknown/missing levels are treated as 'low' so a badly-formed entry never
    silently disappears from the count but also doesn't spuriously escalate.
    Returns ``{'n': 0, 'max_level': None}`` when there are no caveats.
    """
    if not caveats:
        return {'n': 0, 'max_level': None}
    max_rank = 0
    max_level: Optional[str] = None
    for c in caveats:
        lvl = (c.get('level') if isinstance(c, dict) else None) or 'low'
        rank = _CAVEAT_LEVEL_RANK.get(lvl, 1)
        if rank > max_rank:
            max_rank = rank
            max_level = lvl
    return {'n': len(caveats), 'max_level': max_level or 'low'}


# ── landing-page list helpers ─────────────────────────────────────────────────
# The catalogue list shows plain words and only the values that set a
# collection apart from the rest of the catalogue; these maps turn ontology
# terms and technology classes into readable labels.
_CELLTYPE_PLAIN = {
    'microglial cell': 'microglia', 'microglia': 'microglia',
    'astrocyte': 'astrocytes', 'oligodendrocyte': 'oligodendrocytes',
    'oligodendrocyte precursor cell': 'OPCs', 'neuron': 'neurons',
    'excitatory neuron': 'excitatory neurons', 'inhibitory neuron': 'inhibitory neurons',
    'myeloid cell': 'myeloid cells', 'macrophage': 'macrophages',
    'endothelial cell': 'endothelial cells', 'pericyte': 'pericytes', 'pericyte cell': 'pericytes',
    'vascular associated smooth muscle cell': 'smooth muscle cells',
}
_TISSUE_PLAIN = {
    'brodmann (1909) area 17': 'visual cortex (BA17)',
}
# Disease values that describe controls rather than a condition: never tags.
_CONTROL_VALUES = {'normal', 'control', 'healthy', 'none', 'not applicable'}
_TAG_GROUP_WORD = {'organism': 'species', 'method': 'methods', 'tissue': 'regions',
                   'celltype': 'cell types', 'disease': 'conditions'}
_TECH_PLAIN = {
    'sc/sn RNA': 'single-cell RNA', 'bulk RNA': 'bulk RNA', 'spatial': 'spatial',
    'proteomics': 'proteomics', 'other': 'other method',
}
_FACET_SKIP = {'', 'na', 'n/a', 'none', 'null', 'unknown', 'mixed', 'mix', 'other'}
LANDING_FACETS = ('organism', 'celltype', 'tissue', 'method', 'disease')


def _fold_text(text: str) -> str:
    """Lower-case, accent-free, whitespace-collapsed text for client search."""
    decomposed = unicodedata.normalize('NFKD', str(text or ''))
    stripped = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    return ' '.join(stripped.lower().split())


def _plain_facet_value(field: str, value: str) -> str:
    text = str(value).strip()
    if field == 'celltype':
        return _CELLTYPE_PLAIN.get(text.lower(), text)
    if field == 'method':
        return _TECH_PLAIN.get(text, text)
    if field == 'tissue':
        return _TISSUE_PLAIN.get(text.lower(), text)
    return text


def _collection_facets(dbs: List[Dict]) -> Dict[str, List[str]]:
    """Plain-word facet values per field, unioned over a collection's datasets."""
    out: Dict[str, List[str]] = {}
    for field in LANDING_FACETS:
        values: List[str] = []
        for db in dbs:
            if field == 'method':
                raw = [_tech_class(t) for t in _as_list(db.get('technology'))]
            else:
                source_field = 'celltype' if field == 'celltype' else field
                raw = _as_list(db.get(source_field)) or (
                    _as_list(db.get('cell_type')) if field == 'celltype' else [])
            for item in raw:
                for part in str(item).split(','):
                    if part.strip().lower() in _FACET_SKIP:
                        continue
                    plain = _plain_facet_value(field, part)
                    if plain and plain not in values:
                        values.append(plain)
        out[field] = sorted(values, key=str.lower)
    return out


_ADDED_PLACEHOLDER = re.compile(r'^\d{4}-01-01$')


def _collection_added(collection_meta: Dict, last_modified: str) -> str:
    """Date a collection was added: index.md date unless it is a YYYY-01-01
    placeholder, then the newest dataset modification date."""
    curated = str(collection_meta.get('date') or '').strip()[:10]
    if curated and not _ADDED_PLACEHOLDER.match(curated):
        return curated
    return str(last_modified or '')[:10]


def _collection_search_text(name: str, group_id: str, paper: Dict, collection_meta: Dict,
                            facets: Dict[str, List[str]], dbs: List[Dict]) -> str:
    """Everything a person might type to find a collection, folded for search."""
    parts: List[str] = [name, group_id, collection_meta.get('group') or '',
                        collection_meta.get('title') or '', collection_meta.get('summary') or '',
                        (collection_meta.get('description') or '')[:1500],
                        str(collection_meta.get('year') or ''), str(collection_meta.get('doi') or ''),
                        str(collection_meta.get('pmid') or ''), str(collection_meta.get('accession') or '')]
    parts += [paper.get(k) or '' for k in ('paper_title', 'first_author', 'last_author', 'journal')]
    for values in facets.values():
        parts += values
    for db in dbs:
        for key in ('id', 'file_id', 'basename', 'title', 'description', 'organism', 'tissue',
                    'celltype', 'cell_type', 'disease', 'condition', 'region', 'technology',
                    'suspension_type', 'experimental_system', 'accession', 'doi', 'pmid',
                    'contributor', 'access', 'source', 'year'):
            parts += [str(v) for v in _as_list(db.get(key))]
        parts.append((db.get('markdown') or '')[:600])
    return _fold_text(' '.join(p for p in parts if p))


def _landing_tags(facets: Dict[str, List[str]], defaults: Dict[str, set]) -> List[Dict[str, str]]:
    """Facet values that differ from the catalogue default, organism and method
    first. More than two unusual values in one field collapse into a single
    count tag ("11 regions") whose title lists them."""
    tags: List[Dict[str, str]] = []
    for field in ('organism', 'method', 'tissue', 'celltype', 'disease'):
        unusual = [v for v in facets.get(field) or []
                   if v not in defaults.get(field, set())
                   and not (field == 'disease' and v.lower() in _CONTROL_VALUES)]
        if len(unusual) > 2:
            tags.append({'field': field, 'value': f"{len(unusual)} {_TAG_GROUP_WORD[field]}",
                         'title': ', '.join(unusual)})
        else:
            tags += [{'field': field, 'value': v, 'title': v} for v in unusual]
    return tags


def _collection_access_label(access_values: List[str]) -> str:
    """One access word per collection: 'public', 'internal' (or another
    single non-public value), or 'partly public' when datasets disagree."""
    values = sorted(set(access_values or ['internal']))
    if len(values) == 1:
        return values[0]
    return 'partly public' if 'public' in values else ', '.join(values)


def landing_access_marks(collections: List[Dict]) -> str:
    """Set coll['access_mark'] only where a collection's access differs from
    the catalogue majority, so the mark carries information. Returns the
    majority label. Majority non-public: public ones get a muted 'public'
    mark. Majority public: non-public ones get the lock."""
    counts: Dict[str, int] = defaultdict(int)
    for coll in collections:
        coll['access_label'] = _collection_access_label(coll.get('access_values') or [])
        counts[coll['access_label']] += 1
    majority = max(counts.items(), key=lambda item: (item[1], item[0]))[0] if counts else 'internal'
    for coll in collections:
        label = coll['access_label']
        if label == majority:
            coll['access_mark'] = None
        elif label == 'public' or (majority != 'public' and label == 'partly public'):
            coll['access_mark'] = {'kind': 'public', 'text': label,
                                   'title': f"{label}: shareable outside the lab"
                                            if label == 'public' else
                                            "some datasets are public, others internal"}
        else:
            coll['access_mark'] = {'kind': 'lock', 'text': label,
                                   'title': f"Not public ({label}): for signed-in lab members; "
                                            f"do not share outside the group"}
    return majority


def landing_facet_defaults(collections: List[Dict], share: float = 0.30) -> Dict[str, set]:
    """Per facet, the single most common value when at least `share` of all
    collections carry it: the catalogue's unstated default, not shown as a tag."""
    total = len(collections) or 1
    defaults: Dict[str, set] = {}
    for field in LANDING_FACETS:
        counts: Dict[str, int] = defaultdict(int)
        for coll in collections:
            for value in (coll.get('facets') or {}).get(field) or []:
                counts[value] += 1
        top = max(counts.items(), key=lambda item: (item[1], item[0]), default=None)
        defaults[field] = {top[0]} if top and top[1] / total >= share else set()
    return defaults


def _authors_label(first: Optional[str], last: Optional[str]) -> str:
    """'First … Last' for the landing list, or whichever name is known."""
    first, last = (first or '').strip(), (last or '').strip()
    if first and last and first != last:
        return f"{first} … {last}"
    return first or last


def _cells_label(count: Optional[int]) -> str:
    """Readable cell count: '88 cells', '3.9 k cells', '833 k cells', '1.4 M cells'."""
    if not count:
        return ''
    if count < 1000:
        return f"{count} cells"
    if count < 10_000:
        return f"{count / 1000:.1f} k cells"
    if count < 1_000_000:
        return f"{round(count / 1000)} k cells"
    return f"{count / 1_000_000:.1f} M cells"


def get_collection_list(api_url: str) -> List[Dict]:
    """Build collection list by grouping API databases by group_id."""
    databases = _all_databases(api_url)

    groups: Dict[str, List[Dict]] = defaultdict(list)
    for db in databases:
        groups[db.get('group_id') or db['id'].split('/')[0]].append(db)

    collections = []
    for group_id, dbs in groups.items():
        total_cells = sum(db.get('n_cells') or 0 for db in dbs)
        collection_meta = (dbs[0].get('collection') or {})
        # Display name falls through: index.md `title` (curator override) →
        # `group` (folder name) → `group_id`. Blank/whitespace title is
        # ignored so a cleared field reverts to the folder name.
        name = (
            (collection_meta.get('title') or '').strip()
            or collection_meta.get('group')
            or group_id
        )
        # Group modification = most recent member modification.
        last_modified = max((_modified_ts(db) for db in dbs), default='')

        # publication_status is a collection-level attribute (index.md
        # frontmatter). Ant-serve doesn't expose it yet; read from disk.
        pub_status = _publication_status_for_group(collection_meta.get('group'))
        # paper_title / first_author / last_author likewise come straight from
        # the folder's index.md frontmatter — the /databases endpoint doesn't
        # expose them. Landing renders them on the collection row's line 1.
        paper_info = _paper_info_for_group(collection_meta.get('group'))
        facets = _collection_facets(dbs)

        # Unified source+publication icon (one glyph per collection). The
        # shape is semantically about *provenance* — beaker = lab-generated
        # (source=internal), globe = imported from an external source. That
        # matches how users read the icon on a collection card ("is this
        # ours?"). We deliberately use `source` here, not `access`: a
        # collection where every dataset was lab-generated but a subset is
        # currently public-shareable ('access=public') is still lab-
        # generated. Per-dataset icons on the folder page keep using
        # `access` (a per-dataset shareability concern).
        #
        # Aggregation: if any dataset was imported (source=external) we show
        # the globe — a "mixed" collection with even one imported dataset is
        # not wholly lab-generated. Missing source defaults to 'internal'
        # (matches the safer-default contract elsewhere).
        source_values = sorted({(db.get('source') or 'internal').lower() for db in dbs})
        # Map source vocabulary → the access glyph vocabulary that
        # _access_pub_icon speaks (internal → beaker, public → globe).
        coll_is_external = any(v not in ('internal',) for v in source_values)
        icon_access = 'public' if coll_is_external else 'internal'
        pub_icon = _access_pub_icon(icon_access, pub_status)
        # Rewrite the icon labels to source-terminology so the tooltip
        # reads correctly (the shape encodes provenance here, not
        # shareability). Keep the publication_status half untouched.
        source_word = 'imported' if coll_is_external else 'lab-generated'
        pub_icon = {
            **pub_icon,
            'access': source_word,
            'tooltip': f"{source_word} · {pub_icon['publication']}",
        }

        # Canonical dataset date: for the collection use the newest date
        # across its datasets (published year OR most-recent mtime).
        member_dates = [
            _dataset_date(db, group_name=collection_meta.get('group'),
                          publication_status=pub_status)
            for db in dbs
        ]
        best = max(member_dates, key=lambda d: d['sort_ts']) if member_dates else {'iso': '', 'sort_ts': 0.0}
        dataset_date = {'iso': best['iso'], 'sort_ts': best['sort_ts']}

        collections.append({
            'name': name,
            'group_id': group_id,
            'browse_id': group_id,
            'dataset_count': len(dbs),
            'total_cells': total_cells or None,
            # all API-registered databases have a duckdb (local or S3)
            'all_converted': True,
            'some_converted': True,
            'duckdb_count': len(dbs),
            'update_pending': 0,
            'last_modified': last_modified,
            'publication_status': pub_status,
            'pub_icon': pub_icon,
            'dataset_date': dataset_date,
            'paper_title': paper_info.get('paper_title') or '',
            'first_author': paper_info.get('first_author') or '',
            'last_author': paper_info.get('last_author') or '',
            # Sort key for a per-collection tissue-hierarchy sort — highest =
            # sinks to the bottom. Multi-region collections use the first
            # dataset's first tissue.
            'tissue_rank': _tissue_sort_key([db.get('tissue') for db in dbs if db.get('tissue')]),
            'description': None,
            # one-line collection summary (index.md frontmatter `description`,
            # exposed by the API as collection.summary — distinct from
            # collection.description which is the index.md body). Shown on the
            # landing page under the collection name.
            'summary': collection_meta.get('summary'),
            # Group-level summaries: single shared value → that value;
            # multiple values across datasets → "mix". Used by index.html.
            'organism':        _summarize_meta_field(dbs, 'organism',        abbrev_map=_ORGANISM_ABBREV),
            'technology':      _summarize_meta_field(dbs, 'technology',      abbrev_map=_TECHNOLOGY_ABBREV),
            'technology_category': _summarize_meta_field(dbs, 'technology_category', max_len=12),
            'celltype':        _summarize_meta_field(dbs, 'celltype',        abbrev_map=_CELLTYPE_ABBREV),
            'suspension_type': _summarize_meta_field(dbs, 'suspension_type', abbrev_map=_SUSPENSION_ABBREV),
            'tissue':          _summarize_meta_field(dbs, 'tissue',          abbrev_map=_TISSUE_ABBREV),
            'disease':         _summarize_meta_field(dbs, 'disease',         abbrev_map=_DISEASE_ABBREV),
            'source':          _summarize_meta_field(dbs, 'source',          abbrev_map=_SOURCE_ABBREV),
            'access':          _summarize_meta_field(dbs, 'access',          abbrev_map=_ACCESS_ABBREV),
            # Per-organism chips — one letter chip per organism (Hs, Mm, …),
            # so a multi-species collection ("human | mouse") gets two chips
            # side by side instead of a "mix" placeholder.
            'organism_chips': _organism_chips(sorted({
                item.strip().lower()
                for db in dbs
                for item in _as_list(db.get('organism'))
                if item and str(item).strip()
            } - {''})),
            # Top-level tech classification (sc/sn RNA, bulk RNA, spatial, …).
            # Multi-tech collections resolve to 'mixed'.
            'tech_class': _tech_class(sorted({
                str(item).strip().lower()
                for db in dbs
                for item in _as_list(db.get('technology'))
                if item and str(item).strip()
            } - {''})),
            # any member dataset with is_secondary=true → mark the whole
            # collection as containing secondary data. All-primary stays False.
            'has_secondary': any(bool(db.get('is_secondary')) for db in dbs),
            # Landing list: plain-word facets, paper line extras, sort keys
            # and the folded search text the client-side filter reads.
            'facets': facets,
            'journal': paper_info.get('journal') or '',
            'paper_year': str(collection_meta.get('year') or ''),
            'added': _collection_added(collection_meta, last_modified),
            'non_public': any(str(db.get('access') or 'internal').lower() != 'public' for db in dbs),
            'access_values': sorted({str(db.get('access') or 'internal').lower() for db in dbs}),
            'search_text': _collection_search_text(name, group_id, paper_info, collection_meta, facets, dbs),
        })

    # Default sort: newest-first by last_modified (empty values sink to the
    # bottom). Lexical compare works because all timestamps are ISO-ish.
    collections.sort(key=lambda c: (c['last_modified'] or '', c['name']), reverse=True)
    return collections


def get_filter_options(collections: List[Dict]) -> Dict[str, List[str]]:
    """Compute the unique 'full' values across all collections for each filter
    field. Used to populate the frontpage dropdowns.

    A folder with multiple values in a field contributes each of them
    (e.g. organism='human, mouse' → ['human', 'mouse']).
    """
    fields = ['organism', 'technology_category', 'technology', 'celltype',
              'suspension_type', 'tissue', 'disease', 'source', 'access']
    out: Dict[str, set] = {f: set() for f in fields}
    for c in collections:
        for f in fields:
            full = (c.get(f) or {}).get('full') or ''
            if not full:
                continue
            for value in full.split(','):
                value = value.strip()
                if value:
                    out[f].add(value)
    return {f: sorted(out[f]) for f in fields}


def get_collection_datasets(api_url: str, group_id: str) -> tuple:
    """Return (datasets, description_html, collection_meta) for a collection.

    ``collection_meta`` splits the per-dataset metadata fields into those that
    are *constant* across every dataset in the collection (hoisted to inert
    header pills, and dropped from the table) vs those that *vary* (kept as
    table columns). So the dataset table only shows the discriminating fields —
    e.g. an all-microglia collection shows one 'microglial cell' pill in the
    header instead of repeating it down a Cell column.
    """
    databases = _all_databases(api_url)

    datasets = []
    description_html = None
    group_dbs = []

    # publication_status is collection-level (index.md frontmatter).
    # Read it once — we need it for the icon + canonical date on every row.
    pub_status: Optional[str] = None
    group_folder: Optional[str] = None
    for db in databases:
        if (db.get('group_id') or db['id'].split('/')[0]) == group_id:
            group_folder = (db.get('collection') or {}).get('group')
            break
    pub_status = _publication_status_for_group(group_folder)

    for db in databases:
        if (db.get('group_id') or db['id'].split('/')[0]) != group_id:
            continue
        group_dbs.append(db)

        if description_html is None:
            # Collection blurb = the markdown body of the collection's top-level
            # index.md (REST API 2.5 `collection.description`). NOT `experiment`,
            # which is a per-dataset field that read confusingly as the blurb.
            body = ((db.get('collection') or {}).get('description') or '').strip()
            # The index.md body conventionally opens with `# <Group>` — drop that
            # leading H1 so it doesn't duplicate the page title rendered above.
            lines = body.split('\n')
            if lines and lines[0].lstrip().startswith('# '):
                body = '\n'.join(lines[1:]).strip()
            if body:
                description_html = markdown.markdown(
                    body, extensions=['tables', 'fenced_code']
                )
                # collection markdown has no implicit dataset — plot: tokens
                # must carry &db=<group>/<file>.
                description_html = _expand_plot_thumbnails(description_html, default_db=None)

        layers = list(db.get('layers', {}).keys())
        embeddings = list(db.get('embeddings', {}).keys())
        file_id = db.get('file_id', '')

        # Description (one-line summary) — moved from a grey sub-line under
        # the name to a title= tooltip on the link itself.
        description_summary = (db.get('description') or db.get('summary') or '').strip()

        datasets.append({
            'name': db.get('title') or file_id,
            'file_id': file_id,
            'browse_id': file_id,
            'subfolder': None,
            'n_obs': db.get('n_cells'),
            'n_vars': db.get('n_genes'),
            'modified': _modified_ts(db),
            'layers': layers,
            'embeddings': embeddings,
            'has_metadata': True,
            'has_duckdb': True,
            'update_pending': False,
            'description': description_summary,
            'pub_icon': _access_pub_icon(db.get('access'), pub_status),
            'dataset_date': _dataset_date(db, group_name=group_folder,
                                          publication_status=pub_status),
            'tissue_rank': _tissue_sort_key(_as_list(db.get('tissue'))),
            'organism':        _summarize_meta_field([db], 'organism',        abbrev_map=_ORGANISM_ABBREV),
            'technology':      _summarize_meta_field([db], 'technology',      abbrev_map=_TECHNOLOGY_ABBREV),
            'celltype':        _summarize_meta_field([db], 'celltype',        abbrev_map=_CELLTYPE_ABBREV),
            'suspension_type': _summarize_meta_field([db], 'suspension_type', abbrev_map=_SUSPENSION_ABBREV),
            'tissue':          _summarize_meta_field([db], 'tissue',          abbrev_map=_TISSUE_ABBREV),
            'disease':         _summarize_meta_field([db], 'disease',         abbrev_map=_DISEASE_ABBREV),
            'access':          _summarize_meta_field([db], 'access',          abbrev_map=_ACCESS_ABBREV),
            'source':          _summarize_meta_field([db], 'source',          abbrev_map=_SOURCE_ABBREV),
            'organism_chips':  _organism_chips(db.get('organism')),
            'tech_class':      _tech_class(db.get('technology')),
            # External resources — surfaced as small link icons after the name.
            'doi':             _as_list(db.get('doi')),
            'link':            _as_list(db.get('link')),
            'accession':       db.get('accession'),
            'accession_url':   _accession_url(str(db.get('accession'))) if db.get('accession') else None,
            # Caveats summary → ⚠ badge next to the dataset name; folder.html
            # only needs (n, max_level), not the full body (that lives on the
            # dataset page).
            'caveats_summary': _summarize_caveats(db.get('caveats')),
            # is_secondary: true when the dataset contains cells that also
            # appear in other deposits (re-aggregated collection). Sourced from
            # the per-dataset .md frontmatter via the REST API. Defensive .get()
            # so a rescan-lagging API still renders (the badge just stays off).
            'is_secondary': bool(db.get('is_secondary')),
            'version':         (str(db.get('version')).strip() or None) if db.get('version') else None,
        })

    # Constant-across-collection fields → header pills; varying fields → columns.
    # (field, abbrev_map, css class, human label)
    _coll_fields = [
        ('organism',        _ORGANISM_ABBREV,    '',        'organism'),
        ('technology',      _TECHNOLOGY_ABBREV,  'tech',    'technology'),
        ('celltype',        _CELLTYPE_ABBREV,    'cell',    'cell type'),
        ('tissue',          _TISSUE_ABBREV,      'tissue',  'tissue'),
        ('disease',         _DISEASE_ABBREV,     'disease', 'disease'),
        ('suspension_type', _SUSPENSION_ABBREV,  'prep',    'prep'),
        ('access',          _ACCESS_ABBREV,      'icon',    'access'),
        ('source',          _SOURCE_ABBREV,      'icon',    'source'),
    ]
    # `tissue` has its own dedicated column and `suspension_type` (prep) info
    # is folded into the Tech tooltip — neither should appear as a header
    # context pill, and the tissue column must render unconditionally.
    _NO_HEADER_PILL = {'tissue', 'suspension_type'}
    _ALWAYS_COLUMN = {'tissue'}
    header_pills: List[Dict] = []
    var_fields: List[str] = []
    for field, abbrev_map, css, label in _coll_fields:
        summary = _summarize_meta_field(group_dbs, field, abbrev_map=abbrev_map)
        abbrev, full = summary['abbrev'], summary['full']
        if field in _ALWAYS_COLUMN:
            # Column shows regardless of variability (Tissue column stays even
            # when constant across the collection). Skip for empty groups —
            # no datasets means no table, and the empty-group contract
            # returns an empty var_fields.
            if group_dbs:
                var_fields.append(field)
            continue
        if field in _NO_HEADER_PILL:
            # No header pill; fall back to the mix→column behaviour so the
            # column still appears when the field actually varies.
            if abbrev == 'mix':
                var_fields.append(field)
            continue
        if abbrev and abbrev != 'mix':
            # single shared value across the whole collection → header pill
            is_icon = css == 'icon'
            header_pills.append({
                'cls': css,
                'text': f'{abbrev} {full}' if is_icon else full,
                'title': f'{label}: {full}',
            })
        elif abbrev == 'mix':
            var_fields.append(field)   # differs between datasets → keep column

    # Newest-first by canonical dataset_date (published year OR upload mtime);
    # ties broken by name ASC.
    datasets.sort(key=lambda d: (-(d.get('dataset_date') or {}).get('sort_ts', 0.0), d['name']))

    collection_meta = {
        'pills': header_pills,
        'var_fields': var_fields,
        'publication_status': pub_status,
    }
    return datasets, description_html, collection_meta


# ── routes ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Catalog landing page.

    UX shortcut: when the (post-filter) catalog contains exactly one
    collection, skip the overview and redirect straight into that
    collection's folder page.  The user can opt out — or simply land on
    the overview while searching — by appending **any** query string
    (e.g. ``/?all=1``, ``/?search=foo``); a non-empty query string
    suppresses the auto-redirect.  Zero collections → the empty-catalog
    page renders as before; two or more → the normal overview.
    """
    try:
        collections = get_collection_list(_api_url(request))
    except Exception as error:
        collections = []
        print(f"API error: {error}")

    # Auto-jump into the only collection.  Use the same list the overview
    # would render (already filtered for visibility / inactives upstream)
    # so a hidden second group can't trigger an unexpected redirect.  Any
    # query string (?all=1, ?search=…, …) opts out.
    if len(collections) == 1 and not request.url.query:
        target = f"/browse/{url_quote(collections[0]['browse_id'])}"
        return RedirectResponse(url=target, status_code=302)

    total_datasets = sum(c['dataset_count'] for c in collections)
    total_cells = sum(c['total_cells'] for c in collections if c['total_cells']) or None

    filter_options = get_filter_options(collections)
    tissue_tree = build_tissue_tree(collections)
    facet_defaults = landing_facet_defaults(collections)
    landing_access_marks(collections)
    for coll in collections:
        coll['tags'] = _landing_tags(coll.get('facets') or {}, facet_defaults)
        coll['display_title'] = coll.get('paper_title') or coll.get('name') or coll.get('group_id')
        coll['authors_label'] = _authors_label(coll.get('first_author'), coll.get('last_author'))
        coll['cells_label'] = _cells_label(coll.get('total_cells'))
        coll['landing_index'] = {
            'gid': coll.get('group_id'),
            'text': coll.get('search_text') or '',
            'facets': coll.get('facets') or {},
            'added': coll.get('added') or '',
            'year': coll.get('paper_year') or '',
            'cells': coll.get('total_cells') or 0,
            'name': coll.get('display_title') or '',
        }

    return templates.TemplateResponse(request, "index.html", {
        "folders": collections,
        "total_datasets": total_datasets,
        "total_folders": len(collections),
        "total_cells": total_cells,
        "filter_options": filter_options,
        "tissue_tree": tissue_tree,
        "facet_defaults": {k: sorted(v) for k, v in facet_defaults.items()},
        "landing_facets": LANDING_FACETS,
    })


def _is_admin(request: Request) -> bool:
    """True when Authelia forwarded a Remote-Groups header containing 'admins'.
    The proxy (Caddy/nginx) sets it from the SSO session; un-proxied access has
    no header → not admin. This only gates SHOWING/serving the admin UI — the
    authoritative write gate is server-side in ant-serve."""
    groups = request.headers.get('remote-groups') or ''
    return 'admins' in [g.strip() for g in groups.split(',') if g.strip()]


# Files hidden from any downloads listing across the site — internal artifacts
# users shouldn't reach for. Kept in one place so filter logic can't drift.
_DOWNLOAD_HIDE_SUFFIXES = ('.duckdb', '.h5ad.mus', '.md', '.inactive')
_DOWNLOAD_HIDE_INFIX = '.layer.'          # x.layer.<name>.parquet sidecars


def _is_download_hidden(name: str) -> bool:
    """True if ``name`` should be hidden from any UI downloads list."""
    if not name or name.startswith(('_', '.')):
        return True
    lower = name.lower()
    if any(lower.endswith(suffix) for suffix in _DOWNLOAD_HIDE_SUFFIXES):
        return True
    if _DOWNLOAD_HIDE_INFIX in lower and lower.endswith('.parquet'):
        return True
    return False


def _resolve_static_dir(group_key: str) -> Optional[Path]:
    """Return the on-disk static/ folder for a collection.

    `group_key` can be either the short browse-id (e.g. `S25`) or the
    friendly folder name (`Sierksma2025`). Both map to
    `data/h5adstore/<friendly>/static/` — we try the key as-is first, then
    look it up in the API catalog by matching group_id.
    """
    candidate = _H5ADSTORE_ROOT / group_key / 'static'
    try:
        if candidate.is_dir():
            return candidate
    except OSError:
        pass
    # Resolve short → friendly via the catalog. Best-effort.
    try:
        for db in _all_databases_cached():
            if (db.get('group_id') or db['id'].split('/')[0]) == group_key:
                friendly = (db.get('collection') or {}).get('group')
                if friendly:
                    fpath = _H5ADSTORE_ROOT / friendly / 'static'
                    try:
                        if fpath.is_dir():
                            return fpath
                    except OSError:
                        pass
                break
    except Exception:
        pass
    return None


def _all_databases_cached():
    """Small local wrapper — _all_databases uses the request api_url; the
    static-file lookup doesn't have request context, so hit the default."""
    from config import get_config
    cfg = get_config()
    api_url = f"http://{cfg.get('rest_api_host','localhost')}:{cfg.get('rest_api_port',8080)}"
    return _all_databases(api_url)


def _group_static_files(group_id: str) -> List[Dict]:
    """List files under data/h5adstore/{group}/static/ eligible for download.

    Returns a list of ``{name, size_bytes}`` sorted by name. Empty when the
    folder does not exist or contains only hidden entries. Accepts either
    short browse-id (S25) or friendly folder name (Sierksma2025).
    """
    static_dir = _resolve_static_dir(group_id)
    if static_dir is None:
        return []
    out: List[Dict] = []
    try:
        for entry in sorted(static_dir.iterdir()):
            if not entry.is_file():
                continue
            if _is_download_hidden(entry.name):
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = None
            out.append({'name': entry.name, 'size_bytes': size})
    except OSError:
        return []
    return out


@router.get("/browse/{browse_id}", response_class=HTMLResponse)
async def folder_view(request: Request, browse_id: str):
    group_id = unquote(browse_id)

    try:
        datasets, description_html, collection_meta = get_collection_datasets(_api_url(request), group_id)
    except Exception as error:
        print(f"API error: {error}")
        datasets, description_html, collection_meta = [], None, {'pills': [], 'var_fields': []}

    if not datasets:
        return templates.TemplateResponse(
            request, "index.html",
            {"folders": [], "error": f"Collection '{group_id}' not found"},
            status_code=404
        )

    # Friendly group name. Falls through index.md `title` (curator override)
    # → `group` (folder name) → the short group_id when neither is set.
    friendly_group = None
    try:
        all_dbs = _all_databases(_api_url(request))
        for db in all_dbs:
            if (db.get('group_id') or db['id'].split('/')[0]) == group_id:
                coll = db.get('collection') or {}
                friendly_group = (
                    (coll.get('title') or '').strip()
                    or coll.get('group')
                )
                if friendly_group:
                    break
    except Exception:
        pass

    # Group-level static downloads (data/h5adstore/{group}/static/). Empty list
    # → the Downloads section is hidden on the folder page entirely.
    static_files = _group_static_files(group_id)

    return templates.TemplateResponse(request, "folder.html", {
        "folder_name": friendly_group or group_id,
        "folder_browse_id": group_id,
        "group_id": group_id,
        "description_html": description_html,
        "datasets": datasets,
        "collection_pills": collection_meta['pills'],
        "var_fields": collection_meta['var_fields'],
        "is_admin": _is_admin(request),
        "static_files": static_files,
        "papers": _collection_papers(_api_url(request), group_id,
                                     [d['file_id'] for d in datasets if d.get('file_id')]),
    })


@router.get("/browse/{browse_id}/static/{filename}")
async def group_static_download(request: Request, browse_id: str, filename: str):
    """Serve a file from ``data/h5adstore/{group}/static/`` as a download.

    Safety: rejects any name that would escape the static dir (path segments,
    absolute paths) or that matches the sitewide hidden-download filter. The
    file must actually live under the resolved static directory."""
    from fastapi import HTTPException
    group_id = unquote(browse_id)
    name = unquote(filename)
    if '/' in name or '\\' in name or name in ('.', '..') or _is_download_hidden(name):
        raise HTTPException(status_code=404, detail="not found")
    resolved = _resolve_static_dir(group_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="not found")
    static_dir = resolved.resolve()
    try:
        target = (static_dir / name).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="not found")
    # containment check — the resolved path must sit inside static_dir
    try:
        target.relative_to(static_dir)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target, filename=name)


@router.get("/admin/edit/{browse_id}", response_class=HTMLResponse)
async def edit_collection_view(request: Request, browse_id: str):
    """Admin-only editor for a collection's index.md (frontmatter + markdown
    body). Loads/saves via ant-serve's /collections/{group}/source endpoint,
    which writes to disk. The /admin/* path is also gated at the proxy."""
    group_id = unquote(browse_id)
    if not _is_admin(request):
        return templates.TemplateResponse(
            request, "index.html",
            {"folders": [], "error": "Not authorized — admin only."},
            status_code=403,
        )
    # Datasets in this collection → options for the default_dataset dropdown
    # (value = file_id, the collection-relative id, e.g. "AQB").
    dataset_opts = []
    try:
        for db in _all_databases(_api_url(request)):
            gid = db.get('group_id') or db.get('id', '').split('/')[0]
            if gid != group_id:
                continue
            file_id = db.get('file_id') or db.get('id', '').split('/')[-1]
            title = db.get('title') or db.get('name')
            dataset_opts.append({'value': file_id, 'label': file_id + (f' — {title}' if title else '')})
    except Exception:
        pass
    dataset_opts.sort(key=lambda d: d['value'])
    return templates.TemplateResponse(request, "edit_collection.html", {
        "group_id": group_id,
        "datasets": dataset_opts,
    })


# ── plot UI routes ─────────────────────────────────────────────────────────────
#
# Registered BEFORE dataset_view because dataset_view uses {dataset_browse_id:path}
# which would otherwise greedy-match "TSO/plot/scatter" as the dataset id.

_PLOT_GEOMS_UI = {
    'scatter':   {'label': 'Scatter (embedding or XY)', 'icon': '🗺',
                  'needs': {'embedding_or_xy': True, 'color': 'any'},
                  'group': 'spatial'},
    'hexbin':    {'label': 'Hexbin (embedding or XY)', 'icon': '⬣',
                  'needs': {'embedding_or_xy': True, 'color': 'any'},
                  'group': 'spatial'},
    'violin':    {'label': 'Violin',           'icon': '🎻',
                  'needs': {'x': 'categorical', 'y': 'gene_or_num'},
                  'group': 'grouped'},
    'box':       {'label': 'Boxplot',          'icon': '📦',
                  'needs': {'x': 'categorical', 'y': 'gene_or_num'},
                  'group': 'grouped'},
    'bar':       {'label': 'Bar (mean ± CI)',  'icon': '📊',
                  'needs': {'x': 'categorical', 'y': 'gene_or_num_or_count'},
                  'group': 'grouped'},
    'histogram': {'label': 'Histogram',        'icon': '📈',
                  'needs': {'x': 'gene_or_num', 'color': 'categorical'},
                  'group': 'distribution'},
    'ecdf':      {'label': 'ECDF',             'icon': '📉',
                  'needs': {'x': 'gene_or_num', 'color': 'categorical'},
                  'group': 'distribution'},
    'kde':       {'label': 'KDE density',      'icon': '🌊',
                  'needs': {'x': 'gene_or_num', 'color': 'categorical'},
                  'group': 'distribution'},
    'kde2d':     {'label': 'KDE density (2D)', 'icon': '🗻',
                  'needs': {'x': 'gene_or_num', 'y': 'gene_or_num'},
                  'group': 'distribution'},
    'heatmap':   {'label': 'Heatmap',          'icon': '🔥',
                  'needs': {'y': 'categorical', 'x_genes': True},
                  'group': 'matrix'},
    'rolling':   {'label': 'Rolling-mean trajectory', 'icon': '〰',
                  'needs': {'x': 'gene_or_num', 'y': 'gene_or_num',
                            'color': 'categorical'},
                  'group': 'trajectory'},
    # ── DE-driven geoms (REST API 2.3) — need differential-expression data
    #    ingested for the dataset.  volcano/ma take a (study, term, contrast)
    #    triple; forest takes a gene and plots it across contrasts.
    'volcano':   {'label': 'Volcano (DE)', 'icon': '🌋',
                  'needs': {'de': 'study_term_contrast'},
                  'group': 'de'},
    'ma':        {'label': 'MA (DE)', 'icon': '📐',
                  'needs': {'de': 'study_term_contrast'},
                  'group': 'de'},
    'forest':    {'label': 'Forest (gene × contrasts)', 'icon': '🌲',
                  'needs': {'de': 'gene'},
                  'group': 'de'},
    'de_heatmap': {'label': 'DE heatmap (contrasts × genes)', 'icon': '▦',
                   'needs': {'de': 'study_term'},
                   'group': 'de'},
    'de_quadrant': {'label': 'DE quadrant (A vs B)', 'icon': '🎯',
                    'needs': {'de': 'two_contrasts'},
                    'group': 'de'},
}

# Display order for the viz row on the dataset page (group + label).
_PLOT_GEOM_GROUPS = (
    ('spatial',      'spatial'),       # scatter, hexbin
    ('grouped',      'per category'),  # violin, box, bar
    ('distribution', 'distribution'),  # histogram, ecdf, kde
    ('matrix',       'matrix'),        # heatmap
)


@router.get("/browse/{folder_browse_id}/{dataset_browse_id}/plot/{geom}",
            response_class=HTMLResponse)
async def plot_view(request: Request, folder_browse_id: str,
                    dataset_browse_id: str, geom: str):
    """Render the interactive plot form for one geom on one dataset."""
    if geom not in _PLOT_GEOMS_UI:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404,
            detail=f"Unknown plot geom '{geom}'. Valid: {sorted(_PLOT_GEOMS_UI)}",
        )

    group_id = unquote(folder_browse_id)
    file_id = unquote(dataset_browse_id)
    db_id = f"{group_id}/{file_id}"
    api_url = _api_url(request)

    try:
        db = _api_get(api_url, f'/databases/{db_id}/info')
    except Exception as error:
        print(f"API error: {error}")
        return templates.TemplateResponse(
            request, "index.html",
            {"folders": [], "error": f"Dataset '{db_id}' not found"},
            status_code=404,
        )

    collection = db.get('collection') or {}
    # Collection display name. index.md `title` (curator override) beats the
    # folder name; both fall through to the short group_id.
    friendly_group = (
        (collection.get('title') or '').strip()
        or collection.get('group')
        or group_id
    )
    dataset_title = db.get('title') or file_id

    # Fetch the dataset's DE study list (best-effort). Used two ways:
    #   • seeds the study→term→contrast pickers for DE geoms, and
    #   • gates the geom-switcher so volcano/ma/forest are only offered when
    #     the dataset actually has DE data (has_de).
    de_studies: List[Dict] = []
    try:
        de_studies = _api_get(api_url, f'/databases/{db_id}/de').get('studies', [])
    except Exception as error:
        print(f"DE study fetch error: {error}")
    has_de = bool(de_studies)

    obs = db.get('obs') or {}
    # top_* defaults come from the .md and were curated by `ant-suggest-tops`.
    # Fall back to empty lists so the template never crashes on null.
    top_genes = db.get('top_genes') or []
    top_cat   = db.get('top_cat')   or []
    top_num   = db.get('top_num')   or []

    # Caveats banner (task #254) — surface the SAME warning list that
    # dataset.html shows, above the plot preview. A caveat like SP4/MPF's
    # astro-doublets applies directly to the plot the user is about to look
    # at; hiding it on the dataset overview page is dangerous.
    caveats = db.get('caveats') or []
    caveats_summary = _summarize_caveats(caveats)
    plot_meta = {
        'embeddings':  sorted(list((db.get('embeddings') or {}).keys())),
        'layers':      sorted(list((db.get('layers') or {}).keys())),
        'cat_fields':  sorted(obs.get('categorical') or []),
        'num_fields':  sorted(obs.get('numerical') or []),
        'default_layer': db.get('default_layer') or 'X',
        # Curated defaults — drive the plot form's pre-population.
        'top_genes':   top_genes,
        'top_cat':     top_cat,
        'top_num':     top_num,
        # DE study list (empty unless this is a DE geom) — seeds the
        # study→term→contrast cascade in plot.html.
        'de_studies':  de_studies,
    }

    return templates.TemplateResponse(request, "plot.html", {
        "folder_name":      friendly_group,
        "folder_browse_id": group_id,
        "group_id":         group_id,
        "file_id":          file_id,
        "dataset_name":     dataset_title,
        "db_id":            db_id,
        "is_secondary":     bool(db.get('is_secondary')),
        "geom":             geom,
        "geom_label":       _PLOT_GEOMS_UI[geom]['label'],
        "geom_icon":        _PLOT_GEOMS_UI[geom]['icon'],
        "geom_needs":       _PLOT_GEOMS_UI[geom]['needs'],
        "plot_meta":        plot_meta,
        "all_geoms":        _PLOT_GEOMS_UI,
        # gates DE geoms in the switcher — only shown when the dataset has DE.
        "has_de":           has_de,
        # caveats surfaced ABOVE the plot preview (task #254). Same format
        # dataset.html renders, reduced summary for the header accent.
        "caveats":          caveats,
        "caveats_summary":  caveats_summary,
        "api_url":          "/api",
    })


# ── DE study browser ─────────────────────────────────────────────────────────
# Registered BEFORE dataset_view (same reason as plot_view): the {dataset:path}
# catch-all would otherwise swallow "<file>/de/<study>".

@router.get("/browse/{folder_browse_id}/{dataset_browse_id}/de/{study}",
            response_class=HTMLResponse)
async def de_study_view(request: Request, folder_browse_id: str,
                        dataset_browse_id: str, study: str):
    """Per-study DE explorer: term → contrast tree + a sortable/filterable gene
    table.  All the term/contrast/gene fetching happens client-side against the
    /de/{study}/… endpoints; this handler just resolves the study metadata and
    the breadcrumb context."""
    group_id = unquote(folder_browse_id)
    file_id = unquote(dataset_browse_id)
    study_id = unquote(study)
    db_id = f"{group_id}/{file_id}"
    api_url = _api_url(request)

    try:
        study_meta = _api_get(api_url, f'/databases/{db_id}/de/{study_id}')
    except Exception as error:
        print(f"API error: {error}")
        return templates.TemplateResponse(
            request, "index.html",
            {"folders": [], "error": f"DE study '{study_id}' not found on {db_id}"},
            status_code=404,
        )

    # Breadcrumb names (best-effort).
    friendly_group, dataset_title = group_id, file_id
    is_secondary = False
    try:
        info = _api_get(api_url, f'/databases/{db_id}/info')
        coll = info.get('collection') or {}
        friendly_group = (
            (coll.get('title') or '').strip()
            or coll.get('group')
            or group_id
        )
        dataset_title = info.get('title') or file_id
        is_secondary = bool(info.get('is_secondary'))
    except Exception:
        pass

    return templates.TemplateResponse(request, "de_browser.html", {
        "folder_name":      friendly_group,
        "folder_browse_id": group_id,
        "group_id":         group_id,
        "file_id":          file_id,
        "dataset_name":     dataset_title,
        "db_id":            db_id,
        "study_id":         study_id,
        "study":            study_meta,
        "api_url":          "/api",
        "is_secondary":     is_secondary,
    })


@router.get("/browse/{folder_browse_id}/{dataset_browse_id:path}", response_class=HTMLResponse)
async def dataset_view(request: Request, folder_browse_id: str, dataset_browse_id: str):
    group_id = unquote(folder_browse_id)
    file_id = unquote(dataset_browse_id)
    db_id = f"{group_id}/{file_id}"
    api_url = _api_url(request)

    try:
        db = _api_get(api_url, f'/databases/{db_id}/info')
    except Exception as error:
        print(f"API error: {error}")
        return templates.TemplateResponse(
            request, "index.html",
            {"folders": [], "error": f"Dataset '{db_id}' not found"},
            status_code=404
        )

    # Differential-expression studies (best-effort — empty if none ingested or
    # the geneset/DE store isn't configured). Drives the DE panel + badge.
    de_studies: List[Dict] = []
    try:
        de_studies = _api_get(api_url, f'/databases/{db_id}/de').get('studies', [])
    except Exception:
        pass
    de_total_contrasts = sum((s.get('n_contrasts') or 0) for s in de_studies)

    layers = list(db.get('layers', {}).keys())
    # Filter out latent-space obsm keys (X_scVI, X_pca, scvi_latents, …) — they
    # scatter to meaningless dense blobs. See _is_visual_embedding above.
    embeddings = [
        name for name in db.get('embeddings', {}).keys() if _is_visual_embedding(name)
    ]
    obs = db.get('obs', {})

    # Default embedding for the "quick scatter" icons on the obs table:
    #   1. `h5ad_embedding` from .h5ad.mus if present + still visual
    #   2. else X_umap if present
    #   3. else X_tsne if present
    #   4. else the first remaining visual embedding
    #   5. else None (no scatter icon rendered)
    _hint = db.get('h5ad_embedding')
    if _hint and _hint in embeddings:
        default_embedding = _hint
    elif 'X_umap' in embeddings:
        default_embedding = 'X_umap'
    elif 'X_tsne' in embeddings:
        default_embedding = 'X_tsne'
    elif embeddings:
        default_embedding = embeddings[0]
    else:
        default_embedding = None

    # Split numerical obs columns into module-score triples (grouped by
    # (catalog, module) so scanpy/starcat/ucell for the same module collapse
    # to one row with per-method chips) vs everything else.
    score_grouped = _group_module_scores(obs.get('numerical', []) or [])
    module_scores = score_grouped['modules']
    method_counts = score_grouped['method_counts']

    # Group modules by catalog so the Module-scores tab can render one
    # collapsible <details> block per catalog. First catalog opens by default.
    module_scores_by_catalog: List[Dict[str, Any]] = []
    if module_scores:
        by_cat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for mod in module_scores:
            by_cat[mod.get('catalog') or ''].append(mod)
        for cat_name in sorted(by_cat.keys(), key=lambda s: s.lower()):
            module_scores_by_catalog.append({
                'catalog': cat_name or '(uncategorised)',
                'modules': by_cat[cat_name],
                'n':       len(by_cat[cat_name]),
            })

    # Hide auto-generated starCAT columns from the raw obs listings on the
    # dataset page. Numerical starCAT scores (``starcat__<catalog>__<module>``)
    # are already routed into the Module Scores tab by ``_group_module_scores``;
    # the derived categorical flags (``starcat__…_pos`` / ``…_high`` /
    # ``…_Identity_Label``) are pure clutter in the Categorical tab. Keep them
    # out of ``obs_columns`` (which drives both the Numerical and Categorical
    # tables). The data itself is untouched — plot endpoints still accept these
    # column names for users who know them.
    cat_cols_filtered = [
        col for col in obs.get('categorical', []) if not col.startswith(_STARCAT_PREFIX)
    ]
    num_cols_filtered = [
        col for col in score_grouped['non_score_cols'] if not col.startswith(_STARCAT_PREFIX)
    ]
    obs_columns = (
        [{'name': col, 'type': 'num'} for col in num_cols_filtered] +
        [{'name': col, 'type': 'cat'} for col in cat_cols_filtered]
    )

    info = {
        'n_cells': db.get('n_cells'),
        'n_genes': db.get('n_genes'),
        'n_obs_columns': len(obs_columns),
        'layers': layers,
        'obsm_keys': embeddings,
        'file_id': db.get('file_id'),
        'group_id': db.get('group_id'),
        'title': db.get('title'),
        'modified': _modified_ts(db),
        # lineage (API: primary / reanalysis / unresolved, donor group)
        'designation': db.get('designation'),
        'reanalysis_of': _as_list(db.get('reanalysis_of')),
        'donor_group': db.get('donor_group'),
        'has_donor_uid': bool(db.get('has_donor_uid')),
        # replicate unit and disease definition (API: unreleased; absent on older servers)
        'replicate_unit': db.get('replicate_unit'),
        'replicate_column': db.get('replicate_column'),
        'replicate_basis': db.get('replicate_basis'),
        'disease_basis': db.get('disease_basis'),
        'facet_scope': db.get('facet_scope'),
        'control_basis': db.get('control_basis'),
    }

    accession = db.get('accession')
    _celltype = db.get('cell_type') or db.get('celltype')
    _top_genes_raw = _as_list(db.get('top_genes'))
    # Gene panel for the Genes tab: prefer dataset's precomputed favourites,
    # else a canonical curated panel keyed on cell type (microglia / astrocyte
    # / neuron). Non-matching cell types get no panel (source='none').
    _panel_genes, _panel_source = _resolve_gene_panel(_top_genes_raw, _celltype)
    _top_genes = _panel_genes                 # legacy alias for the Explore chip
    top_genes_fallback = _panel_source != 'precomputed' and bool(_panel_genes)
    gene_panel = {
        'genes':  _panel_genes,
        'source': _panel_source,
        'label':  {
            'precomputed':          'dataset favourites',
            'microglia_fallback':   'generic microglia markers',
            'astrocyte_fallback':   'generic astrocyte markers',
            'neuron_fallback':      'generic neuron markers',
            'none':                 '',
        }[_panel_source],
    }
    dataset_meta = {
        # Biology
        'organism':        db.get('organism'),
        'tissue':          _as_list(db.get('tissue')),
        'celltype':        _celltype,
        'suspension_type': db.get('suspension_type'),
        'disease':         _as_list(db.get('disease')),
        'condition':       _as_list(db.get('condition')),
        'sex':             db.get('sex'),
        'age':             db.get('age'),
        'strain':          db.get('strain'),
        'technology':      _as_list(db.get('technology')),
        # Provenance + access
        'access':          db.get('access'),
        'source':          db.get('source'),
        'license':         db.get('license'),
        'year':            db.get('year'),
        'contributor':     db.get('contributor'),
        'accession':       accession,
        'accession_url':   _accession_url(str(accession)) if accession else None,
        'doi':             _as_list(db.get('doi')),
        'pmid':            _as_list(db.get('pmid')),
        'link':            _as_list(db.get('link')),
        # UI hints from .md
        'top_genes':       _top_genes,
        'top_cat':         _as_list(db.get('top_cat')),
        'top_num':         _as_list(db.get('top_num')),
    }

    # Description = the .md body MINUS a leading "# <title>" heading (which just
    # repeats the title). If nothing substantive remains (the common "no real
    # description" case where the body is only the heading), show no description.
    raw_md = db.get('markdown') or ''
    _md_lines = raw_md.lstrip().split('\n')
    _desc_md = ('\n'.join(_md_lines[1:]) if (_md_lines and _md_lines[0].lstrip().startswith('#'))
                else raw_md).strip()
    markdown_content = markdown.markdown(_desc_md, extensions=['tables', 'fenced_code']) if _desc_md else None
    markdown_content = _expand_plot_thumbnails(markdown_content, default_db=db_id)

    # Context bar — same abbrev maps as landing / folder pages.  Uses single-db
    # summarise so 'mix' never appears (one dataset → one value).  Empty
    # `abbrev` is correctly skipped by the template guards.
    context_bar = {
        'organism':        _summarize_meta_field([db], 'organism',        abbrev_map=_ORGANISM_ABBREV),
        'technology':      _summarize_meta_field([db], 'technology',      abbrev_map=_TECHNOLOGY_ABBREV),
        'celltype':        _summarize_meta_field([db], 'celltype',        abbrev_map=_CELLTYPE_ABBREV),
        'tissue':          _summarize_meta_field([db], 'tissue',          abbrev_map=_TISSUE_ABBREV),
        'disease':         _summarize_meta_field([db], 'disease',         abbrev_map=_DISEASE_ABBREV),
        'suspension_type': _summarize_meta_field([db], 'suspension_type', abbrev_map=_SUSPENSION_ABBREV),
        'access':          _summarize_meta_field([db], 'access',          abbrev_map=_ACCESS_ABBREV),
        'source':          _summarize_meta_field([db], 'source',          abbrev_map=_SOURCE_ABBREV),
        'year':            db.get('year'),
    }
    # Counts follow the filtered lists: starcat__* columns are hidden from
    # the raw obs tabs, so they don't contribute to the tab-bar badges either.
    n_cat = len(cat_cols_filtered)
    # n_num = the plain (non-score) numerical columns shown in the Numerical
    # tab; module-score columns get their own "Module scores" tab now.
    n_num = len(num_cols_filtered)
    n_modules = len(module_scores)

    layer_stats = db.get('layer_stats') or {}
    # layer_stats holds the union of every layer in the h5ad (machine computed).
    # A curator can hide layers via layers_include on the .md, and ant-serve's
    # /info endpoint already scopes db['layers'] to those. Mirror that filter
    # here so hidden layers do not leak into the Layers card stats table.
    _served_layers = db.get('layers') or {}
    layer_stats_rows = [
        {
            'name':      name,
            'dtype':     stats.get('dtype', ''),
            'min':       format_number(stats['min_value'])     if stats.get('min_value')    is not None else '',
            'min_nz':    format_number(stats['min_nonzero'])   if stats.get('min_nonzero')  is not None else '',
            'max':       format_number(stats['max_value'])     if stats.get('max_value')    is not None else '',
            'median':    format_number(stats['median'])        if stats.get('median')       is not None else '',
            'q90':       format_number(stats['q90'])           if stats.get('q90')          is not None else '',
            'pct_zeros': format_number(stats['pct_zeros'])     if stats.get('pct_zeros')    is not None else '',
        }
        for name, stats in layer_stats.items()
        if name in _served_layers
    ]

    collection = db.get('collection') or {}
    # Folder name (never overridden). Feeds the id-strip `data_project`
    # label so admins can copy-paste the real folder path.
    group_folder_name = collection.get('group') or group_id
    # Display name for the breadcrumb. index.md `title` overrides when set.
    friendly_group = (
        (collection.get('title') or '').strip()
        or group_folder_name
    )
    dataset_title = db.get('title') or file_id

    # Known caveats (REST /info `caveats: []`) — surfaced as a warning card
    # near the top of the dataset page. The Python side reduces to a max
    # severity so the card's accent colour follows the highest-level entry
    # (cleaner than doing a max-reduce in Jinja).
    caveats = db.get('caveats') or []
    caveats_summary = _summarize_caveats(caveats)

    # Unified access + publication-status icon (shape=access, colour=pub).
    # publication_status is a collection-level index.md field ant-serve doesn't
    # yet expose — read straight from disk (cached).
    group_folder = (db.get('collection') or {}).get('group') or group_id
    pub_status = _publication_status_for_group(group_folder)
    pub_icon = _access_pub_icon(db.get('access'), pub_status)

    # Facet projection (ant_* columns: source column, mapping kind, value
    # translation). Best-effort: an older ant-serve without the endpoint
    # just hides the card.
    dataset_facets: List[Dict[str, Any]] = []
    try:
        dataset_facets = _api_get_as_user(request, api_url, f'/databases/{db_id}/facets').get('facets') or []
    except Exception:
        dataset_facets = []
    # Facets outside the configured visible list (facets_visible) are not
    # shown for now; ant-serve flags them, they stay in the data and the API.
    # They are not dropped: the Facets tab lists them under "Not analysed yet".
    dataset_facets_other = [f for f in dataset_facets if f.get('hidden_by_config')]
    dataset_facets = [f for f in dataset_facets if not f.get('hidden_by_config')]

    facet_tests = _dataset_facet_tests(api_url, db_id)
    observed_columns = db.get('obs') if isinstance(db.get('obs'), dict) else {}
    tissue_detail = {
        'specific': _flatten_annotation_field(db.get('tissue')),
        'column': 'tissue_uberon' in (observed_columns.get('categorical') or []),
    }
    return templates.TemplateResponse(request, "dataset.html", {
        "tissue_detail": tissue_detail,
        "dataset_facets": dataset_facets,
        "dataset_facets_other": dataset_facets_other,
        "n_facets_total": sum(1 for f in dataset_facets + dataset_facets_other if f.get('live', True)),
        # an excluded dataset (spatial, proteomics, metacells, mouse) keeps the tab to say why it has no facet analysis
        "show_facets_tab": bool(dataset_facets or dataset_facets_other or (facet_tests or {}).get('excluded')),
        "folder_name": friendly_group,
        "folder_browse_id": group_id,
        "group_id": group_id,
        "data_project": group_folder_name,       # collection folder name (e.g. Sierksma2025)
        "basename": db.get('basename'),           # .md filename stem (from API when available)
        "dataset_name": dataset_title,
        # One-line description from the .md frontmatter (edited via the admin
        # editor's Basics tab). Rendered near the title in dataset.html.
        "dataset_description": (db.get('description') or '').strip() or None,
        "file_id": file_id,
        "db_id": db_id,
        # is_secondary marker (surfaced next to the title + h1 gets the
        # `.secondary-dataset` mute-class). Defaults to False so a rescan-
        # lagging API still renders.
        "is_secondary": bool(db.get('is_secondary')),
        # Dataset version (semver) and last-updated date, shown small and grey.
        "dataset_version": str(db.get('version')).strip() if db.get('version') else None,
        "dataset_updated": str(db.get('updated'))[:10] if db.get('updated') else None,
        "info": info,
        "markdown_content": markdown_content,
        "obs_columns": obs_columns,
        "default_embedding": default_embedding,
        "gene_panel": gene_panel,
        "layer_stats_rows": layer_stats_rows,
        "dataset_meta": dataset_meta,
        "context_bar": context_bar,
        "n_cat_cols": n_cat,
        "n_num_cols": n_num,
        "categorical_plot_max_distinct": CATEGORICAL_PLOT_MAX_DISTINCT,
        "module_scores": module_scores,
        "module_scores_by_catalog": module_scores_by_catalog,
        "n_module_scores": n_modules,
        "score_method_counts": method_counts,
        "top_genes_fallback": top_genes_fallback,
        "has_duckdb": True,
        "update_pending": False,
        "de_studies": de_studies,
        "de_total_contrasts": de_total_contrasts,
        "papers": _dataset_papers(api_url, db_id),
        "facet_tests": facet_tests,
        "replicate": _replicate_view(info, (facet_tests or {}).get('replicate_source')),
        "caveats": caveats,
        "caveats_summary": caveats_summary,
        "publication_status": pub_status,
        "pub_icon": pub_icon,
        "is_admin": _is_admin(request),
        "mv_ctx": {
            "scope": "dataset",
            "dataset": db_id,
            "id": "mv-modules-tab",
            # For the empty-state branching in _module_view.html: does this
            # dataset carry any facet (`ant_*`) obs columns at all?
            # No facets means variability cannot be computed, so the message
            # needs to say that plainly rather than "computing…".
            "has_canonical_tags": any(
                str(col).startswith("ant_")
                for col in (obs.get("categorical", []) or [])
                          + (obs.get("numerical", []) or [])
            ),
            # Authoritative kind lookup for THIS dataset's obs columns.
            # _module_view.html's plotUrl uses these to pick scatter vs box
            # per-tag_column, so a tag that's "canonically numerical" but
            # stored categorically on this dataset (e.g. ant_age as
            # "young"/"old") gets a box plot link, not a broken scatter.
            "obs_numerical":   list(obs.get("numerical", []) or []),
            "obs_categorical": list(obs.get("categorical", []) or []),
        },
    })


@router.get("/admin/edit/{folder_browse_id}/{dataset_browse_id}", response_class=HTMLResponse)
async def edit_dataset_view(request: Request, folder_browse_id: str, dataset_browse_id: str):
    """Admin-only editor for a dataset's .md (curatable fields + markdown body).
    Most of the .md is machine-generated (id/sha256/n_obs/layer_stats/…) and is
    shown read-only or hidden; biology annotation lives in the .mus sidecar, not
    here. Loads/saves via ant-serve GET/PUT /databases/{db_id}/source."""
    group_id = unquote(folder_browse_id)
    file_id = unquote(dataset_browse_id)
    if not _is_admin(request):
        return templates.TemplateResponse(
            request, "index.html",
            {"folders": [], "error": "Not authorized — admin only."},
            status_code=403,
        )
    return templates.TemplateResponse(request, "edit_dataset.html", {
        "group_id": group_id,
        "file_id": file_id,
        "db_id": f"{group_id}/{file_id}",
    })


# ── geneset routes ─────────────────────────────────────────────────────────────

@router.get("/about", response_class=HTMLResponse)
async def about_view(request: Request):
    """Static 'about anthive' page — logo, what-it-is, live catalogue counts."""
    api_url = _api_url(request)
    n_datasets = n_collections = api_version = None
    try:
        databases = _all_databases(api_url)
        n_datasets = len(databases)
        n_collections = len({
            (db.get('group_id') or db['id'].split('/')[0]) for db in databases
        })
    except Exception as error:
        print(f"API error: {error}")
    try:
        api_version = _api_get(api_url, '/version').get('version')
    except Exception:
        pass
    return templates.TemplateResponse(request, "about.html", {
        "logo_data_uri": _LOGO_DATA_URI,
        "n_datasets": n_datasets,
        "n_collections": n_collections,
        "api_version": api_version,
        "genesets_available": getattr(request.app.state, 'genesets_available', False),
    })


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_view(request: Request):
    """Chat-assistant privacy & data-handling note. Draft copy — to be revised."""
    return templates.TemplateResponse(request, "privacy.html", {})


@router.get("/help", response_class=HTMLResponse)
async def help_view(request: Request):
    """User-facing cheat-sheet: what the site is, how to navigate, what the
    dataset tabs mean, facets, icons, and where preferences live.

    Reachable from the topbar 'Help' link. Purely informational — no
    server-side interactivity, no admin gate. Live catalogue counts are best-
    effort so the page renders even if the REST API is briefly unavailable.
    """
    api_url = _api_url(request)
    n_datasets: Optional[int] = None
    n_collections: Optional[int] = None
    n_genesets: Optional[int] = None
    try:
        databases = _all_databases(api_url)
        n_datasets = len(databases)
        n_collections = len({
            (db.get('group_id') or db['id'].split('/')[0]) for db in databases
        })
    except Exception as error:
        print(f"API error (help counts): {error}")
    try:
        genesets_payload = _api_get(api_url, '/genesets') or {}
        sets_list = genesets_payload.get('genesets') if isinstance(genesets_payload, dict) else None
        if isinstance(sets_list, list):
            n_genesets = len(sets_list)
    except Exception:
        pass
    return templates.TemplateResponse(request, "help.html", {
        "n_datasets": n_datasets,
        "n_collections": n_collections,
        "n_genesets": n_genesets,
    })


def _api_guide_site_settings() -> Dict[str, Any]:
    """Login mode and configured public base URL for the API page.

    Read per render (cheap; tests and sites without a config fall back to
    the defaults). mode None means "not declared", which behaves like
    authelia/basic: the proxy asks for HTTP basic on /api.
    """
    try:
        from config import (get_auth_settings, get_library_upload_settings,  # noqa: PLC0415
                            load_config)
        cfg = load_config()
        auth = get_auth_settings(cfg)
        mode, admins = auth['mode'], set(auth['admins'])
        upload = get_library_upload_settings(cfg)
        upload_groups, max_pdf_mb = list(upload['upload_groups']), int(upload['max_pdf_mb'])
        public = str(((cfg.get('mcp') or {}).get('public_base_url')) or '').strip()
    except Exception:  # config unavailable (tests import this module bare)
        mode, admins, upload_groups, max_pdf_mb, public = None, set(), ['admins'], 25, ''
    return {'mode': mode, 'public_base_url': public.rstrip('/'), 'admins': admins,
            'upload_groups': upload_groups, 'max_pdf_mb': max_pdf_mb}


def _may_upload_papers(request: Request, settings: Dict[str, Any]) -> bool:
    """Whether the caller may add papers to the library.

    Mirrors ant-serve's ``_require_uploader`` (which stays authoritative):
    a signed-in user in one of ``library_upload_groups``, or, when admins
    may upload, an admin by group or (basic mode, no groups) by name in
    ``auth.admins``. Only decides whether the page SHOWS upload commands.
    """
    user = request.headers.get('remote-user') or ''
    if settings['mode'] == 'none' or not user:
        return False
    raw_groups = request.headers.get('remote-groups') or ''
    groups = {g.strip().lower() for g in raw_groups.split(',') if g.strip()}
    allowed = set(settings['upload_groups'])
    if groups & allowed:
        return True
    return ('admins' in allowed and settings['mode'] == 'basic' and not raw_groups
            and user in settings['admins'])


def _site_base_url(request: Request, configured: str = '') -> str:
    """Public https base URL of this site, for copy-pasteable examples.

    The configured public URL wins. Otherwise the host the browser used
    (X-Forwarded-Host, then Host). The scheme is always https: the API
    sends a password with every request, so the examples never show http.
    """
    if configured:
        return configured
    host = (request.headers.get('x-forwarded-host') or request.headers.get('host')
            or 'localhost').split(',')[0].strip()
    return f"https://{host}"


@router.get("/api-guide", response_class=HTMLResponse)
async def api_guide_view(request: Request):
    """How to use the REST API: login, first curl calls, pyvark, vark.

    The top-bar 'API' link and the user-menu 'API' entry land here; the page
    links on to the interactive docs (/api/docs). Examples are rendered with
    this site's own base URL and the signed-in user's name. The API version
    is best-effort (the page renders without it).
    """
    settings = _api_guide_site_settings()
    base_url = _site_base_url(request, settings['public_base_url'])
    api_version: Optional[str] = None
    api_released: Optional[str] = None
    try:
        version_payload = _api_get(_api_url(request), '/version') or {}
        api_version = version_payload.get('version') or None
        api_released = version_payload.get('released') or None
    except Exception as error:
        print(f"API error (api guide version): {error}")
    host = base_url.split('://', 1)[-1].split('/', 1)[0].split(':', 1)[0]
    return templates.TemplateResponse(request, "api_guide.html", {
        "base_url": base_url,
        "api_base": base_url + "/api",
        "netrc_host": host,
        "user": request.headers.get('remote-user') or '',
        "auth_mode": settings['mode'],
        "api_version": api_version,
        "api_released": api_released,
        "may_upload_papers": _may_upload_papers(request, settings),
        "upload_groups": settings['upload_groups'],
        "max_pdf_mb": settings['max_pdf_mb'],
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    """User-preferences page (per-browser, localStorage-only).

    Renders a shell with two controls:

    - **Module scoring algorithm** — scanpy (default) vs UCell. Read by the
      module drilldown (`_module_view.html`) and the module-variability
      heatmap (`geneset_variability.html`) via `window.antSettings`.
    - **Favourite genes** — comma/newline list; upper-cased on save. Read by
      the dataset Genes tab (star + top-pin) and the gene atlas
      (title marker) via `window.antSettings`.

    Nothing here is server-persisted; no server endpoint is called. Storage
    keys live under the shared `ant.remember.*` prefix used elsewhere.
    """
    return templates.TemplateResponse(request, "settings.html", {})


@router.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback_view(request: Request):
    """Admin-only triage of chat-assistant feedback flags. The rows are fetched
    client-side from the admin-gated GET /chat/feedback (which carries the
    flagged assistant message content); this route only renders the shell and
    gates SHOWING it on the Authelia admins group."""
    if not _is_admin(request):
        return templates.TemplateResponse(
            request, "index.html",
            {"folders": [], "error": "Not authorized — admin only."},
            status_code=403,
        )
    return templates.TemplateResponse(request, "admin_feedback.html", {})


#: Stands in for the user's MCP link in the server-rendered setup prompt.
#: The page's JS swaps in the real link (fetched from /api/me/mcp), so the
#: secret never appears in the HTML.
MCP_PROMPT_LINK_PLACEHOLDER = '__ANTHIVE_MCP_LINK__'

_MCP_NAME_SKIP_LABELS = {'www', 'anthive'}


def _mcp_connection_name(base_url: str) -> str:
    """A per-site MCP connection name such as ``anthive-intern``.

    Taken from the first meaningful host label of the site's public URL
    (``www`` and ``anthive`` are skipped when more labels follow), so users
    with several anthive servers get a distinct name per site. IP addresses
    and empty hosts fall back to plain ``anthive``.
    """
    host = base_url.split('://', 1)[-1].split('/', 1)[0].split(':', 1)[0].lower()
    if not host or re.fullmatch(r'[0-9.]+', host):
        return 'anthive'
    labels = [label for label in host.split('.') if label]
    while len(labels) > 1 and labels[0] in _MCP_NAME_SKIP_LABELS:
        labels = labels[1:]
    label = re.sub(r'[^a-z0-9-]+', '-', labels[0] if labels else '').strip('-')
    if not label or label == 'anthive':
        return 'anthive'
    return label if label.startswith('anthive-') else f'anthive-{label}'


def _mcp_setup_prompt(link: str, connection_name: str, site_url: str,
                      api_url: str, user: str, docs_url: str) -> str:
    """The 'set up with one paste' prompt for an AI agent with a shell.

    Plain language, numbered, with the safety rules first. ``link`` is the
    user's personal MCP link (or MCP_PROMPT_LINK_PLACEHOLDER when the page
    renders it, the JS fills in the real one).
    """
    # Only the non-secret start of the link: the token follows /mcp/u/.
    masked = f"{site_url.rstrip('/')}/mcp/u/..."
    user_text = user or '<your anthive user name>'
    return f"""Please help me connect this computer's AI agent to Anthive, a single-cell data browser at {site_url}. I am a beginner, so explain each step in plain language and keep it short.

Facts you need.
- My personal Anthive MCP link is {link}
- Use the connection name {connection_name}
- My Anthive user name is {user_text}
- The Anthive API address (only for the optional vark tool) is {api_url}

Safety rules. Follow them for the whole session.
- The MCP link works like my password. Put it only in your own user-level (global) MCP configuration on this computer. Never write it into a project folder, a git repository, a shared or synced file, a script or a note.
- Never repeat the link in full in your messages or summaries. If you need to refer to it, write only its start, for example {masked}
- Never ask me for my Anthive password, and never type, store or read it. When a step needs it, I type it myself in my own terminal.
- Do not change anything before I say OK in step 1.
- If a step fails, stop. Tell me which step failed and what the error message said, then ask me how to continue. Do not improvise workarounds and do not edit configuration in ways this prompt does not describe.
- If your sandbox or permission settings block a command, ask me to approve it, or give me the command so I can run it myself.

1. Explain and ask. In a few sentences, tell me what you will do in steps 2 to 5, which settings or files on this computer will change, and that step 4 is optional. Then wait for my OK.

2. Add the Anthive connection. Work out which AI agent you are and add the connection at user level, so it works in all my projects (never only in the current project folder).
   - For Claude Code, run
     claude mcp add --transport http --scope user {connection_name} {link}
   - For OpenAI Codex, run
     codex mcp add {connection_name} --url {link}
   - For Gemini CLI, run
     gemini mcp add --scope user --transport http {connection_name} {link}
   - For any other agent (OpenCode, Cursor, Windsurf, VS Code, Zed, Goose and others), open {docs_url}/MCP_CLIENTS.md, find the section for your agent and follow it, using your user-level (global) configuration file, the connection name {connection_name} and my link. Anthive needs no sign-in, so if your configuration has an OAuth or sign-in option, switch it off. If your agent is not listed, tell me and stop this step.
   If a connection named {connection_name} already exists, tell me (without showing its full link) and ask whether to replace it.

3. Install the Anthive skills. Skills are small instruction files that teach you how to use Anthive well. Check whether Node.js is installed by running node --version
   - If it is, run
     DISABLE_TELEMETRY=1 npx skills add https://gitlab.kuleuven.be/bdslab/anthive/skills.git -g --agent '*'
     In Windows PowerShell, which does not understand the DISABLE_TELEMETRY=1 prefix, first run $env:DISABLE_TELEMETRY=1 and then the same command without that prefix.
     A few agents, such as Eve and PromptScript, only take per-project skills, so the output may contain lines like "Eve does not support global skill installation". Those lines are expected and harmless and do not count as a failed step. Tell me so, then verify the install by checking that the anthive-* folders exist in ~/.agents/skills or ~/.claude/skills.
   - If Node.js is missing, do not install it on your own. Tell me how to install it for my operating system (https://nodejs.org) and ask whether I want to do that now. If I prefer not to, offer to copy the folders under skills/ from https://gitlab.kuleuven.be/bdslab/anthive/skills into your own user-level skills folder (for Claude Code ~/.claude/skills/, for most other agents ~/.agents/skills/).

4. Optional, ask me first: install vark. vark is a command-line tool for querying Anthive from a terminal or a script. Ask whether I want it. If I say no, skip to step 5.
   a. If vark is already installed (vark version prints a version), run vark upgrade
      Otherwise install it with
      curl -sSL https://gitlab.kuleuven.be/bdslab/anthive/vark/-/raw/main/install.sh | bash
      On Windows this needs Git Bash or WSL. If neither is available, tell me and skip vark.
   b. vark needs my password once. Do not run the login yourself. Ask me to open my own terminal window, run the command below and type my password when it asks:
      vark server login --name {connection_name} --url {api_url} --user {user_text}
      Wait until I tell you it is done.

5. Check that it works.
   - Show that the connection is registered, with claude mcp list, codex mcp list, gemini mcp list or your agent's equivalent. Do not copy the full link from that output into your reply.
   - Most agents only load a new MCP connection in a new session. If that applies to you, tell me exactly how to restart you. If you can already see Anthive tools (for example search_datasets), call search_datasets once with search set to microglia and tell me how many datasets came back.
   - If vark is installed, run vark server check {connection_name} -o json and tell me whether "ok" is true. If it is not, tell me the "problem" and "hint" it reports. An older vark without the check command needs vark upgrade first.

6. Finish with a short summary: what you added and where (with the link shortened as above), what was skipped, and what I still need to do, such as restarting you. Then suggest a first question to ask once Anthive is connected, for example "Which Anthive datasets are about microglia in Alzheimer's disease?"
"""


@router.get("/account/mcp", response_class=HTMLResponse)
async def account_mcp_view(request: Request):
    """The signed-in user's private MCP link: create, show, copy, rotate,
    revoke, plus per-client setup snippets and the 'set up with one paste'
    agent prompt. Everything secret is fetched client-side from ant-serve's
    /me/mcp (which identifies the user from the same proxy headers); the
    prompt is rendered here with a placeholder that the JS replaces with
    the chosen link. This route refuses un-proxied access."""
    user = request.headers.get('remote-user')
    if not user:
        return templates.TemplateResponse(
            request, "account_mcp.html", {"signed_in": False}, status_code=401)
    site_url = _site_base_url(request, _api_guide_site_settings()['public_base_url'])
    connection_name = _mcp_connection_name(site_url)
    setup_prompt = _mcp_setup_prompt(
        MCP_PROMPT_LINK_PLACEHOLDER, connection_name, site_url, site_url + '/api',
        user, _docs_base_url())
    return templates.TemplateResponse(
        request, "account_mcp.html",
        {"signed_in": True, "user": user, "is_admin": _is_admin(request),
         "mcp_connection_name": connection_name,
         "mcp_setup_prompt": setup_prompt,
         "mcp_prompt_placeholder": MCP_PROMPT_LINK_PLACEHOLDER})


@router.get("/admin/mcp-links")
async def admin_mcp_links_view(request: Request):
    """The admin overview now lives on /account/mcp (section #all)."""
    return RedirectResponse("/account/mcp#all", status_code=307)


# Example genes on the /genes landing page: the one place to change them.
LANDING_EXAMPLE_GENES = ('PTPRG', 'APOE', 'TREM2', 'GFAP', 'MEIS1', 'CD9', 'P2RY12', 'AQP4')


@router.get("/genes", response_class=HTMLResponse)
async def gene_index_view(request: Request, q: Optional[str] = None):
    """Gene atlas entry page: a search form that leads to /genes/{gene}.

    The top-bar "genes" link lands here. A submitted ``?q=`` (one gene or a
    comma-separated list) redirects to the atlas, so the form works without
    JavaScript.
    """
    query = ','.join(part.strip() for part in (q or '').split(',') if part.strip())
    if query:
        return RedirectResponse(url='/genes/' + url_quote(query, safe=','),
                                status_code=302)
    return templates.TemplateResponse(request, "genes.html", {
        "gene": "",
        "pill_map": {},
        "filter_opts": {'organism': [], 'cell_type': [], 'tissue': []},
        "dataset_annotations": {},
        "example_genes": LANDING_EXAMPLE_GENES,
        "logged_in": bool(request.headers.get('remote-user')),
    })


@router.get("/genes/{gene}", response_class=HTMLResponse)
async def gene_view(request: Request, gene: str):
    """Single-gene cross-catalogue atlas — where is {gene} expressed?

    Server-renders the shell + a catalogue pill map (db_id → context pills,
    fast/cached); the (possibly slow, cold ~10-60s) per-gene cross-dataset stats
    are fetched client-side from /api/gene_stats_all so the page never blocks.

    Also passes dataset_annotations (same structure as the module view) so the
    client-side table can render hover popovers and populate facet filters
    (cell_type / tissue / system).
    """
    api_url = _api_url(request)
    pill_map: Dict[str, Dict] = {}
    # distinct values for the in-page filter dropdowns (server-side filter params
    # on /gene_stats_all: organism / cell_type / tissue), by frequency.
    _opt: Dict[str, Dict[str, int]] = {'organism': {}, 'cell_type': {}, 'tissue': {}}
    _field_for = {'organism': 'organism', 'cell_type': 'celltype', 'tissue': 'tissue'}
    try:
        for db in _all_databases(api_url):
            did = db.get('id') or ''
            if not did:
                continue
            pill_map[did] = {
                'group':    db.get('group_id') or did.split('/')[0],
                'organism': _summarize_meta_field([db], 'organism', abbrev_map=_ORGANISM_ABBREV),
                'celltype': _summarize_meta_field([db], 'celltype', abbrev_map=_CELLTYPE_ABBREV),
                'disease':  _summarize_meta_field([db], 'disease',  abbrev_map=_DISEASE_ABBREV),
            }
            for fkey, dbfield in _field_for.items():
                raw = db.get(dbfield)
                for item in (raw if isinstance(raw, list) else [raw]):
                    if item:
                        _opt[fkey][item] = _opt[fkey].get(item, 0) + 1
    except Exception as error:
        print(f"API error: {error}")
    filter_opts = {k: [v for v, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]
                   for k, c in _opt.items()}
    dataset_annotations = _build_dataset_annotations(api_url)
    return templates.TemplateResponse(request, "genes.html", {
        "gene": gene,
        "pill_map": pill_map,
        "filter_opts": filter_opts,
        "dataset_annotations": dataset_annotations,
        "view_dbs": _view_db_map(api_url) if ',' not in gene else {},
        "view_categories": VIEW_CATEGORIES,
    })


@router.get("/genes/{gene}/author-de")
def gene_author_de(request: Request, gene: str):
    """JSON: the author DE rows of one gene (the "Author DE (as published)" tab).

    Aggregated here because the API serves author DE per dataset only: the datasets with
    DE are found once (cached for an hour), then each is asked for the gene. Rows carry a
    plain contrast label; they are never combined with the facet results.
    """
    from fastapi.responses import JSONResponse
    try:
        rows = _gene_author_de_rows(_api_url(request), gene)
    except Exception as error:
        print(f"API error: {error}")
        return JSONResponse({'gene': gene, 'rows': [], 'error': 'unavailable'}, status_code=502)
    return {'gene': gene, 'rows': rows}


@router.get("/genesets", response_class=HTMLResponse)
async def genesets_index(request: Request):
    """Genesets landing page.

    Renders a drilldown table of studies (one row per study). Filters for
    cell type and organism sit above the table (folder-style header
    popovers). starCAT was surfaced here previously, but the entry point
    is now hidden. Deep-links to ``/genesets/starcat/…`` still resolve
    via the dedicated route.
    """
    api_url = _api_url(request)
    # /genesets/studies returns one row per study with title/doi/journal/year/
    # authors/organisms/celltypes/methods pre-aggregated by the API.  Fall back
    # to an empty list if the endpoint is missing (older ant-serve).
    studies: List[Dict[str, Any]] = []
    total_genesets = 0
    try:
        payload = _api_get(api_url, '/genesets/studies') or {}
        studies = payload.get('studies') or []
        total_genesets = sum(int(row.get('n_genesets') or 0) for row in studies)
    except Exception as error:
        print(f"API error (/genesets/studies): {error}")
    # If /genesets/studies is unavailable (older API), fall back to the flat
    # list so the page still renders (without title/doi/year enrichment).
    if not studies:
        try:
            flat = _api_get(api_url, '/genesets') or {}
            genesets = flat.get('genesets', [])
        except Exception as error:
            print(f"API error (/genesets fallback): {error}")
            genesets = []
        per_study: Dict[str, Dict[str, Any]] = {}
        for geneset in genesets:
            study_name = geneset.get('study') or ''
            if not study_name:
                continue
            entry = per_study.setdefault(study_name, {
                'study': study_name,
                'organisms': set(),
                'celltypes': set(),
                'methods': set(),
                'n_experiments': set(),
                'n_genesets': 0,
                'title': '', 'doi': '', 'pmid': '', 'journal': '',
                'year': None, 'authors': [],
            })
            entry['n_genesets'] += 1
            entry['n_experiments'].add(geneset.get('experiment') or '')
            if geneset.get('organism'):
                entry['organisms'].add(geneset['organism'])
            for cell in geneset.get('celltypes') or []:
                if cell:
                    entry['celltypes'].add(cell)
            if geneset.get('method'):
                entry['methods'].add(geneset['method'])
        studies = []
        for study_name, entry in sorted(per_study.items()):
            studies.append({
                'study': study_name,
                'title': entry['title'], 'doi': entry['doi'],
                'pmid': entry['pmid'], 'journal': entry['journal'],
                'year': entry['year'], 'authors': entry['authors'],
                'organisms': sorted(entry['organisms']),
                'celltypes': sorted(entry['celltypes']),
                'methods': sorted(entry['methods']),
                'n_experiments': len(entry['n_experiments']),
                'n_genesets': entry['n_genesets'],
            })
        total_genesets = sum(int(row.get('n_genesets') or 0) for row in studies)

    active_group = request.query_params.get("group")

    # Group selector. starCAT was here previously; we keep the ``starcat``
    # route (``/genesets/starcat/{catalog_id}/modules/{module_name}``) and its
    # template alive for deep-links, but the tab is intentionally hidden on
    # this landing page. Find-by-gene remains available.
    groups = []
    if studies:
        groups.append({"key": "genesets", "label": "Gene sets", "count": len(studies)})
        # Reverse lookup: which sets/programs contain a given gene.
        groups.append({"key": "bygene", "label": "🔍 find by gene", "count": None})
    valid_keys = {g["key"] for g in groups}
    if active_group not in valid_keys:
        active_group = groups[0]["key"] if groups else "genesets"

    # dataset_annotations feeds the parent-dataset `(2)` badge on rendered
    # study rows and inside the drilldown.
    dataset_annotations = _build_dataset_annotations(api_url)
    return templates.TemplateResponse(request, "genesets.html", {
        "studies": studies,
        "total": len(studies),
        "total_genesets": total_genesets,
        "groups": groups,
        "active_group": active_group,
        "dataset_annotations": dataset_annotations,
    })


@router.get("/genesets/variability", response_class=HTMLResponse)
async def geneset_variability_view(request: Request):
    """Fleet-wide module-variability heatmap (REST 2.28 GET /module-variability):
    module × dataset, coloured by how much a chosen tag (braak / sex / cluster /
    …) explains of each module score's variance. Data is fetched client-side
    from /api/module-variability so this route only renders the shell.

    Registered BEFORE the /{study}/{experiment} route so 'variability' isn't
    swallowed as a study id."""
    from canonical_columns import CANONICAL_TAGS, NUMERICAL_TAGS
    # Fleet-wide dataset annotations — the column-header renderer uses this to
    # flag secondary datasets with a `(2)` badge. Same shape as the module view
    # consumes.
    dataset_annotations = _build_dataset_annotations(_api_url(request))
    return templates.TemplateResponse(request, "geneset_variability.html", {
        "canonical_tags": list(CANONICAL_TAGS),
        "numerical_tags": sorted(NUMERICAL_TAGS),
        "dataset_annotations": dataset_annotations,
    })


@router.get("/genesets/starcat/{catalog_id}/modules/{module_name}", response_class=HTMLResponse)
async def geneset_starcat_module_view(request: Request, catalog_id: str, module_name: str):
    """One starCAT program (module): its top-N genes with weights, group, and
    biological description. Fetches /api/catalogs/starcat/{catalog_id}/modules/
    {module_name} client-side (REST 2.30). Registered BEFORE the generic
    /{study}/{experiment}/{name} route so 'starcat' isn't read as a study id."""
    return templates.TemplateResponse(request, "geneset_starcat.html", {
        "catalog_id": catalog_id,
        "module_name": module_name,
    })


@router.get("/facets", response_class=HTMLResponse)
async def facets_index(request: Request):
    """Top-level browser for the fleet's ``ant_*`` obs columns.

    One row per distinct facet name across all datasets. The heavy lifting
    happens server-side in ant-serve's ``/facets`` endpoint; this route
    just renders the table shell around the JSON payload.
    """
    api_url = _api_url(request)
    error: Optional[str] = None
    facets: List[Dict[str, Any]] = []
    definitions_status: Dict[str, Any] = {}
    try:
        payload = _api_get(api_url, "/facets")
        facets = payload.get("facets") or []
        definitions_status = payload.get("definitions_status") or {}
    except Exception as exc:
        error = f"Failed to load facets from ant-serve: {exc}"
    # facets_visible: the others are not shown for now (still in the data and
    # the API, flagged hidden_by_config). Admins get them as a list of links.
    for facet_row in facets:
        facet_row['scope_options'] = _facet_scope_options(facet_row.get('scopes'), None)
    hidden_facets = [f for f in facets if f.get("hidden_by_config")]
    facets = [f for f in facets if not f.get("hidden_by_config")]
    return templates.TemplateResponse(request, "facets.html", {
        "facets": facets,
        "n_facets": len(facets),
        "hidden_facets": hidden_facets,
        "is_admin": _is_admin(request),
        "definitions_status": definitions_status,
        "error": error,
    })


@router.get("/donor-groups", response_class=HTMLResponse)
async def donor_groups_view(request: Request):
    """Donor groups: datasets whose donors come from one donor collection,
    each marked primary data, reanalysis or unresolved (GET /donor-groups)."""
    api_url = _api_url(request)
    error: Optional[str] = None
    payload: Dict[str, Any] = {}
    try:
        payload = _api_get(api_url, "/donor-groups") or {}
    except Exception as exc:
        error = f"Failed to load donor groups from ant-serve: {exc}"
    groups = sorted(payload.get("groups") or [], key=lambda g: str(g.get("donor_group") or ""))
    return templates.TemplateResponse(request, "donor_groups.html", {
        "groups": groups,
        "n_ungrouped": payload.get("n_ungrouped"),
        "error": error,
    })


@router.get("/facets/{facet_name}", response_class=HTMLResponse)
async def facet_detail_view(request: Request, facet_name: str):
    """Per-facet drilldown page.

    Header stats, value distribution across all carriers, and the top
    variable genesets from ``module_variability.parquet`` filtered on
    ``tag_column == facet``. A deprecated facet name redirects to the current one.
    """
    current_name = canonical_facet(facet_name)
    if not current_name.startswith('ant_') and current_name in _FACET_TEST_LABELS:
        current_name = 'ant_' + current_name       # the bare name of a tested facet (braak) means its column
    if current_name != facet_name:
        return RedirectResponse(f"/facets/{url_quote(current_name, safe='')}", status_code=307)
    api_url = _api_url(request)
    error: Optional[str] = None
    facet_data: Dict[str, Any] = {}
    is_admin = _is_admin(request)
    visible = _facets_visible()
    if visible and facet_name not in visible and not is_admin:
        return _facet_hidden_page(request, facet_name, visible)
    requested_scope = request.query_params.get('scope') or ''
    if requested_scope not in _FACET_SCOPE_ORDER:
        requested_scope = ''
    try:
        # A cold /facets/{name} scans every carrying dataset (value counts
        # plus the per-dataset mapping); give it longer than the default.
        facet_data = _api_get_as_user(
            request, api_url,
            f"/facets/{url_quote(facet_name, safe='')}" + (f'?scope={requested_scope}' if requested_scope else ''),
            timeout=90)
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        # In tests the fake raises a bare HTTPError with no response attached;
        # treat that as 404 too so route logic exercises cleanly.
        if status == 404 or status is None:
            return templates.TemplateResponse(request, "facets.html", {
                "facets": [],
                "n_facets": 0,
                "error": f"Facet '{facet_name}' is not present in the catalog.",
            }, status_code=404)
        error = f"Failed to load facet '{facet_name}': {exc}"
    except Exception as exc:
        error = f"Failed to load facet '{facet_name}': {exc}"
    if facet_data.get("hidden_by_config") and not is_admin:
        return _facet_hidden_page(request, facet_name, visible)
    # The effects table is slow on a cold cache: the page renders first and
    # the table is fetched from /facets/{name}/effects-table.
    scope = facet_data.get('scope') or requested_scope or None
    scope_options = _facet_scope_options(facet_data.get('scopes'), scope)
    facet_effects_pending = (facet_name.startswith('ant_') and facet_name[4:] in _FACET_TEST_LABELS
                             and (scope is None or scope in _FACET_SCOPES_TESTED))
    variants = [{'name': 'ant_' + name, 'label': label, 'note': _FACET_VARIANT_NOTES.get(name, '')}
                for name, label in _FACET_VARIANTS.get(facet_name[4:], [])] if facet_name.startswith('ant_') else []
    non_carriers = facet_data.get("non_carriers") or []
    return templates.TemplateResponse(request, "facet.html", {
        "facet_scope": scope,
        "facet_scope_options": scope_options,
        "facet_variants": variants,
        "facet_dataset_scoped": (facet_data.get('definition') or {}).get('scope') == 'dataset',
        "facet_effects_pending": facet_effects_pending,
        "hidden_by_config": bool(facet_data.get("hidden_by_config")
                                 or (visible and facet_name not in visible)),
        "facet_name": facet_name,
        "facet": facet_data,
        "definition": facet_data.get("definition") or {},
        "definitions_status": facet_data.get("definitions_status") or {},
        "non_carriers": non_carriers,
        "n_non_carriers_hinted": sum(1 for d in non_carriers if d.get("candidate_columns")),
        "n_non_carriers_declared": sum(1 for d in non_carriers if d.get("declared")),
        "error": error,
    })


@router.get("/facets/{facet_name}/effects-table", response_class=HTMLResponse)
async def facet_effects_table(request: Request, facet_name: str):
    """HTML fragment: the per dataset and cell type effects table of one facet.

    Fetched by ``facet.html`` after the page has rendered, because the
    underlying ``/facets/{name}/tests`` call can take seconds on a cold cache.
    Same visibility rule as the facet page.
    """
    visible = _facets_visible()
    variant = request.query_params.get('variant') or facet_name
    scope = request.query_params.get('scope') or None
    if scope is not None and scope not in _FACET_SCOPE_ORDER:
        return HTMLResponse('<p class="fx-none">Unknown scope.</p>', status_code=400)
    variants = [f'ant_{name}' for name, _ in _FACET_VARIANTS.get(facet_name[4:], [])] or [facet_name]
    if (visible and facet_name not in visible and not _is_admin(request)) \
            or not (facet_name.startswith('ant_') and facet_name[4:] in _FACET_TEST_LABELS) \
            or variant not in variants:
        return HTMLResponse('<p class="fx-none">No effects are shown for this facet.</p>', status_code=404)
    effects = _facet_tests_across(_api_url(request), variant, scope)
    return templates.TemplateResponse(request, "_facet_effects_across.html", {
        "facet_effects": effects, "facet_scope": scope})


def _facet_hidden_page(request: Request, facet_name: str, visible: List[str]) -> HTMLResponse:
    """Short page for a facet outside facets_visible (non-admins)."""
    return templates.TemplateResponse(request, "facet.html", {
        "facet_name": facet_name,
        "facet": {},
        "hidden_notice": True,
        "visible_facets": visible,
        "definition": {},
        "definitions_status": {},
        "non_carriers": [],
        "n_non_carriers_hinted": 0,
        "n_non_carriers_declared": 0,
        "facet_effects_pending": False,
        "error": None,
    })


def _flatten_annotation_field(value: object) -> str:
    """Flatten a possibly-list annotation value to a comma-joined string.

    The REST API returns celltype/tissue as a string for single-valued datasets
    and as a list for multi-valued ones.  The JS filter calls .toLowerCase() on
    these values, which throws TypeError on arrays.  This helper ensures the
    embedded DS_ANNOT always contains plain strings.

    Args:
        value: A string, list of strings, or None/falsy.

    Returns:
        Comma-joined string, e.g. ``"microglial cell,macrophage"`` or ``""``.
    """
    if not value:
        return ''
    if isinstance(value, list):
        return ','.join(str(v) for v in value if v)
    return str(value)



# ── gene, geneset and facet views (new tabs next to the atlas / geneset / facet pages) ──

def _view_modality(db: Dict[str, Any]) -> str:
    """Nuclei or cells, for the modality tag of a dataset row in the view panels.

    The API's ``modality`` field wins when it is served. Otherwise it is derived from
    ``suspension_type``: ``nucleus`` is "nuclei", a xenograft or chimera of ``cell`` is
    "sorted live cells", any other ``cell`` is "cells". Unknown or ``na`` gives "".
    """
    served = str(db.get('modality') or '').strip().lower()
    if served:
        return served
    suspension = str(db.get('suspension_type') or '').strip().lower()
    if suspension == 'nucleus':
        return 'nuclei'
    if suspension == 'cell':
        system = str(db.get('experimental_system') or '').lower()
        return 'sorted live cells' if system in ('xenograft', 'chimera') else 'cells'
    return ''


def _view_celltype_column(db: Dict[str, Any]) -> str:
    """Name of the per-cell cell type column of a dataset, '' when none is recognisable.

    Prefers the standardised ``ant_cell_type``, else a categorical column called
    ``cell_type`` / ``celltype`` (any case). Used for the "plot by cell type" link of the
    gene view.
    """
    obs = db.get('obs')
    categorical = obs.get('categorical') if isinstance(obs, dict) else None
    names = [name for name in (categorical or []) if isinstance(name, str)]
    if 'ant_cell_type' in names:
        return 'ant_cell_type'
    for name in names:
        if re.fullmatch(r'cell[_ ]?type', name, re.IGNORECASE):
            return name
    return ''


# One place for the dataset categories shown as tags in the gene view: key -> label, tooltip,
# and whether the category has per-cell cell types that may be averaged into a cell type row.
VIEW_CATEGORIES: Dict[str, Dict[str, Any]] = {
    'post_mortem': {'label': 'post-mortem', 'typed': True,
                    'tip': 'Human post-mortem brain tissue.'},
    'xenograft': {'label': 'xenograft', 'typed': True,
                  'tip': 'Human cells transplanted into a mouse brain (human in mouse).'},
    'biopsy': {'label': 'biopsy', 'typed': True,
               'tip': 'Fresh or live surgical biopsy tissue.'},
    'spatial': {'label': 'spatial', 'typed': False,
                'tip': 'Spatial transcriptomics: spots or segmented cells in a tissue section, no per-cell cell type call comparable to dissociated cells.'},
    'in_vitro': {'label': 'in vitro', 'typed': False,
                 'tip': 'Cultured cells, iPSC derivatives or organoids, not tissue.'},
    'bulk': {'label': 'bulk', 'typed': False,
             'tip': 'Bulk RNA-seq or proteomics: no single cells.'},
    'unclassified': {'label': 'unclassified', 'typed': False,
                     'tip': 'The experimental system of this dataset is not recorded.'},
}
_SPATIAL_TECHNOLOGIES = ('visium', 'xenium', 'slide-tags', 'slide_tags', 'novast', 'cosmx',
                         'stereo', 'merfish', 'slide-seq', 'spatial')


def _view_category(db: Dict[str, Any]) -> str:
    """Key of ``VIEW_CATEGORIES`` for a dataset, from experimental_system, technology and facet_scope."""
    system = _flatten_annotation_field(db.get('experimental_system')).lower()
    technology = _flatten_annotation_field(db.get('technology')).lower()
    scope = str(db.get('facet_scope') or '').lower()
    if 'bulk' in system or 'bulk' in technology or 'mass-spec' in technology:
        return 'bulk'
    if any(name in technology for name in _SPATIAL_TECHNOLOGIES):
        return 'spatial'
    if system.startswith('in_vitro') or 'organoid' in system or scope == 'in_vitro':
        return 'in_vitro'
    if 'xenograft' in system or 'chimera' in system or scope == 'xenograft_human_in_mouse':
        return 'xenograft'
    if 'biopsy' in system:
        return 'biopsy'
    if 'post_mortem' in system or 'postmortem' in system or scope == 'human_postmortem':
        return 'post_mortem'
    return 'unclassified'


def _view_db_map(api_url: str) -> Dict[str, Dict[str, Any]]:
    """Compact per-dataset facts the view panels need, keyed by db_id.

    ``celltype_col`` the per-cell cell type column (may be empty), ``scope`` is the facet scope, ``group`` the cell type group of the dataset-level
    cell type (empty when the dataset is mixed or has several cell types), ``human``
    whether the organism is human, ``secondary`` the reanalysis flag and ``modality``
    nuclei versus cells (``_view_modality``), ``category`` the dataset category (``_view_category``). Empty when the API is unreachable.
    """
    result: Dict[str, Dict[str, Any]] = {}
    try:
        databases = _all_databases(api_url)
    except Exception as error:
        print(f"API error: {error}")
        return result
    for db in databases:
        db_id = db.get('id') or ''
        if not db_id:
            continue
        celltype = db.get('celltype')
        single = celltype if isinstance(celltype, str) else (celltype[0] if isinstance(celltype, list) and len(celltype) == 1 else None)
        group = celltype_groups.group_of(single) if single else ''
        if group == celltype_groups.UNMAPPED:
            group = ''
        result[db_id] = {
            'title': db.get('title') or db.get('basename') or db_id,
            'celltype_col': _view_celltype_column(db),
            'scope': db.get('facet_scope') or '',
            'group': group,
            'human': 'human' in _flatten_annotation_field(db.get('organism')).lower(),
            'secondary': bool(db.get('is_secondary')),
            'modality': _view_modality(db),
            'category': _view_category(db),
        }
    return result


_AUTHOR_DE_TTL = 3600.0
_author_de_state: Dict[str, Any] = {'at': 0.0, 'studies': {}}
_author_de_lock = threading.Lock()


def _author_de_studies(api_url: str) -> Dict[str, List[Dict[str, Any]]]:
    """db_id -> DE studies, for every dataset that has author DE (cached for an hour).

    One ``/databases/{id}/de`` call per dataset on the first request, in a small thread
    pool; later requests reuse the answer, so a gene page never repeats the scan.
    """
    from concurrent.futures import ThreadPoolExecutor
    with _author_de_lock:
        if _author_de_state['studies'] and time.time() - _author_de_state['at'] < _AUTHOR_DE_TTL:
            return _author_de_state['studies']
        try:
            db_ids = [db['id'] for db in _all_databases(api_url) if db.get('id')]
        except Exception as error:
            print(f"API error: {error}")
            return {}

        def studies_of(db_id: str) -> Tuple[str, List[Dict[str, Any]]]:
            try:
                path = '/databases/' + '/'.join(url_quote(part, safe='') for part in db_id.split('/')) + '/de'
                return db_id, _api_get(api_url, path, timeout=20).get('studies') or []
            except Exception:
                return db_id, []
        with ThreadPoolExecutor(max_workers=8) as pool:
            found = {db_id: studies for db_id, studies in pool.map(studies_of, db_ids) if studies}
        _author_de_state['studies'] = found
        _author_de_state['at'] = time.time()
        return found


def _plain_de_label(row: Dict[str, Any], study: Dict[str, Any]) -> str:
    """A reader's label for one author DE row: what was compared, in plain words."""
    term = str(row.get('term') or '').replace('_', ' ').strip()
    contrast = str(row.get('contrast') or '').strip()
    reference = study.get('reference_type')
    if ' - ' in contrast:
        left, right = contrast.split(' - ', 1)
        return f"{left.strip()} versus {right.strip()} ({term})"
    if reference == 'vsgrandmean':
        return f"{contrast} versus the average of all levels ({term})"
    if reference == 'vsreference':
        return f"{contrast.replace('_vs_', ' versus ')} ({term})"
    return f"{term}: {contrast}" if contrast else term


def _gene_author_de_rows(api_url: str, gene: str) -> List[Dict[str, Any]]:
    """Author DE rows of one gene across every dataset that has DE, best adjusted p first."""
    from concurrent.futures import ThreadPoolExecutor
    studies_by_db = _author_de_studies(api_url)
    titles = {}
    try:
        for db in _all_databases(api_url):
            titles[db.get('id')] = db
    except Exception:
        pass

    def rows_of(db_id: str) -> List[Dict[str, Any]]:
        try:
            path = '/databases/' + '/'.join(url_quote(part, safe='') for part in db_id.split('/')) + '/de/by-gene/' + url_quote(gene, safe='')
            payload = _api_get(api_url, path, timeout=30)
        except Exception:
            return []
        by_id = {study.get('study_id'): study for study in studies_by_db.get(db_id, [])}
        db = titles.get(db_id) or {}
        out = []
        for row in payload.get('rows') or []:
            if row.get('term') == 'intercept' or (row.get('padj') is None and row.get('pvalue') is None):
                continue
            study = by_id.get(row.get('study_id')) or {}
            out.append({
                'db_id': db_id, 'title': db.get('title') or db_id, 'collection': db.get('group') or db.get('group_id') or '',
                'study': row.get('study_name') or row.get('study_id'), 'method': row.get('method') or study.get('method') or '',
                'term': row.get('term') or '', 'contrast': row.get('contrast') or '', 'label': _plain_de_label(row, study),
                'log2fc': row.get('log2fc'), 'padj': row.get('padj'), 'pvalue': row.get('pvalue'),
            })
        return out
    with ThreadPoolExecutor(max_workers=6) as pool:
        collected = [row for rows in pool.map(rows_of, list(studies_by_db)) for row in rows]
    collected.sort(key=lambda row: (row['padj'] if row['padj'] is not None else 2.0))
    return collected[:200]


# ── drilldown facet derivation ─────────────────────────────────────────────
# The old "system" facet conflated assay type (bulk, spatial, sc) with
# biosample preparation (xenograft, post-mortem, fresh brain). Split into two
# clean axes for the module-view and gene-atlas drilldowns.

_SPATIAL_TECH_TOKENS = frozenset({
    'visium', 'xenium', 'merfish', 'cosmx', 'stereo', 'novast',
    'slide-seq', 'slide-tags', 'slide_tags', 'slide_seq',
})

_BULK_TECH_TOKENS = frozenset({
    'bulk', 'bulk_rna', 'bulk-rna',
})


def _derive_technology(db: Dict[str, Any]) -> str:
    """Derive a coarse technology category for drilldown faceting.

    Priority order:
    1. ``suspension_type`` = nucleus → "single nucleus"
    2. ``suspension_type`` = cell   → "single cell" (unless overridden below)
    3. Technology string contains a spatial token → "spatial"
    4. Technology string contains a bulk token → "bulk"
    5. Fallback → "single cell" (safe default for 10x etc.)

    ``chimera`` and ``xenograft`` entries carry ``suspension_type=cell`` and
    10x technology, so they correctly land in "single cell".

    Args:
        db: A single database-info dict from the REST API.

    Returns:
        One of: ``"single cell"``, ``"single nucleus"``, ``"spatial"``,
        ``"bulk"``, or ``""`` (empty string when technology is absent/unknown).
    """
    tech_raw = _flatten_annotation_field(db.get('technology')).lower()
    susp = str(db.get('suspension_type') or '').strip().lower()

    # Bulk and spatial override suspension_type because suspension_type='na'
    # for both; check them first.
    for token in _SPATIAL_TECH_TOKENS:
        if token in tech_raw:
            return 'spatial'
    for token in _BULK_TECH_TOKENS:
        if token in tech_raw:
            return 'bulk'
    if 'mass-spectrometry' in tech_raw or 'mass_spectrometry' in tech_raw:
        return 'bulk'

    # Single-cell vs single-nucleus is determined by suspension_type.
    if susp == 'nucleus':
        return 'single nucleus'
    if susp == 'cell':
        return 'single cell'
    if susp == 'na':
        # Fallback: bulk or spatial already handled above; na alone with no
        # recognisable spatial/bulk token — treat as bulk (conservative).
        return 'bulk'
    # No useful data.
    if not tech_raw:
        return ''
    return 'single cell'


# Biosample display-name map.  Raw mus vocab values → human-readable labels.
# ``chimera`` is a xenograft variant (developmental iPSC-in-mouse host);
# collapse to "xenograft" in the display layer; raw mus value is unchanged.
_BIOSAMPLE_MAP: Dict[str, str] = {
    'xenograft':         'xenograft',
    'chimera':           'xenograft',  # chimera = developmental xenograft
    'post_mortem_tissue': 'post-mortem',
    'post-mortem':       'post-mortem',
    'post_mortem':       'post-mortem',
    'live_biopsy':       'fresh',
    'fresh':             'fresh',
    'in_vitro_2d':       'in-vitro',
    'in_vitro':          'in-vitro',
    'organoid':          'in-vitro',
    'bulk_tissue':       '',  # biosample undefined for pure bulk — omit
}


def _derive_biosample(db: Dict[str, Any]) -> str:
    """Derive a display-level biosample category from experimental_system.

    Maps the mus ``experimental_system`` vocabulary to a clean set of
    display labels.  ``chimera`` is collapsed to ``xenograft`` because both
    represent iPSC/ESC-derived cells hosted in a non-native brain.
    ``bulk_tissue`` is left empty — the biosample axis is not meaningful for
    bulk RNA-seq that has no single-cell resolution.

    Args:
        db: A single database-info dict from the REST API.

    Returns:
        One of: ``"xenograft"``, ``"post-mortem"``, ``"fresh"``,
        ``"in-vitro"``, or ``""`` (empty for bulk or unmapped values).
    """
    raw = str(db.get('experimental_system') or '').strip().lower().replace('-', '_').replace(' ', '_')
    return _BIOSAMPLE_MAP.get(raw, raw.replace('_', '-') if raw else '')


def _md_first_paragraph(markdown_body: str, max_chars: int) -> str:
    """Extract the first non-heading paragraph from a markdown body.

    Skips leading ``# Title`` lines and returns the first non-blank paragraph
    as plain text (strip markdown emphasis), truncated to ``max_chars``.

    Args:
        markdown_body: Raw markdown string (may be empty or None-cast).
        max_chars: Maximum character count before an ellipsis is appended.

    Returns:
        Plain-text first paragraph or empty string.
    """
    if not markdown_body:
        return ''
    lines = markdown_body.split('\n')
    # Skip leading heading lines and blanks.
    para_lines: list = []
    in_para = False
    for line in lines:
        stripped = line.strip()
        if not in_para:
            if not stripped or stripped.startswith('#'):
                continue
            in_para = True
        else:
            if not stripped:
                break  # end of first paragraph
        # Strip markdown emphasis (*bold*, _italic_) — naive but good enough.
        cleaned = re.sub(r'[*_`]', '', stripped)
        para_lines.append(cleaned)
    text = ' '.join(para_lines).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + '…'
    return text


def _build_dataset_annotations(api_url: str) -> Dict[str, Dict[str, str]]:
    """Return a per-db_id annotation map for client-side facet filtering and popovers.

    Keys returned per entry:

    - ``celltype``, ``tissue``: comma-joined strings for facet filters.
    - ``technology``: derived coarse tech category — "single cell", "single
      nucleus", "spatial", or "bulk".  Derived from ``suspension_type`` +
      ``technology`` via :func:`_derive_technology`.
    - ``biosample``: derived biosample category — "xenograft", "post-mortem",
      "fresh", or "in-vitro".  Derived from ``experimental_system`` via
      :func:`_derive_biosample`.  Empty for bulk datasets.
    - ``system``: **deprecated**; kept for one release as the biosample value
      (callers that read ``system`` still work, but prefer ``biosample``).
    - ``title``: dataset display title.
    - ``n_cells``: cell count as a string (empty when unknown).
    - ``contributor``: contributor name string.
    - ``collection_name``: display name of the parent collection.
    - ``collection_desc``: first paragraph of the collection's markdown body
      (plain text, up to 200 chars).
    - ``dataset_desc``: first paragraph of the dataset's description field
      (plain text, up to 300 chars).

    All values are plain strings so JS ``.toLowerCase()`` is safe. Multi-valued
    fields are comma-joined (e.g. ``"microglial cell,macrophage"``).

    Args:
        api_url: Base URL for the ant-serve REST API.

    Returns:
        Dict mapping db_id to annotation + display-info strings.
    """
    try:
        databases = _all_databases(api_url)
    except Exception:
        return {}
    result: Dict[str, Dict[str, str]] = {}
    for db in databases:
        db_id = db.get('id') or ''
        if not db_id:
            continue
        n_obs = db.get('n_obs') or db.get('n_cells') or ''
        collection_meta = db.get('collection') or {}
        collection_name = str(collection_meta.get('group') or '').strip()
        coll_desc_raw = str(collection_meta.get('description') or '').strip()
        collection_desc = _md_first_paragraph(coll_desc_raw, 200)
        ds_desc_raw = str(db.get('description') or db.get('summary') or '').strip()
        dataset_desc = _md_first_paragraph(ds_desc_raw, 300)
        technology = _derive_technology(db)
        biosample = _derive_biosample(db)
        result[db_id] = {
            'celltype': _flatten_annotation_field(db.get('celltype')),
            'tissue': _flatten_annotation_field(db.get('tissue')),
            'technology': technology,
            'biosample': biosample,
            'system': biosample,  # deprecated alias — remove in a future release
            'title': str(db.get('title') or ''),
            'n_cells': str(n_obs) if n_obs else '',
            'contributor': str(db.get('contributor') or ''),
            'collection_name': collection_name,
            'collection_desc': collection_desc,
            'dataset_desc': dataset_desc,
            # secondary marker — the client renders a `(2)` badge next to the
            # dataset id when true. Boolean so JS can `if (ann.is_secondary)`
            # without truthiness surprises on empty strings.
            'is_secondary': bool(db.get('is_secondary')),
        }
    return result


def _preselect_cell_types(metadata: Dict[str, Any]) -> List[str]:
    """Return the cell-type preselection list from experiment metadata.

    Reads ``celltypes`` from the experiment's index.md frontmatter (ant-serve
    passes it through as ``metadata.celltypes``). Returns the list as-is when
    non-empty; empty list means no preselection. The JS treats a single-entry
    list the same as a scalar preselect, and OR-selects multiple entries.
    """
    celltypes = metadata.get('celltypes') or []
    return [ct for ct in celltypes if ct] if isinstance(celltypes, list) else []


@router.get("/genesets/{study}/{experiment}", response_class=HTMLResponse)
async def geneset_experiment_view(request: Request, study: str, experiment: str):
    api_url = _api_url(request)
    try:
        payload = _api_get(api_url, f'/genesets/{study}/{experiment}')
    except Exception as error:
        print(f"API error: {error}")
        return templates.TemplateResponse(
            request, "genesets.html",
            {"by_study": {}, "error": f"Experiment {study}/{experiment} not found"},
            status_code=404,
        )

    body_html = markdown.markdown(payload.get('body') or '', extensions=['tables'])
    meta = payload.get('metadata', {})
    catalog = f"{study}/{experiment}"
    dataset_annotations = _build_dataset_annotations(api_url)

    return templates.TemplateResponse(request, "geneset_experiment.html", {
        "study": study,
        "experiment": experiment,
        "metadata": meta,
        "body_html": body_html,
        "genesets": payload.get('genesets', []),
        "dataset_annotations": dataset_annotations,
        "mv_ctx": {
            "scope": "module",
            "module_catalog": catalog,
            "module_name": None,
            "id": "mv-experiment",
            "dataset_annotations": dataset_annotations,
            "tissue_ancestors": _TISSUE_ANCESTORS,
            "preselect_cell_types": _preselect_cell_types(meta),
        },
    })


def _geneset_provenance(api_url: str, payload: Dict[str, Any], study: str, experiment: str) -> Dict[str, Any]:
    """Where a gene set comes from, for the header of its page.

    Args:
        api_url: Base URL of the REST API.
        payload: The ``/genesets/{study}/{experiment}/{name}`` answer.
        study: Study folder of the gene set.
        experiment: Experiment (catalog) folder of the gene set.

    Returns:
        ``{items, papers, description}``: ``items`` are ``(label, text, href)`` rows (href may be empty) for
        experiment, method, organism, source dataset and who derived it; ``papers`` the source-paper cards of the
        source dataset (empty when the library is off); ``description`` the experiment description.
    """
    experiment_meta = payload.get('experiment_metadata') or {}
    geneset_meta = payload.get('geneset_metadata') or {}
    parent = payload.get('parent_dataset') or experiment_meta.get('parent_dataset') or ''
    items: List[Tuple[str, str, str]] = []
    experiment_name = experiment_meta.get('name') or f'{study}/{experiment}'
    items.append(('Experiment', experiment_name, f'/genesets/{study}/{experiment}'))
    method = payload.get('method') or experiment_meta.get('method')
    software = experiment_meta.get('software')
    if method:
        items.append(('Method', f'{method}' + (f' ({software})' if software and software != method else ''), ''))
    if payload.get('organism'):
        items.append(('Organism', str(payload['organism']), ''))
    if parent:
        parts = str(parent).split('/')
        items.append(('Source dataset', str(parent), f'/browse/{parts[0]}/{parts[1]}' if len(parts) == 2 else ''))
    derived = experiment_meta.get('derived_by') or geneset_meta.get('derived_by')
    if derived:
        items.append(('Derived by', str(derived), ''))
    papers = [card for card in _dataset_papers(api_url, str(parent)) if card.get('role') == 'source'][:2] if parent else []
    return {'items': items, 'papers': papers, 'description': (experiment_meta.get('description') or '').strip()}


@router.get("/genesets/{study}/{experiment}/{name}", response_class=HTMLResponse)
async def geneset_view(request: Request, study: str, experiment: str, name: str):
    api_url = _api_url(request)
    try:
        payload = _api_get(api_url, f'/genesets/{study}/{experiment}/{name}')
    except Exception as error:
        print(f"API error: {error}")
        return templates.TemplateResponse(
            request, "genesets.html",
            {"by_study": {}, "error": f"Geneset {study}/{experiment}/{name} not found"},
            status_code=404,
        )

    geneset_body_html = markdown.markdown(payload.get('geneset_body') or '', extensions=['tables'])
    experiment_body_html = markdown.markdown(payload.get('experiment_body') or '', extensions=['tables'])

    catalog = f"{study}/{experiment}"
    dataset_annotations = _build_dataset_annotations(api_url)
    # Prefer experiment-level celltypes (from experiment_metadata); fall back to
    # the top-level geneset metadata if the server merges them differently.
    exp_meta = payload.get('experiment_metadata') or payload.get('metadata') or {}
    preselect_cts = _preselect_cell_types(exp_meta)

    return templates.TemplateResponse(request, "geneset.html", {
        "study": study,
        "experiment": experiment,
        "name": name,
        "payload": payload,
        "geneset_body_html": geneset_body_html,
        "experiment_body_html": experiment_body_html,
        # Exposed to the outer geneset.html scope so parent_dataset / scored_in
        # chips can render a `(2)` badge for secondary datasets.
        "dataset_annotations": dataset_annotations,
        "view_dbs": _view_db_map(api_url),
        "view_payload": {k: payload.get(k) for k in ('scored_in', 'column_name', 'direction', 'n_genes', 'quality_sensitive', 'genes', 'ranks', 'format')},
        "provenance": _geneset_provenance(api_url, payload, study, experiment),
        "mv_ctx": {
            "scope": "module",
            "module_catalog": catalog,
            "module_name": name,
            "id": "mv-varies-most",
            "dataset_annotations": dataset_annotations,
            "tissue_ancestors": _TISSUE_ANCESTORS,
            "preselect_cell_types": preselect_cts,
        },
    })


@router.get("/genesets/{study}", response_class=HTMLResponse)
async def geneset_study_view(request: Request, study: str):
    """Per-study drilldown of experiments and their genesets.

    Reached from a row click on the ``/genesets`` landing table. Renders a
    table of experiments; each row expands into the genesets that live
    under it. Registered AFTER every other ``/genesets/…`` route because
    the single-segment pattern would otherwise swallow ``variability``
    and ``starcat`` deep-links.
    """
    api_url = _api_url(request)
    try:
        payload = _api_get(api_url, '/genesets') or {}
        all_genesets = payload.get('genesets') or []
    except Exception as error:
        print(f"API error: {error}")
        all_genesets = []

    study_genesets = [g for g in all_genesets if g.get('study') == study]
    if not study_genesets:
        return templates.TemplateResponse(
            request, "genesets.html",
            {
                "studies": [],
                "groups": [],
                "active_group": "genesets",
                "error": f"Study {study} not found",
            },
            status_code=404,
        )

    # Group into experiments, fetch experiment-level metadata once each so
    # organism / celltypes / method / description are available.
    experiments: List[Dict[str, Any]] = []
    seen_experiments: Dict[str, List[Dict[str, Any]]] = {}
    for geneset in study_genesets:
        exp_name = geneset.get('experiment') or ''
        seen_experiments.setdefault(exp_name, []).append(geneset)

    study_provenance = {
        'doi': '',
        'pmid': '',
        'journal': '',
        'title': '',
        'year': None,
        'authors': [],
        'body': '',
    }
    # Seed provenance from the study-level index.md via /genesets/studies.
    # Falls through to experiment.annotation.doi below when the study has no
    # index.md of its own (e.g. LuChen2025, Mancuso2024, Sierksma2025).
    try:
        studies_payload = _api_get(api_url, '/genesets/studies') or {}
        for row in studies_payload.get('studies') or []:
            if row.get('study') == study:
                study_provenance['title'] = row.get('title') or ''
                study_provenance['doi'] = row.get('doi') or ''
                study_provenance['pmid'] = row.get('pmid') or ''
                study_provenance['journal'] = row.get('journal') or ''
                study_provenance['year'] = row.get('year')
                study_provenance['authors'] = row.get('authors') or []
                break
    except Exception as error:
        print(f"API error (/genesets/studies): {error}")

    for exp_name in sorted(seen_experiments):
        meta: Dict[str, Any] = {}
        body_html = ''
        try:
            exp_payload = _api_get(api_url, f'/genesets/{study}/{exp_name}') or {}
            meta = exp_payload.get('metadata') or {}
            body_html = markdown.markdown(
                exp_payload.get('body') or '', extensions=['tables']
            )
        except Exception as error:
            print(f"API error (/genesets/{study}/{exp_name}): {error}")
        # Study-level provenance: first non-empty wins. Every experiment
        # under a study points back at the same paper.
        annotation = meta.get('annotation') or {}
        for key in ('doi', 'pmid', 'journal', 'title'):
            if not study_provenance[key]:
                value = annotation.get(key) or meta.get(key)
                if value:
                    study_provenance[key] = str(value)
        if study_provenance['year'] is None:
            year_raw = meta.get('year') or annotation.get('year')
            if year_raw:
                try:
                    study_provenance['year'] = int(str(year_raw)[:4])
                except ValueError:
                    pass
        if not study_provenance['authors']:
            authors = meta.get('authors') or annotation.get('authors')
            if isinstance(authors, list) and authors:
                study_provenance['authors'] = [str(a) for a in authors]
        experiments.append({
            'name': exp_name,
            'display_name': meta.get('name') or exp_name,
            'description': meta.get('description') or '',
            'organism': meta.get('organism') or '',
            'celltypes': meta.get('celltypes') or [],
            'method': meta.get('method') or '',
            'parent_dataset': meta.get('parent_dataset'),
            'body_html': body_html,
            'genesets': seen_experiments[exp_name],
            'n_genesets': len(seen_experiments[exp_name]),
        })

    # Aggregate distinct organisms across experiments for a study-level pill
    # cluster in the header. Preserve first-seen order (experiments are already
    # alphabetised above) and pair each with its Hs/Mm abbrev for the pill.
    organisms: List[Dict[str, str]] = []
    seen_orgs: set = set()
    for exp in experiments:
        raw = (exp.get('organism') or '').strip()
        if not raw:
            continue
        key = raw.lower()
        if key in seen_orgs:
            continue
        seen_orgs.add(key)
        organisms.append({
            'full': raw,
            'abbrev': _ORGANISM_ABBREV.get(key, raw[:2].capitalize()),
        })

    dataset_annotations = _build_dataset_annotations(api_url)
    return templates.TemplateResponse(request, "geneset_study.html", {
        "study": study,
        "experiments": experiments,
        "organisms": organisms,
        "provenance": study_provenance,
        "dataset_annotations": dataset_annotations,
    })


# ── Paper library: cards, link reasons, paper page, library page ────────────
#
# Everything comes from ant-serve's library endpoints (2.39+). There are no
# per-paper access rules (2.41): everyone who can sign in sees the whole
# library. The web side only renders what it gets and stays quiet (no
# section) when the library is absent or the call fails.

_PAPER_ROLE_ORDER = {'source': 0, 'reanalysis': 1, 'method': 2, 'related': 3, 'background': 4}
_PAPER_ROLE_BADGE = {'source': 'Source', 'reanalysis': 'Reanalysis', 'method': 'Method',
                     'related': 'Related', 'background': 'Background'}
# What each role means (same wording as GET /papers/upload/requirements).
_PAPER_ROLE_HELP = {
    'source': 'the data in anthive come from this paper',
    'reanalysis': 'the paper reanalyses data that are in anthive',
    'method': 'a method used on anthive data',
    'related': 'related to a dataset in anthive, not its source',
    'background': 'of interest to the lab, not linked to any dataset (the default)',
}
_PREPRINT_TYPES = {'online', 'unpublished', 'preprint', 'misc'}


def _paper_reason(role: str, entrytype: str = '', scope: str = 'dataset') -> str:
    """Plain-language sentence saying why a paper is linked to a dataset or
    collection."""
    what = {'collection': 'this collection', 'datasets': 'these datasets'}.get(scope, 'this dataset')
    role = (role or '').lower()
    if role == 'source':
        return f"Source of {what}: the data come from this paper."
    if role == 'reanalysis':
        return f"Reanalysis: this paper analyses the data of {what} again."
    if role == 'method':
        return f"Method used: a protocol or tool used to produce or process {what}."
    if role == 'related':
        if (entrytype or '').lower() in _PREPRINT_TYPES:
            return "Related: a preprint of a closely related study, not the source of these data."
        return "Related: a closely related study, not the source of these data."
    if role == 'background':
        return "Background reading on the topic of these data."
    return f"Linked to {what}."


def _surname(author: str) -> str:
    author = (author or '').strip()
    if ',' in author:
        return author.split(',', 1)[0].strip()
    return author.split()[-1] if author else ''


def _paper_citation_line(paper: Dict) -> str:
    """'Lee et al., Nature Genetics, 2026' from a library paper record."""
    authors = [a for a in _as_list(paper.get('authors')) if a]
    first = _surname(paper.get('first_author') or (authors[0] if authors else ''))
    if len(authors) == 2:
        who = f"{first} and {_surname(authors[1])}"
    elif len(authors) > 2:
        who = f"{first} et al."
    else:
        who = first
    parts = [p for p in (who, paper.get('journal') or '', str(paper.get('year') or '')) if p]
    return ', '.join(parts) or paper.get('citation') or paper.get('citekey') or ''


def _paper_card(paper: Dict, scope: str = 'dataset') -> Dict:
    """Template-ready view of one paper linked to a dataset or collection."""
    citekey = paper.get('citekey') or ''
    role = (paper.get('link_role') or paper.get('role') or '').lower()
    drafts = set(_as_list(paper.get('draft_fields')))
    doi = (paper.get('doi') or '').strip()
    pmid = str(paper.get('pmid') or '').strip()
    key = url_quote(citekey, safe='')
    return {
        'citekey': citekey,
        'title': paper.get('title') or citekey,
        'citation_line': _paper_citation_line(paper),
        'url': f"/papers/{key}",
        'role': role,
        'badge': _PAPER_ROLE_BADGE.get(role, role.capitalize() or 'Linked'),
        'reason': _paper_reason(role, paper.get('entrytype') or '', scope),
        'doi': doi,
        'doi_url': f"https://doi.org/{doi}" if doi else '',
        'pmid': pmid,
        'pmid_url': f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else '',
        'pdf_url': f"/api/papers/{key}/file" if paper.get('has_pdf') else '',
        'bib_url': f"/api/papers/{key}.bib",
        'summary': (paper.get('summary') or '').strip(),
        'summary_draft': 'anthive_summary' in drafts,
        'year': paper.get('year'),
    }


def _year_int(card: Dict) -> int:
    year = str(card.get('year') or '')
    return int(year) if year.isdigit() else 0


def _sort_paper_cards(cards: List[Dict]) -> List[Dict]:
    return sorted(cards, key=lambda c: (_PAPER_ROLE_ORDER.get(c['role'], 9), -_year_int(c), c['citekey']))


# ── Facet effects (donor-level facet tests, API /facet-tests) ─────────────────
#
# Rendered in two places from one partial (_facet_effects.html): inside the
# dataset page's Facets card (per facet, the strata of this dataset) and on
# /facets/{name} ("Effects across datasets", one row per dataset x stratum).
# These helpers only turn API payloads into display fields.

# Tested facets whose column is not 'ant_<name>': the sex of the grafted cells' donor line is ant_sex.
_TEST_FACET_COLUMN = {'graft_sex': 'ant_sex'}

_FACET_TEST_LABELS = {
    'sex': 'Sex',
    'graft_sex': 'Sex of grafted cells (donor line)',
    'host_sex': 'Sex of host mouse',
    'disease': 'Disease',
    'apoe4_carrier': 'APOE e4 carrier',
    'apoe4_dose': 'APOE e4 dose',
    'braak': 'Braak stage',
    'braak_threshold': 'Braak stage III or higher',
    'age': 'Age, not adjusted for pathology',
    'age_adjusted': 'Age, adjusted for pathology',
    'app_nlgf': 'App NL-G-F host',
}

# Tested variants of one facet column: the page of ``ant_<first>`` shows all of them
# (the later ones are sensitivity or twin analyses with no column of their own).
_FACET_VARIANTS = {
    'sex': [('sex', 'donor, post-mortem tissue'), ('graft_sex', 'donor line of grafted cells, xenografts')],
    'age': [('age', 'not adjusted for pathology'), ('age_adjusted', 'adjusted for pathology')],
    'braak': [('braak', 'trend per stage'), ('braak_threshold', 'stage III or higher vs lower')],
    'apoe4_carrier': [('apoe4_carrier', 'carrier vs non-carrier'), ('apoe4_dose', 'per e4 allele')],
}

_FACET_VARIANT_NOTES = {
    'sex': 'Sex of the donor in post-mortem tissue: male against female donors.',
    'graft_sex': ('Xenografts: sex of the donor line of the grafted human cells (ant_sex), one value per mouse. Not the sex of the '
                  'host mouse (ant_host_sex). Refused where a mouse carries several lines (villages).'),
    'age': ('Age, not adjusted for pathology: the straight-line trend per decade of age at death, in donors of 60 years and older. '
            'Age and AD pathology go together in every brain bank, so this result partly re-detects pathology; read it next to the adjusted twin.'),
    'age_adjusted': ('Age, adjusted for pathology: the same trend with Braak stage (else pathologic AD) in the model. '
                     'The gene rows say whether an age effect persists after that adjustment.'),
    'braak': 'Braak stage: the straight-line trend per stage (0 to VI), adjusted for sex and, where known, age and post-mortem interval.',
    'braak_threshold': 'Sensitivity analysis of the Braak trend: Braak stage III or higher against II or lower.',
    'apoe4_carrier': 'APOE e4 carrier: any e4 allele against none (e2 carriers pooled with e3/e3).',
    'apoe4_dose': 'Sensitivity analysis of the carrier contrast: the change per e4 allele (0, 1 or 2 alleles).',
}

# Facets tested on a numeric axis (trend per unit) rather than between two groups.
_NUMERIC_TEST_FACETS = frozenset({'braak', 'age', 'age_adjusted', 'apoe4_dose'})

# What a test variant without a column of its own was derived from (dataset Facets tab).
_FACET_DERIVED_NOTES = {
    'ant_age_adjusted': 'the age test with a pathology term in the model (Braak stage, else pathologic AD)',
    'ant_braak_threshold': 'Braak stage III or higher against II or lower, from ant_braak',
    'ant_apoe4_dose': 'the number of e4 alleles (0, 1, 2), from ant_apoe',
    'ant_apoe4_carrier': 'any e4 allele in ant_apoe',
}

_FACET_SCOPE_LABELS = {
    'human_postmortem': 'human post-mortem',
    'xenograft_human_in_mouse': 'human cells in mouse hosts (xenograft)',
    'in_vitro': 'in vitro cultures',
}
_FACET_SCOPE_ORDER = ('human_postmortem', 'xenograft_human_in_mouse', 'in_vitro')
_FACET_SCOPES_TESTED = ('human_postmortem', 'xenograft_human_in_mouse')

_CONTROL_BASIS_TEXT = {
    'humanised_app_host': 'humanised-App host (App hu/hu)',
    'wild_type_host': 'wild-type host',
    'wild_type': 'wild-type host',
}

# p-value floors by replicate unit: (replicates per group, replicates of a trend); lib/facet_rules.py
_P_VALUE_FLOORS = {'donor': (5, 10), 'animal': (3, 6), 'sample': (3, 6)}


def _facet_scope_label(scope: Optional[str]) -> str:
    """Plain name of a facet scope; unknown names are shown with underscores replaced."""
    return _FACET_SCOPE_LABELS.get(str(scope or ''), str(scope or '').replace('_', ' '))


def _facet_test_label(column_or_name: Optional[str]) -> str:
    """Plain label of a tested facet given as ``ant_age`` or ``age``."""
    name = str(column_or_name or '')
    name = name[4:] if name.startswith('ant_') else name
    return _FACET_TEST_LABELS.get(name, name.replace('_', ' '))


def _facet_scope_options(scopes: Any, current: Optional[str]) -> List[Dict[str, Any]]:
    """Scope selector entries from the ``scopes`` list of ``GET /facets/{name}``.

    Entries are ``{scope, label, n_datasets, n_cells, current, tested}`` in display order; the
    pseudo scope ``none`` (datasets outside every scope) is dropped.
    """
    by_scope: Dict[str, Dict[str, Any]] = {}
    for item in scopes or []:
        row = item if isinstance(item, dict) else {'scope': item}
        name = str(row.get('scope') or '')
        if name and name != 'none':
            by_scope[name] = row
    ordered = [name for name in _FACET_SCOPE_ORDER if name in by_scope] + \
        sorted(name for name in by_scope if name not in _FACET_SCOPE_ORDER)
    return [{'scope': name, 'label': _facet_scope_label(name), 'n_datasets': by_scope[name].get('n_datasets'),
             'n_cells': by_scope[name].get('n_cells'), 'current': name == current,
             'tested': name in _FACET_SCOPES_TESTED} for name in ordered]


templates.env.globals['facet_scope_label'] = _facet_scope_label
templates.env.globals['facet_test_label'] = _facet_test_label
templates.env.globals['facet_derived_note'] = lambda column: _FACET_DERIVED_NOTES.get(str(column), 'see the model details')
templates.env.globals['facet_scope_order'] = _FACET_SCOPE_ORDER
templates.env.globals['facet_control_basis'] = lambda value: _CONTROL_BASIS_TEXT.get(str(value), str(value).replace('_', ' '))


_METHOD_LABELS = {
    ('cell_mean', 'geneset'): 'cell mean, scanpy score',
    ('cell_mean', 'ucell'): 'cell mean, UCell',
    ('pseudobulk', 'gene_list'): 'pseudobulk',
}


def _facet_contrast_label(unit: Dict[str, Any]) -> str:
    """Plain contrast wording: the test level first, the reference second."""
    facet = unit.get('facet')
    if facet == 'braak':
        return 'trend per Braak stage'
    if facet == 'braak_threshold':
        return 'Braak stage III or higher vs II or lower'
    if facet in ('age', 'age_adjusted'):
        return 'trend per decade of age' + (', adjusted for pathology' if facet == 'age_adjusted'
                                            else ', not adjusted for pathology')
    if facet == 'apoe4_dose':
        return 'trend per APOE e4 allele'
    if facet == 'app_nlgf':
        return 'App NL-G-F vs control'
    test, ref = unit.get('level_test'), unit.get('level_ref')
    if test and ref:
        return f'{test} vs {ref}'
    return unit.get('contrast') or ''


def _replicate_noun(unit: Dict[str, Any], plural: bool = True) -> str:
    """'donors', 'animals' or 'samples' for the replicate unit of a unit (donors when unknown)."""
    noun = {'animal': 'animal', 'sample': 'sample'}.get(str(unit.get('replicate_unit') or ''), 'donor')
    return noun + ('s' if plural else '')


def _fx_donors_text(unit: Dict[str, Any]) -> str:
    counts = unit.get('level_counts') or {}
    noun = _replicate_noun(unit)
    facet = unit.get('facet')
    if facet in _NUMERIC_TEST_FACETS:
        n_donors = unit.get('n_donors') or counts.get('n_donors') or 0
        low = counts.get('braak_min', counts.get('value_min'))
        high = counts.get('braak_max', counts.get('value_max'))
        what = {'braak': 'stages', 'apoe4_dose': 'e4 alleles'}.get(facet, 'age')
        span = ''
        if low is not None and high is not None:
            low_text, high_text = (f'{low:.0f}', f'{high:.0f}') if facet in ('age', 'age_adjusted') else (f'{low:g}', f'{high:g}')
            span = f', {what} {low_text} to {high_text}'
        return f'{n_donors} {noun}{span}'
    if unit.get('n_donors_ref') is not None and unit.get('n_donors_test') is not None:
        return f"{unit.get('level_test')} {unit['n_donors_test']}, {unit.get('level_ref')} {unit['n_donors_ref']}"
    return ', '.join(f'{level} {n}' for level, n in counts.items())


def _floor_text(unit: Dict[str, Any]) -> str:
    """How many replicates the p-value floor of this unit asks for, in words ('donors need 5 per group')."""
    per_level, numeric = _P_VALUE_FLOORS.get(str(unit.get('replicate_unit') or 'donor'), _P_VALUE_FLOORS['donor'])
    noun = _replicate_noun(unit)
    if unit.get('facet') in _NUMERIC_TEST_FACETS:
        return f'{noun} need {numeric} for a trend'
    return f'{noun} need {per_level} per group'


def _fx_stratum(unit: Dict[str, Any]) -> str:
    """Design stratum of a unit ('line=3A21', 'timepoint=6m'), from ``design_subset`` or the ``@`` part of the cell type source."""
    subset = unit.get('design_subset')
    if subset:
        return str(subset)
    source = str(unit.get('celltype_source') or '')
    return source.split('@', 1)[1] if '@' in source else ''


def _fx_pills(unit: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    """Flag pills of one unit as (label, css class, one-sentence tooltip, glossary slug)."""
    flags = unit.get('flags') or {}
    pills: List[Tuple[str, str, str, str]] = []
    level, replicate = unit.get('analysis_level'), unit.get('replicate_unit')
    if level == 'cell':
        pills.append(('cell-level, descriptive only', 'warn',
                      (unit.get('analysis_level_reason') or unit.get('reason') or 'Too few replicates for a test')
                      + '; the numbers describe cells, so there is no p or q value.', 'descriptive-only'))
    elif level == 'below_floor':
        pills.append((f'descriptive: below the p-value floor ({_floor_text(unit)})', 'warn',
                      (unit.get('analysis_level_reason') or 'Fewer replicates than the p-value floor')
                      + '. The estimate is shown without p, q or interval and the unit is not counted towards replication.',
                      'p-value-floor'))
    elif level == 'replicate':
        pills.append((f'replicate level ({replicate})' if replicate else 'replicate level', 'info',
                      f'Tested with one value per {replicate or "replicate"}, not per cell.', 'analysis-level'))
    if unit.get('pooled'):
        pills.append(('pooled', 'info',
                      'Several files of one cohort were combined per donor, so the cohort counts once.',
                      'pooled-unit'))
    if unit.get('overlap_unknown'):
        pills.append(('donor overlap not checked', 'warn',
                      'It is not known whether this cohort shares donors with another, which would inflate agreement.',
                      'donor-overlap'))
    if unit.get('is_secondary'):
        pills.append(('reanalysis, not counted', 'warn',
                      'A reanalysis of cells another dataset serves; shown but never counted.', 'cohort'))
    if flags.get('descriptive_only') and level != 'below_floor':
        pills.append(('descriptive only', 'warn',
                      'Too few donors in a group for a p or q value; the estimate is shown for orientation.',
                      'descriptive-only'))
    if flags.get('small_sample') and level == 'replicate':
        pills.append(('small sample', 'info',
                      'Fewer than 5 replicates in a group (10 for a trend): tested, but with little power.',
                      'small-cohorts'))
    if unit.get('inflated'):
        lam = unit.get('lambda_gc')
        pills.append((f'inflated, lambda {lam:.2f}' if isinstance(lam, (int, float)) else 'inflated', 'warn',
                      'Many genes shift together (lambda above 1.3): often a batch, depth or composition difference '
                      'rather than biology. Read the p values with care.', 'lambda'))
    dropped = unit.get('covariates_dropped') or []
    if dropped:
        pills.append((f'covariates dropped ({len(dropped)})', 'info',
                      'Left out of the model to keep the test: ' + '; '.join(str(item) for item in dropped), 'covariates'))
    if unit.get('control_basis'):
        pills.append((f"control: {_CONTROL_BASIS_TEXT.get(str(unit['control_basis']), str(unit['control_basis']).replace('_', ' '))}",
                      'info', 'What the control animals of this comparison are.', 'app-nlgf'))
    stratum = _fx_stratum(unit)
    if stratum and unit.get('facet') == 'app_nlgf':
        pills.append((f"stratum {stratum.replace('|', ', ')}", 'info',
                      'Animals are compared within one grafted line and timepoint, never across them.', 'app-nlgf'))
    if unit.get('age_top_coded'):
        pills.append(('ages top-coded', 'warn',
                      f"Ages above {unit['age_top_coded']} are recorded as {unit['age_top_coded']}, which flattens the oldest donors."
                      if not isinstance(unit['age_top_coded'], bool) else
                      'Ages above a ceiling are recorded as the ceiling, which flattens the oldest donors.', 'age-twins'))
    discordant = unit.get('n_sex_discordant')
    if isinstance(discordant, int) and discordant > 0:
        pills.append((f"{discordant} of {unit.get('n_sex_checked')} sex labels disagree", 'warn',
                      'XIST and Y-linked expression contradict the recorded sex of these donors.', 'sex-check'))
    if flags.get('provisional'):
        pills.append(('provisional', 'warn',
                      'What counts as control still differs between studies, so these results may change.',
                      'disease-basis'))
    if flags.get('composition_flag'):
        pvalue = flags.get('composition_p')
        ptext = f' (p = {pvalue:.2g})' if isinstance(pvalue, (int, float)) else ''
        pills.append(('composition', 'warn',
                      f"The share of this cell type among each donor's cells changes with the facet{ptext}, "
                      'so the effect may be a shift in which cells were captured.', 'composition'))
    if flags.get('batch_confounded'):
        pills.append(('batch confounded', 'warn',
                      'The facet cannot be separated from batch, so batch was left out of the model.',
                      'covariates'))
    if flags.get('batch_used'):
        pills.append(('batch adjusted', 'info', 'A donor-level batch column was included in the model.',
                      'covariates'))
    if unit.get('dataset_changed'):
        pills.append(('data changed', 'warn',
                      'The dataset changed after this test was run; the result describes the earlier version.',
                      'facet'))
    return pills


def _fx_model_view(model: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact model line plus the detail rows for the disclosure."""
    model = model or {}
    parts, details = [], []
    stratum = model.get('stratum') or {}
    details.append(('Stratum', f"{stratum.get('cell_type', 'not recorded')}: "
                               f"{stratum.get('description', 'not recorded')}"))
    details.append(('Facet term', model.get('facet_term') or 'not recorded'))
    donors = model.get('donors') or {}
    per_level = donors.get('per_level')
    if isinstance(per_level, dict):
        per_level = ', '.join(f'{k} {v}' for k, v in per_level.items())
    details.append(('Donors used', f"{per_level or 'not recorded'}; analysed {donors.get('n_analysed', 'not recorded')}, "
                                   f"left out for a missing covariate {donors.get('n_dropped_missing_covariate', 'not recorded')}; "
                                   f"at least {donors.get('min_cells_per_donor', 'not recorded')} cells per donor in the stratum, "
                                   f"at least {donors.get('min_donors_per_level', 'not recorded')} donors per level"))
    details.append(('Descriptive-only rule', model.get('descriptive_only_rule') or 'not recorded'))
    for kind, label in (('genes', 'Genes'), ('modules', 'Modules')):
        part = model.get(kind)
        if not part:
            continue
        method_short = 'limma-voom' if kind == 'genes' else (
            f"OLS + {part['n_permutations']:,} permutations" if isinstance(part.get('n_permutations'), int)
            else 'OLS + permutations')
        parts.append(f"{part.get('formula')} ({method_short})")
        unit_of_analysis = part.get('unit_of_analysis')
        if isinstance(unit_of_analysis, dict):
            unit_of_analysis = ' | '.join(f'{k}: {v}' for k, v in unit_of_analysis.items())
        covariates = '; '.join(f"{c['name']} {c['status']} ({c['reason']})" for c in part.get('covariates') or [])
        versions = part.get('versions')
        if isinstance(versions, dict):
            versions = ', '.join(f'{k} {v}' for k, v in versions.items())
        details.append((f'{label}: unit of analysis', unit_of_analysis or 'not recorded'))
        details.append((f'{label}: formula', part.get('formula') or 'not recorded'))
        details.append((f'{label}: covariates', covariates or 'not recorded'))
        details.append((f'{label}: method', part.get('method') or 'not recorded'))
        details.append((f'{label}: multiple testing', part.get('multiple_testing') or 'not recorded'))
        details.append((f'{label}: run', f"{part.get('tool')}, seed {part.get('seed')}, run {part.get('run_date')}; "
                                         f"{versions or 'versions not recorded'}"))
    return {'line': '; '.join(parts) or 'not recorded', 'details': details}


_DISEASE_BASIS_TEXT = {
    'braak_cerad': 'AD vs control: pathologic (Braak x CERAD)',
    'nia_reagan': 'AD vs control: pathologic (NIA-Reagan)',
    'adnc': 'AD vs control: pathologic (ADNC)',
    'clinical_dx': 'AD vs control: clinical diagnosis',
    'author_dx': 'AD vs control: author diagnosis',
}
_REPLICATE_UNITS = ('donor', 'animal', 'sample', 'none')


def _disease_basis_text(basis: Optional[str]) -> str:
    """Plain wording of a ``disease_basis`` value; empty when none is recorded."""
    if not basis:
        return ''
    return _DISEASE_BASIS_TEXT.get(str(basis), f'AD vs control: {basis}')


def _replicate_view(*sources: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Replicate unit and disease basis of a dataset for display.

    Each key takes the first non-empty value over ``sources`` (dataset info
    first, then the facet-test summary), so a declared value wins over a
    value copied onto the test units. None when no source names a unit.
    """
    def first(key: str) -> Optional[Any]:
        for source in sources:
            if isinstance(source, dict) and source.get(key):
                return source[key]
        return None
    unit, column, basis, disease = (first('replicate_unit'), first('replicate_column'),
                                    first('replicate_basis'), first('disease_basis'))
    if not unit and not disease:
        return None
    if unit == 'none':
        text = 'none: cell-level only'
    elif unit and column:
        text = f'{unit} ({column})'
    else:
        text = str(unit or '')
    return {
        'unit': unit, 'column': column, 'basis': basis,
        'text': text + (', inferred' if unit and basis == 'inferred' else ''),
        'disease_basis': disease, 'disease_text': _disease_basis_text(disease),
    }


def _fx_model_view_with_qc(unit: Dict[str, Any]) -> Dict[str, Any]:
    """Model line and detail rows of a unit, plus its quality and design rows (inflation, dropped covariates, control, stratum)."""
    view = _fx_model_view(unit.get('model'))
    details = view['details']
    if isinstance(unit.get('lambda_gc'), (int, float)):
        text = f"lambda {unit['lambda_gc']:.2f}" + (' (inflated: above 1.3)' if unit.get('inflated') else ' (calibrated)')
        if isinstance(unit.get('df_total_median'), (int, float)):
            text += f"; median residual degrees of freedom {unit['df_total_median']:.1f}"
        details.append(('Calibration', text))
    if unit.get('covariates_dropped'):
        details.append(('Covariates dropped', '; '.join(str(item) for item in unit['covariates_dropped'])))
    if unit.get('control_basis'):
        details.append(('Control', _CONTROL_BASIS_TEXT.get(str(unit['control_basis']), str(unit['control_basis']).replace('_', ' '))))
    stratum = _fx_stratum(unit)
    if stratum:
        details.append(('Design stratum', stratum.replace('|', ', ')))
    if unit.get('age_top_coded'):
        details.append(('Age ceiling', f"ages above {unit['age_top_coded']} are recorded as {unit['age_top_coded']}"
                        if not isinstance(unit['age_top_coded'], bool) else 'ages above a ceiling are recorded as the ceiling'))
    return view


def _fx_unit_view(unit: Dict[str, Any], index: int, db_id: str = '', title: str = '',
                  pool_members: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    modules = [{**m, 'label': _METHOD_LABELS.get((m.get('method'), m.get('source')),
                                                  f"{m.get('method')}, {m.get('source')}")}
               for m in unit.get('modules') or []]
    return {
        **unit,
        'db_id': db_id or unit.get('db_id') or '',
        'title': title or unit.get('title') or '',
        'facet_label': _FACET_TEST_LABELS.get(unit.get('facet'), unit.get('facet')),
        'contrast_label': _facet_contrast_label(unit),
        'donors_text': _fx_donors_text(unit),
        'pills': _fx_pills(unit),
        'modules_view': modules,
        'model_view': _fx_model_view_with_qc(unit),
        'unit_id': f'fx-{index}',
        # a cell-level or below-floor unit has no test, whatever its flags say
        'no_test': unit.get('analysis_level') in ('cell', 'below_floor')
                   or bool((unit.get('flags') or {}).get('descriptive_only')),
        'is_below_floor': unit.get('analysis_level') == 'below_floor',
        'floor_text': _floor_text(unit),
        'stratum': _fx_stratum(unit).replace('|', ', '),
        'is_pool': str(db_id or unit.get('db_id') or '').startswith('pool:'),
        'pool_members_list': (pool_members or {}).get(db_id or unit.get('db_id') or '') or [],
        'disease_basis_text': _disease_basis_text(unit.get('disease_basis')),
    }


_FX_SYSTEM_REASONS = {
    'xenograft': 'a xenograft with too few host animals per group in any cell type for a test',
    'bulk_tissue': 'bulk tissue: about one observation per sample, no cell types to test within',
    'in_vitro_2d': 'an in vitro culture: there is no donor-level group to compare',
    'in_vitro_3d': 'an in vitro culture: there is no donor-level group to compare',
    'organoid': 'an organoid culture: there is no donor-level group to compare',
    'chimera': 'a chimera with too few host animals per group in any cell type for a test',
}

_FX_EXCLUDED_PREFIX = 'excluded from the facet layer:'
_FX_MOUSE_ONLY = ('mouse-only dataset', 'organism not human (mouse)')


def _fx_exclusion_text(reason: Optional[str]) -> Optional[str]:
    """Plain sentence why a dataset is outside the facet layer on purpose, or None when it is not.

    Covers the served reasons of the facet tool: ``excluded from the facet layer: ...`` (spatial and
    proteomics data, metacell sets and sets set aside in the dataset ``.md``) and pure mouse datasets.
    """
    text = (reason or '').strip()
    if text.startswith(_FX_MOUSE_ONLY):
        return ('This is a pure mouse dataset. The facet analysis covers human cells only (post-mortem tissue, and '
                'human cells grafted into mouse hosts); mouse datasets keep their columns for browsing and plotting '
                'but have no facet tests and no replication.')
    if not text.startswith(_FX_EXCLUDED_PREFIX):
        return None
    detail = text[len(_FX_EXCLUDED_PREFIX):].strip()
    spatial = re.match(r'spatial technology \(([^)]*)\)', detail)
    if spatial or detail.startswith('experimental_system spatial'):
        platform = f' ({spatial.group(1)})' if spatial else ''
        return (f'This is spatial data{platform}. A spot or bin is not a single cell of one cell type, so there is no '
                'per-donor profile of a cell type to compare and no facet analysis.')
    if detail.startswith('proteomics'):
        return ('This is proteomics, not single-cell expression: there are no cells to group by cell type and donor, '
                'so there is no facet analysis.')
    if detail.startswith('metacell'):
        return (f'This set is left out of the facet analysis on purpose: {detail}. Metacells are aggregates, so '
                'they cannot be counted as donors or cells.')
    return f'This set is left out of the facet analysis on purpose: {detail}.'


def _fx_reason_text(status: Optional[str], reason: Optional[str]) -> str:
    """Plain-language reason why a dataset has no facet tests.

    Translates the eligibility reasons of the facet tool (as served by
    ``/databases/{id}/facet-tests``); unknown reasons are quoted verbatim.
    """
    text = (reason or '').strip()
    excluded = _fx_exclusion_text(text)
    if excluded:
        return excluded[0].lower() + excluded[1:].rstrip('.')
    if status == 'not_run' and not text.startswith(('experimental_system', 'organism', 'secondary')):
        return 'no facet test has been run on this dataset yet'
    system = re.match(r'experimental_system\s+(\S+)', text)
    if system:
        known = _FX_SYSTEM_REASONS.get(system.group(1))
        return f'this dataset is {known}' if known else f'experimental system {system.group(1)}, which is not tested'
    if text.startswith('secondary'):
        return ('this dataset is a reanalysis of cells another dataset serves; facets are tested on the primary data '
                'only, so the effects are shown there')
    organism = re.match(r'organism not human \((.*)\)', text)
    if organism:
        return f'organism {organism.group(1)}: the facet analysis covers human cells only'
    donors = re.match(r'(\d+) donors? \(< (\d+)\)', text)
    if donors:
        return f'only {donors.group(1)} donors; at least {donors.group(2)} are needed'
    if 'no raw counts layer' in text:
        return 'no raw counts layer is served, which the donor pseudobulk needs'
    if 'no ant_donor' in text:
        return 'no donor column is mapped (ant_donor), so no donor-level test is possible'
    if 'enough donors per level' in text:
        return ('no combination of facet and cell type had enough donors per level (at least 3 replicates in each level, '
                'at least 4 in total); some facets may have no levels at all in this dataset')
    return text or 'no reason was recorded'


def _facet_tests_view(summary: Optional[Dict[str, Any]], db_id: str = '') -> Optional[Dict[str, Any]]:
    """Dataset-page data for the facet effects, grouped by facet column.

    None when the API could not answer (older ant-serve, error).
    """
    if not isinstance(summary, dict) or 'status' not in summary:
        return None
    by_facet: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    index = 0
    for unit in summary.get('units') or []:
        column = _TEST_FACET_COLUMN.get(str(unit.get('facet')), 'ant_' + str(unit.get('facet')))
        slot = by_facet.setdefault(column, {'tested': [], 'untested': [], 'scope': unit.get('facet_scope')})
        if unit.get('tested'):
            slot['tested'].append(_fx_unit_view({**unit, 'dataset_changed': summary.get('dataset_changed')},
                                                index, db_id=db_id))
            index += 1
        else:
            slot['untested'].append({'cell_type': unit.get('cell_type'), 'reason': unit.get('reason'),
                                     'analysis_level': unit.get('analysis_level'),
                                     'analysis_level_label': unit.get('analysis_level_label')})
    exclusion = _fx_exclusion_text(summary.get('reason')) if summary.get('status') != 'tested' else None
    return {
        'status': summary.get('status'),
        'reason': summary.get('reason'),
        'reason_text': _fx_reason_text(summary.get('status'), summary.get('reason')),
        'excluded': exclusion is not None,
        'excluded_text': exclusion,
        'facet_scope': summary.get('facet_scope') if summary.get('n_units') else None,
        'dataset_changed': bool(summary.get('dataset_changed')),
        'by_facet': by_facet,
        'n_units_tested': summary.get('n_units_tested') or 0,
        'cohort': summary.get('cohort') or None,
        # the summary names the unit; older servers only carry it on the units
        'replicate_source': {key: next((src[key] for src in [summary, *(summary.get('units') or [])]
                                        if src.get(key)), None)
                             for key in ('replicate_unit', 'replicate_column', 'replicate_basis',
                                         'disease_basis')},
        # permutation count of the module run (run manifest); None when unknown
        'n_permutations': ((summary.get('runs') or {}).get('modules') or {}).get('n_permutations')
                          if isinstance(((summary.get('runs') or {}).get('modules') or {}).get('n_permutations'), int)
                          else None,
    }


def _dataset_facet_tests(api_url: str, db_id: str) -> Optional[Dict[str, Any]]:
    """Facet effects of one dataset; None when the API cannot answer."""
    try:
        return _facet_tests_view(_api_get(api_url, f'/databases/{db_id}/facet-tests'), db_id=db_id)
    except Exception:
        return None


def _pool_members(api_url: str) -> Dict[str, List[str]]:
    """Member datasets of every donor-pooled unit (``pool:<id>``), from the cohort table.

    Empty when the API cannot answer: a pooled unit then has no member list.
    """
    try:
        payload = _api_get(api_url, '/facets/cohorts') or {}
    except Exception:
        return {}
    return {str(row.get('db_id')): list(row.get('pool_members') or [])
            for row in payload.get('datasets') or []
            if str(row.get('db_id') or '').startswith('pool:')}


def _facet_tests_across(api_url: str, facet_name: str, scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Rows for the "Effects across datasets" section of /facets/{name};
    None when the facet is not tested or the API cannot answer. ``scope`` names one
    facet scope (None lets the API choose the default of the facet)."""
    try:
        payload = _api_get(api_url, f"/facets/{url_quote(facet_name, safe='')}/tests"
                           + (f'?scope={scope}' if scope else ''), timeout=90)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    pool_members = _pool_members(api_url)
    rows = [_fx_unit_view({**row, 'facet': payload.get('facet')}, index, db_id=row.get('db_id', ''),
                          title=row.get('title') or '', pool_members=pool_members)
            for index, row in enumerate(payload.get('rows') or [])]
    return {
        'facet': payload.get('facet'),
        'scope': payload.get('scope') or scope,
        'rows': rows,
        'n_datasets_tested': payload.get('n_datasets_tested') or 0,
        'not_tested': payload.get('not_tested') or [],
    }


_facets_visible_cfg: list = []     # [list] once read from anthive.yaml


def _facets_visible() -> List[str]:
    """Configured visible facets ([] = all). Same setting ant-serve reads."""
    if not _facets_visible_cfg:
        try:
            from config import get_facets_visible, load_config  # noqa: PLC0415
            _facets_visible_cfg.append(get_facets_visible(load_config()))
        except Exception:  # config unavailable (tests import this module bare)
            _facets_visible_cfg.append([])
    return list(_facets_visible_cfg[0])


def _dataset_papers(api_url: str, db_id: str, scope: str = 'dataset') -> List[Dict]:
    """Paper cards for one dataset, source papers first; [] when the library is
    off, the dataset has no papers or the API errors."""
    try:
        payload = _api_get(api_url, f'/databases/{db_id}/papers') or {}
    except Exception:
        return []
    papers = payload.get('papers') if isinstance(payload, dict) else None
    return _sort_paper_cards([_paper_card(p, scope) for p in papers or [] if p.get('citekey')])


def _collection_papers(api_url: str, group_id: str, file_ids: List[str]) -> List[Dict]:
    """Papers across a collection's datasets, deduplicated; each card notes how
    many of the collection's datasets it is linked to."""
    by_key: Dict[str, Dict] = {}
    counts: Dict[str, int] = defaultdict(int)
    for file_id in file_ids:
        for card in _dataset_papers(api_url, f"{group_id}/{file_id}", scope='collection'):
            counts[card['citekey']] += 1
            current = by_key.get(card['citekey'])
            if current is None or _PAPER_ROLE_ORDER.get(card['role'], 9) < _PAPER_ROLE_ORDER.get(current['role'], 9):
                by_key[card['citekey']] = card
    for key, card in by_key.items():
        card['n_linked'] = counts[key]
        card['n_total'] = len(file_ids)
    return _sort_paper_cards(list(by_key.values()))


# Related-paper grouping on the paper page: the first matching kind wins.
_RELATION_GROUPS = [
    ('explicit', 'Same study (versions, preprints, follow-ups)'),
    ('cohort', 'Same cohort'),
    ('method', 'Same method'),
    ('author', 'Shared authors'),
    ('celltype', 'Same cell type, tissue or disease'),
    ('tissue', 'Same cell type, tissue or disease'),
    ('disease', 'Same cell type, tissue or disease'),
    ('cites', 'Citations'),
    ('cited_by', 'Citations'),
]
_EXPLICIT_WORDS = {'version': 'another version of the same study',
                   'samecohort': 'uses the same cohort',
                   'extends': 'builds on or extends this work',
                   'reanalysis': 'reanalyses the same data',
                   'comment': 'a comment on this work'}


def _relation_reason(kind: str, evidence: Any) -> str:
    items = [str(e) for e in _as_list(evidence) if str(e).strip()]
    if kind == 'explicit':
        return '; '.join(_EXPLICIT_WORDS.get(i.lower(), i) for i in items) or 'explicitly related'
    if kind == 'cohort':
        return f"both use the {', '.join(items)} cohort{'s' if len(items) > 1 else ''}"
    if kind == 'author':
        names = [_surname(n) for n in items]
        shown = ', '.join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else '')
        return f"shared author{'s' if len(names) > 1 else ''}: {shown}"
    if kind in ('celltype', 'tissue', 'disease', 'method'):
        label = {'celltype': 'cell type', 'tissue': 'tissue', 'disease': 'disease',
                 'method': 'method'}[kind]
        return f"same {label}: {', '.join(items)}"
    if kind == 'cites':
        return 'this paper cites it'
    if kind == 'cited_by':
        return 'it cites this paper'
    return kind


def _related_groups(related: List[Dict]) -> List[Dict]:
    """Group related papers by their strongest kind of relation; every entry
    keeps all its reasons."""
    order = [kind for kind, _ in _RELATION_GROUPS]
    titles = dict(_RELATION_GROUPS)
    groups: Dict[str, List[Dict]] = {}
    for item in related or []:
        relations = [r for r in item.get('relations') or [] if r.get('kind')]
        kinds = [r['kind'] for r in relations]
        primary = next((k for k in order if k in kinds), None)
        if not item.get('citekey') or primary is None:
            continue
        ranked = sorted(relations, key=lambda r: order.index(r['kind']) if r['kind'] in order else 99)
        groups.setdefault(titles[primary], []).append({
            'citekey': item['citekey'],
            'url': f"/papers/{url_quote(item['citekey'], safe='')}",
            'title': item.get('title') or item['citekey'],
            'citation': item.get('citation') or '',
            'reasons': [_relation_reason(r['kind'], r.get('evidence')) for r in ranked],
        })
    out: List[Dict] = []
    for _kind, heading in _RELATION_GROUPS:
        if heading in groups and all(g['heading'] != heading for g in out):
            out.append({'heading': heading, 'papers': groups[heading]})
    return out


def _dataset_titles(api_url: str) -> Dict[str, str]:
    try:
        return {db['id']: (db.get('title') or db.get('file_id') or db['id'])
                for db in _all_databases(api_url) if db.get('id')}
    except Exception:
        return {}


# Top-nav "library" link: shown only when the API reports papers. Probed in a
# background thread and cached so page renders never wait on the API.
_LIBRARY_TTL_OK = 120.0
_LIBRARY_TTL_FAIL = 30.0
_library_state: Dict[str, Any] = {'available': False, 'checked': 0.0, 'refreshing': False}
_library_lock = threading.Lock()


def _refresh_library_available(api_url: str) -> None:
    available = False
    try:
        status = _api_get(api_url, '/library/status') or {}
        available = int(status.get('papers') or 0) > 0
    except Exception:
        pass
    with _library_lock:
        _library_state.update(available=available, checked=time.monotonic(), refreshing=False)


def _library_available(request: Request) -> bool:
    now = time.monotonic()
    with _library_lock:
        ttl = _LIBRARY_TTL_OK if _library_state['available'] else _LIBRARY_TTL_FAIL
        start = now - _library_state['checked'] > ttl and not _library_state['refreshing']
        if start:
            _library_state['refreshing'] = True
        available = _library_state['available']
    if start:
        try:
            threading.Thread(target=_refresh_library_available,
                             args=(request.app.state.api_url,), daemon=True).start()
        except Exception:
            with _library_lock:
                _library_state['refreshing'] = False
    return available


templates.env.globals['library_available'] = _library_available


_TOPIC_FIELDS = [('celltypes', 'Cell types', 'anthive_celltypes'),
                 ('tissues', 'Tissues', 'anthive_tissues'),
                 ('diseases', 'Diseases', 'anthive_diseases'),
                 ('organisms', 'Organisms', 'anthive_organisms'),
                 ('methods', 'Methods', 'anthive_methods'),
                 ('cohorts', 'Cohorts', 'anthive_cohorts')]


@router.get("/papers/{citekey}", response_class=HTMLResponse)
async def paper_view(request: Request, citekey: str):
    api_url = _api_url(request)
    try:
        record = _api_get(api_url, f'/papers/{url_quote(citekey, safe="")}')
    except Exception:
        record = None
    if not isinstance(record, dict) or not record.get('citekey'):
        return templates.TemplateResponse(
            request, "index.html", {"folders": [], "error": f"Paper '{citekey}' not found"},
            status_code=404)
    card = _paper_card(record)
    drafts = set(_as_list(record.get('draft_fields')))
    topics = [{'label': label, 'vals': _as_list(record.get(key)), 'draft': field in drafts}
              for key, label, field in _TOPIC_FIELDS if _as_list(record.get(key))]
    titles = _dataset_titles(api_url)
    linked: Dict[str, List[Dict]] = {}
    for link in record.get('datasets') or []:
        db_id = link.get('db_id') or ''
        if '/' not in db_id:
            continue
        group_id, file_id = db_id.split('/', 1)
        linked.setdefault((link.get('role') or '').lower(), []).append({
            'db_id': db_id, 'title': titles.get(db_id, db_id),
            'url': f"/browse/{url_quote(group_id, safe='')}/{url_quote(file_id, safe='')}",
        })
    linked_groups = [{'role': role,
                      'badge': _PAPER_ROLE_BADGE.get(role, role.capitalize() or 'Linked'),
                      'reason': _paper_reason(role, record.get('entrytype') or '', 'datasets'),
                      'datasets': items}
                     for role, items in sorted(linked.items(),
                                               key=lambda kv: _PAPER_ROLE_ORDER.get(kv[0], 9))]
    notes = (record.get('notes') or '').strip()
    notes_html = ''
    if notes and 'no notes yet' not in notes.lower():
        notes_html = markdown.markdown(notes, extensions=['tables', 'fenced_code'])
    abstract = ((record.get('fields') or {}).get('abstract') or record.get('abstract') or '').strip()
    own_role = str(record.get('role') or 'background').lower()
    fields = record.get('fields') or {}
    return templates.TemplateResponse(request, "paper.html", {
        "paper": card,
        "record": record,
        "doc_type": fields.get('type') or '',
        "own_role": own_role,
        "own_role_badge": _PAPER_ROLE_BADGE.get(own_role, own_role.capitalize()),
        "own_role_help": _PAPER_ROLE_HELP.get(own_role, ''),
        "authors": [a for a in _as_list(record.get('authors')) if a],
        "abstract": abstract,
        "topics": topics,
        "topics_draft": any(t['draft'] for t in topics),
        "linked_groups": linked_groups,
        "related_groups": _related_groups(record.get('related') or []),
        "notes_html": notes_html,
    })


@router.get("/library", response_class=HTMLResponse)
async def library_view(request: Request):
    error = None
    try:
        payload = _api_get(_api_url(request), '/papers') or {}
        papers = payload.get('papers') if isinstance(payload, dict) else None
        papers = papers or []
    except Exception:
        papers, error = [], "The paper library is not available on this site."
    rows = []
    for paper in papers:
        if not paper.get('citekey'):
            continue
        card = _paper_card(paper)
        cohorts = [str(c) for c in _as_list(paper.get('cohorts'))]
        celltypes = [str(c) for c in _as_list(paper.get('celltypes'))]
        searchable = [card['title'], card['citation_line'], card['citekey'], card['doi'],
                      ' '.join(str(a) for a in _as_list(paper.get('authors'))),
                      ' '.join(cohorts), ' '.join(celltypes)]
        for key in ('tissues', 'diseases', 'methods', 'organisms'):
            searchable.append(' '.join(str(v) for v in _as_list(paper.get(key))))
        rows.append({**card, 'cohorts': cohorts, 'celltypes': celltypes,
                     'n_datasets': paper.get('n_datasets') or 0,
                     'search': _fold_text(' '.join(s for s in searchable if s))})
    rows.sort(key=lambda r: (-_year_int(r), r['citekey']))
    return templates.TemplateResponse(request, "library.html", {
        "rows": rows,
        "years": sorted({str(r['year']) for r in rows if r.get('year')}, reverse=True),
        "cohorts": sorted({c for r in rows for c in r['cohorts']}, key=str.lower),
        "celltypes": sorted({c for r in rows for c in r['celltypes']}, key=str.lower),
        "roles": [(role, _PAPER_ROLE_BADGE[role]) for role in _PAPER_ROLE_ORDER
                  if any(r['role'] == role for r in rows)],
        "error": error,
    })
