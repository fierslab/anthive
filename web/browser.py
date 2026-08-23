"""
Web browser routes for Anthive

Provides HTML interface for browsing single-cell datasets.
Data is sourced entirely from the ant-serve REST API.
"""

import base64
import html as html_lib
import re
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote as url_quote, unquote, urlencode

import markdown
import requests
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

# lib/ is one level up from web/
_lib_dir = Path(__file__).parent.parent / 'lib'
if str(_lib_dir) not in sys.path:
    sys.path.insert(0, str(_lib_dir))

from canonical_columns import is_canonical_column, strip_ant_prefix  # noqa: E402

router = APIRouter()
templates_dir = Path(__file__).parent / 'templates'
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.filters['urlencode'] = url_quote


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

def _api_get(api_url: str, path: str) -> Any:
    """GET request to the ant-serve API"""
    url = f"{api_url.rstrip('/')}{path}"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def _api_url(request: Request) -> str:
    return request.app.state.api_url


# TTL for the genesets-availability probe: cache True for 60 s, False for 5 s.
# This means a startup race self-heals within 5 s on the next request without
# requiring a browse restart.
_GENESETS_TTL_TRUE = 60.0
_GENESETS_TTL_FALSE = 5.0


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

    source ∈ {'precomputed', 'microglia_fallback', 'astrocyte_fallback',
              'neuron_fallback', 'none'}.
    """
    if top_genes:
        return list(top_genes), 'precomputed'
    ct = (cell_type_term or '').lower()
    if ct in _MICROGLIA_CELLTYPE_TERMS:
        return list(_MICROGLIA_MARKER_FALLBACK), 'microglia_fallback'
    if ct in _ASTROCYTE_CELLTYPE_TERMS:
        return list(_ASTROCYTE_MARKER_FALLBACK), 'astrocyte_fallback'
    if ct in _NEURON_CELLTYPE_TERMS:
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


# ── module-score column classification ───────────────────────────────────
#
# Score sidecar columns show up in /info's numerical obs list under one of
# three name conventions (see lib/anthive_db._score_col_name):
#   scanpy/geneset : "Sierksma2025::WGCNA::Brown_Lysosome"
#   starcat        : "starcat__MICROGLIA_V1__DAM_pos"
#   aucell         : "aucell__Sierksma2025/WGCNA__Brown_Lysosome"
#
# _classify_score_column parses one column name; _group_module_scores
# collapses a full list into a per-module structure exposing which methods
# actually scored it on this dataset, so the dataset page can render a
# unified "module scores" table with per-method chips.

_STARCAT_PREFIX = 'starcat__'
_AUCELL_PREFIX = 'aucell__'


def _classify_score_column(col: str) -> Optional[Dict[str, str]]:
    """Return {method, catalog, module} for a score column, else None.

    ``catalog`` is the human-facing catalog id (``Sierksma2025/WGCNA`` or
    ``MICROGLIA_V1``); ``module`` is the module / program name.
    """
    if col.startswith(_STARCAT_PREFIX):
        rest = col[len(_STARCAT_PREFIX):]
        catalog, sep, module = rest.rpartition('__')
        if not sep or not catalog or not module:
            return None
        return {'method': 'starcat', 'catalog': catalog, 'module': module}
    if col.startswith(_AUCELL_PREFIX):
        rest = col[len(_AUCELL_PREFIX):]
        catalog, sep, module = rest.rpartition('__')
        if not sep or not catalog or not module:
            return None
        return {'method': 'aucell', 'catalog': catalog, 'module': module}
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
        one of ``scanpy|starcat|aucell`` → the actual obs-column name.
      - ``non_score_cols``: list of column names that are NOT module scores
        (native numerical obs — n_counts, pseudotime, …).
      - ``method_counts``: {method → int} number of modules scored by that method.
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
        'aucell':  sum(1 for m in modules if 'aucell'  in m['methods']),
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

# Source — internal/external (lab-generated vs imported from elsewhere)
_SOURCE_ABBREV = {
    'internal': '🏠',
    'external': '↓',
}


# ── unified access + publication-status icon ─────────────────────────────────
# One glyph replaces the old two-icon (access + source) system. Shape encodes
# access (public → globe, internal → house); colour encodes publication_status
# (unpublished → muted red, published → muted green). Container width is fixed
# so row alignment survives regardless of glyph choice.
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
    # house glyph in white on top
    '<g fill="none" stroke="white" stroke-width="1.2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3.4 8.2 L8 4.4 L12.6 8.2"/>'
    '<path d="M4.6 8.2 v4.2 h6.8 v-4.2"/>'
    '<path d="M7.1 12.4 v-2.4 h1.8 v2.4"/>'
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
# calling ``_pub_status_cache.clear()`` if needed.
_pub_status_cache: Dict[str, Optional[str]] = {}


def _publication_status_for_group(group_name: Optional[str]) -> Optional[str]:
    """Read ``publication_status`` from ``data/h5adstore/<group>/index.md``.

    ``group_name`` is the folder name (``collection.group`` from the API — the
    long-form like ``Blackburn2026``, NOT the short ``group_id``). Returns the
    frontmatter value (lower-cased) or None if the file/field is missing.
    """
    if not group_name:
        return None
    if group_name in _pub_status_cache:
        return _pub_status_cache[group_name]
    result: Optional[str] = None
    try:
        path = _H5ADSTORE_ROOT / group_name / 'index.md'
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
    _pub_status_cache[group_name] = result
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
        name = collection_meta.get('group') or group_id
        # Group modification = most recent member modification.
        last_modified = max((_modified_ts(db) for db in dbs), default='')

        # publication_status is a collection-level attribute (index.md
        # frontmatter). Ant-serve doesn't expose it yet; read from disk.
        pub_status = _publication_status_for_group(collection_meta.get('group'))

        # Unified access+publication icon (one glyph per collection). Access
        # is a per-dataset field; when it varies inside the collection we fall
        # back to 'internal' (the safer default). If any dataset is public we
        # keep the 'public' semantic for the header icon.
        access_values = sorted({(db.get('access') or 'internal').lower() for db in dbs})
        coll_access = 'public' if 'public' in access_values else access_values[0]
        pub_icon = _access_pub_icon(coll_access, pub_status)

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

    return templates.TemplateResponse(request, "index.html", {
        "folders": collections,
        "total_datasets": total_datasets,
        "total_folders": len(collections),
        "total_cells": total_cells,
        "filter_options": filter_options,
        "tissue_tree": tissue_tree,
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

    # Friendly group name (from the collection metadata in any of the datasets);
    # falls back to the short group_id when not declared.
    friendly_group = None
    try:
        all_dbs = _all_databases(_api_url(request))
        for db in all_dbs:
            if (db.get('group_id') or db['id'].split('/')[0]) == group_id:
                friendly_group = (db.get('collection') or {}).get('group')
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
    friendly_group = collection.get('group') or group_id
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
    try:
        info = _api_get(api_url, f'/databases/{db_id}/info')
        friendly_group = (info.get('collection') or {}).get('group') or group_id
        dataset_title = info.get('title') or file_id
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
    # (catalog, module) so scanpy/starcat/aucell for the same module collapse
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
    ]

    collection = db.get('collection') or {}
    friendly_group = collection.get('group') or group_id
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

    return templates.TemplateResponse(request, "dataset.html", {
        "folder_name": friendly_group,
        "folder_browse_id": group_id,
        "group_id": group_id,
        "data_project": friendly_group,          # collection folder name (e.g. Sierksma2025)
        "basename": db.get('basename'),           # .md filename stem (from API when available)
        "dataset_name": dataset_title,
        "file_id": file_id,
        "db_id": db_id,
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
        "module_scores": module_scores,
        "module_scores_by_catalog": module_scores_by_catalog,
        "n_module_scores": n_modules,
        "score_method_counts": method_counts,
        "top_genes_fallback": top_genes_fallback,
        "has_duckdb": True,
        "update_pending": False,
        "de_studies": de_studies,
        "de_total_contrasts": de_total_contrasts,
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
            # dataset carry any canonical (`ant_*`) obs columns at all?
            # No canonical tags → variability cannot be computed for it, so
            # the message needs to say that plainly rather than "computing…".
            "has_canonical_tags": any(
                str(col).startswith("ant_")
                for col in (obs.get("categorical", []) or [])
                          + (obs.get("numerical", []) or [])
            ),
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


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    """User-preferences page (per-browser, localStorage-only).

    Renders a shell with two controls:

    - **Module scoring algorithm** — scanpy (default) vs AUCell. Read by the
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
    })


