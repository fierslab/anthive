"""
Web browser routes for Anthive

Provides HTML interface for browsing single-cell datasets.
Data is sourced entirely from the ant-serve REST API.
"""

import base64
import html as html_lib
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote as url_quote, unquote, urlencode

import markdown
import requests
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

router = APIRouter()
templates_dir = Path(__file__).parent / 'templates'
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.filters['urlencode'] = url_quote

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


def _reports(request: Request) -> list:
    return getattr(request.app.state, 'reports', [])


# ── misc helpers ───────────────────────────────────────────────────────────────

def _as_list(value) -> list:
    """Normalise a scalar-or-list field to a Python list."""
    if not value:
        return []
    return value if isinstance(value, list) else [value]


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
            'organism':        _summarize_meta_field([db], 'organism',        abbrev_map=_ORGANISM_ABBREV),
            'technology':      _summarize_meta_field([db], 'technology',      abbrev_map=_TECHNOLOGY_ABBREV),
            'celltype':        _summarize_meta_field([db], 'celltype',        abbrev_map=_CELLTYPE_ABBREV),
            'suspension_type': _summarize_meta_field([db], 'suspension_type', abbrev_map=_SUSPENSION_ABBREV),
            'tissue':          _summarize_meta_field([db], 'tissue',          abbrev_map=_TISSUE_ABBREV),
            'disease':         _summarize_meta_field([db], 'disease',         abbrev_map=_DISEASE_ABBREV),
            'access':          _summarize_meta_field([db], 'access',          abbrev_map=_ACCESS_ABBREV),
            'source':          _summarize_meta_field([db], 'source',          abbrev_map=_SOURCE_ABBREV),
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
    header_pills: List[Dict] = []
    var_fields: List[str] = []
    for field, abbrev_map, css, label in _coll_fields:
        summary = _summarize_meta_field(group_dbs, field, abbrev_map=abbrev_map)
        abbrev, full = summary['abbrev'], summary['full']
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

    # Newest-first: most-recently-modified dataset on top (lexical ISO sort).
    datasets.sort(key=lambda d: (d.get('modified') or '', d['name']), reverse=True)

    collection_meta = {'pills': header_pills, 'var_fields': var_fields}
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

    return templates.TemplateResponse(request, "index.html", {
        "folders": collections,
        "total_datasets": total_datasets,
        "total_folders": len(collections),
        "total_cells": total_cells,
        "filter_options": filter_options,
    })


def _is_admin(request: Request) -> bool:
    """True when Authelia forwarded a Remote-Groups header containing 'admins'.
    The proxy (Caddy/nginx) sets it from the SSO session; un-proxied access has
    no header → not admin. This only gates SHOWING/serving the admin UI — the
    authoritative write gate is server-side in ant-serve."""
    groups = request.headers.get('remote-groups') or ''
    return 'admins' in [g.strip() for g in groups.split(',') if g.strip()]


_H5ADSTORE_ROOT = Path(__file__).parent.parent / 'data' / 'h5adstore'


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


def _group_static_files(group_id: str) -> List[Dict]:
    """List files under data/h5adstore/{group}/static/ eligible for download.

    Returns a list of ``{name, size_bytes}`` sorted by name. Empty when the
    folder does not exist or contains only hidden entries.
    """
    static_dir = _H5ADSTORE_ROOT / group_id / 'static'
    try:
        if not static_dir.is_dir():
            return []
    except OSError:
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
    static_dir = (_H5ADSTORE_ROOT / group_id / 'static').resolve()
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
    embeddings = list(db.get('embeddings', {}).keys())
    obs = db.get('obs', {})

    # Split numerical obs columns into module-score triples (grouped by
    # (catalog, module) so scanpy/starcat/aucell for the same module collapse
    # to one row with per-method chips) vs everything else.
    score_grouped = _group_module_scores(obs.get('numerical', []) or [])
    module_scores = score_grouped['modules']
    method_counts = score_grouped['method_counts']

    obs_columns = (
        [{'name': col, 'type': 'num'} for col in score_grouped['non_score_cols']] +
        [{'name': col, 'type': 'cat'} for col in obs.get('categorical', [])]
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
    dataset_meta = {
        # Biology
        'organism':        db.get('organism'),
        'tissue':          _as_list(db.get('tissue')),
        'celltype':        db.get('cell_type') or db.get('celltype'),
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
        'top_genes':       _as_list(db.get('top_genes')),
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
    n_cat = len(obs.get('categorical') or [])
    # n_num = the plain (non-score) numerical columns shown in the Numerical
    # tab; module-score columns get their own "Module scores" tab now.
    n_num = len(score_grouped['non_score_cols'])
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
        "layer_stats_rows": layer_stats_rows,
        "dataset_meta": dataset_meta,
        "context_bar": context_bar,
        "n_cat_cols": n_cat,
        "n_num_cols": n_num,
        "module_scores": module_scores,
        "n_module_scores": n_modules,
        "score_method_counts": method_counts,
        "has_duckdb": True,
        "update_pending": False,
        "de_studies": de_studies,
        "de_total_contrasts": de_total_contrasts,
        "caveats": caveats,
        "caveats_summary": caveats_summary,
        "is_admin": _is_admin(request),
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
    return templates.TemplateResponse(request, "genes.html", {
        "gene": gene,
        "pill_map": pill_map,
        "filter_opts": filter_opts,
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

    # starCAT programs are a SECOND module source, served by the /catalogs
    # endpoint (REST 2.30) — the authoritative definitions (modules with gene
    # weights + group + biological description, plus derived score recipes).
    # /catalogs lists all catalogs cheaply (for the tab count); the per-catalog
    # module/score detail is only fetched when the starCAT group is active.
    starcat_summaries: list = []
    starcat_detail: list = []
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
    return templates.TemplateResponse(request, "geneset_variability.html", {})


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

    return templates.TemplateResponse(request, "geneset_experiment.html", {
        "study": study,
        "experiment": experiment,
        "metadata": payload.get('metadata', {}),
        "body_html": body_html,
        "genesets": payload.get('genesets', []),
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

    return templates.TemplateResponse(request, "geneset.html", {
        "study": study,
        "experiment": experiment,
        "name": name,
        "payload": payload,
        "geneset_body_html": geneset_body_html,
        "experiment_body_html": experiment_body_html,
    })
