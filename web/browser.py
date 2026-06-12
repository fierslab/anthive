"""
Web browser routes for Anthive

Provides HTML interface for browsing single-cell datasets.
Data is sourced entirely from the ant-serve REST API.
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import quote as url_quote, unquote

import markdown
import requests
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

router = APIRouter()
templates_dir = Path(__file__).parent / 'templates'
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.filters['urlencode'] = url_quote


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


# Register Jinja2 helpers — templates use {{ value|short_count }} / {{ format_count(n) }}
templates.env.filters['short_count'] = _short_count
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
    """Return (datasets, description_html) for a collection."""
    databases = _all_databases(api_url)

    datasets = []
    description_html = None

    for db in databases:
        if (db.get('group_id') or db['id'].split('/')[0]) != group_id:
            continue

        if description_html is None:
            experiment = (db.get('collection') or {}).get('experiment')
            if experiment:
                description_html = markdown.markdown(
                    experiment, extensions=['tables', 'fenced_code']
                )

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
        })

    return datasets, description_html


# ── routes ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        collections = get_collection_list(_api_url(request))
    except Exception as error:
        collections = []
        print(f"API error: {error}")

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


@router.get("/browse/{browse_id}", response_class=HTMLResponse)
async def folder_view(request: Request, browse_id: str):
    group_id = unquote(browse_id)

    try:
        datasets, description_html = get_collection_datasets(_api_url(request), group_id)
    except Exception as error:
        print(f"API error: {error}")
        datasets, description_html = [], None

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

    return templates.TemplateResponse(request, "folder.html", {
        "folder_name": friendly_group or group_id,
        "folder_browse_id": group_id,
        "group_id": group_id,
        "description_html": description_html,
        "datasets": datasets,
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
    obs_columns = (
        [{'name': col, 'type': 'num'} for col in obs.get('numerical', [])] +
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

    raw_md = db.get('markdown')
    markdown_content = markdown.markdown(raw_md, extensions=['tables', 'fenced_code']) if raw_md else None

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
    n_num = len(obs.get('numerical') or [])

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

    return templates.TemplateResponse(request, "dataset.html", {
        "folder_name": friendly_group,
        "folder_browse_id": group_id,
        "group_id": group_id,
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
        "has_duckdb": True,
        "update_pending": False,
        "de_studies": de_studies,
        "de_total_contrasts": de_total_contrasts,
    })


# ── geneset routes ─────────────────────────────────────────────────────────────

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

    return templates.TemplateResponse(request, "genesets.html", {
        "by_study": by_study,
        "total": len(genesets),
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