@router.get("/genesets", response_class=HTMLResponse)
async def genesets_index(request: Request):
    api_url = _api_url(request)
    try:
        payload = _api_get(api_url, '/genesets')
        genesets = payload.get('genesets', [])
    except Exception as error:
        print(f"API error: {error}")
        genesets = []

    # Group by study → experiment for display
    by_study: Dict[str, Dict[str, list]] = {}
    for geneset in genesets:
        by_study.setdefault(geneset['study'], {}).setdefault(
            geneset['experiment'], []
        ).append(geneset)

    active_group = request.query_params.get("group")

    # starCAT tab is now enabled — catalogs are available and the browser
    # handles the module/score detail load when active_group == 'starcat'.
    STARCAT_UI_ENABLED = True

    # starCAT programs are a SECOND module source, served by the /catalogs
    # endpoint (REST 2.30) — the authoritative definitions (modules with gene
    # weights + group + biological description, plus derived score recipes).
    # /catalogs lists all catalogs cheaply (for the tab count); the per-catalog
    # module/score detail is only fetched when the starCAT group is active.
    starcat_summaries: list = []
    starcat_detail: list = []
    if STARCAT_UI_ENABLED:
        try:
            catalogs = _api_get(api_url, '/catalogs').get('catalogs', [])
            starcat_summaries = [c for c in catalogs if c.get('source') == 'starcat']
        except Exception as error:
            print(f"API error (/catalogs): {error}")

    starcat_total = sum(c.get('n_modules', 0) for c in starcat_summaries)

    if active_group == 'starcat':
        for summary in starcat_summaries:
            try:
                detail = _api_get(api_url, f"/catalogs/starcat/{summary['catalog_id']}")
            except Exception as error:
                print(f"API error (/catalogs/{summary.get('catalog_id')}): {error}")
                continue
            # group modules by their curated `group` for display
            by_group: Dict[str, list] = {}
            for module in detail.get('modules', []):
                by_group.setdefault(module.get('group') or 'other', []).append(module)
            starcat_detail.append({
                "catalog_id": summary['catalog_id'],
                "name": detail.get('name') or summary.get('name') or summary['catalog_id'],
                "description": detail.get('description') or summary.get('description'),
                "n_modules": detail.get('n_modules'),
                "n_scores": detail.get('n_scores'),
                "n_genes": detail.get('n_genes'),
                "modules_by_group": dict(sorted(by_group.items())),
                "scores": detail.get('scores', []),
            })

    # Group selector: one tab per module source (gene sets / starCAT; more to
    # follow). Only groups that actually have content are offered. The active
    # group comes from ?group=, defaulting to the first available.
    groups = []
    if by_study:
        groups.append({"key": "genesets", "label": "Gene sets", "count": len(genesets)})
    if starcat_summaries:
        groups.append({"key": "starcat", "label": "starCAT", "count": starcat_total})
    if by_study or starcat_summaries:
        # reverse lookup: which sets/programs contain a given gene (client-side
        # search against GET /genesets/by-gene/{gene}). No count.
        groups.append({"key": "bygene", "label": "🔍 find by gene", "count": None})
    valid_keys = {g["key"] for g in groups}
    if active_group not in valid_keys:
        active_group = groups[0]["key"] if groups else "genesets"

    return templates.TemplateResponse(request, "genesets.html", {
        "by_study": by_study,
        "total": len(genesets),
        "starcat_detail": starcat_detail,
        "starcat_total": starcat_total,
        "groups": groups,
        "active_group": active_group,
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
    return templates.TemplateResponse(request, "geneset_variability.html", {
        "canonical_tags": list(CANONICAL_TAGS),
        "numerical_tags": sorted(NUMERICAL_TAGS),
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
