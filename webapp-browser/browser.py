"""
Web browser routes for Anthive

Provides HTML interface for browsing h5ad datasets.
URLs use short IDs (group_id/file_id) where available, with fallback to folder/file names.
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, unquote

import markdown
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Add lib directory to path
script_dir = Path(__file__).parent.resolve()
lib_dir = script_dir.parent / 'lib'
sys.path.insert(0, str(lib_dir))

from metadata import parse_md_file

router = APIRouter()
templates_dir = script_dir / 'templates'
templates = Jinja2Templates(directory=str(templates_dir))


def format_number(value: float) -> str:
    """Format a number for display (no numpy dependency)."""
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


# ── lookup helpers ─────────────────────────────────────────────────────────────

def _read_group_id(folder_path: Path) -> Optional[str]:
    """Read group_id from folder's index.md, or None."""
    index_md = folder_path / 'index.md'
    if not index_md.exists():
        return None
    try:
        meta, _ = parse_md_file(index_md)
        val = meta.get('group_id')
        return str(val).strip() if val else None
    except Exception:
        return None


def _read_file_id(md_file: Path) -> Optional[str]:
    """Read id from a dataset .md file, or None."""
    if not md_file.exists():
        return None
    try:
        meta, _ = parse_md_file(md_file)
        val = meta.get('id')
        return str(val).strip() if val else None
    except Exception:
        return None


def _find_folder(data_folder: Path, browse_id: str) -> Optional[Path]:
    """Resolve browse_id to a folder path.

    Tries in order:
      1. Direct match as folder name (fast path, no I/O)
      2. Scan folders for group_id match in index.md
    """
    direct = data_folder / browse_id
    if direct.is_dir():
        return direct
    for folder_path in sorted(data_folder.iterdir()):
        if not folder_path.is_dir() or folder_path.name.startswith('.'):
            continue
        if _read_group_id(folder_path) == browse_id:
            return folder_path
    return None


def _find_dataset(folder_path: Path, browse_id: str) -> Optional[Path]:
    """Resolve browse_id to an h5ad file within a folder.

    Tries in order:
      1. Direct relative path match (backwards compat with old URLs)
      2. Scan .md files for id match
    """
    direct = folder_path / f"{browse_id}.h5ad"
    if direct.exists():
        return direct
    for h5ad_file in folder_path.rglob('*.h5ad'):
        if _read_file_id(h5ad_file.with_suffix('.md')) == browse_id:
            return h5ad_file
    return None



# ── status helpers ─────────────────────────────────────────────────────────────

def _is_update_pending(h5ad_file: Path) -> bool:
    """Return True if duckdb exists but is older than h5ad or md."""
    duckdb_file = h5ad_file.with_suffix('.duckdb')
    if not duckdb_file.exists():
        return False
    duckdb_mtime = duckdb_file.stat().st_mtime
    if h5ad_file.stat().st_mtime > duckdb_mtime:
        return True
    md_file = h5ad_file.with_suffix('.md')
    return md_file.exists() and md_file.stat().st_mtime > duckdb_mtime


# ── data builders ──────────────────────────────────────────────────────────────

def get_folder_list(data_folder: Path) -> List[Dict]:
    """Get list of top-level folders, sorted by most recently updated h5ad."""
    folders = []

    for folder_path in sorted(data_folder.iterdir()):
        if not folder_path.is_dir() or folder_path.name.startswith('.'):
            continue

        h5ad_files = list(folder_path.rglob('*.h5ad'))
        if not h5ad_files:
            continue

        duckdb_files = list(folder_path.rglob('*.duckdb'))
        duckdb_count = len(duckdb_files)
        dataset_count = len(h5ad_files)
        update_pending = sum(1 for f in h5ad_files if _is_update_pending(f))
        last_modified = max((f.stat().st_mtime for f in h5ad_files), default=0)

        group_id = _read_group_id(folder_path)
        browse_id = group_id if group_id else quote(folder_path.name, safe='')

        description = None
        index_md = folder_path / 'index.md'
        if index_md.exists():
            try:
                _, body = parse_md_file(index_md)
                for line in body.strip().split('\n'):
                    if line.strip() and not line.startswith('#'):
                        description = line.strip()[:120]
                        break
            except Exception:
                pass

        total_cells = 0
        for h5ad_file in h5ad_files:
            md_file = h5ad_file.with_suffix('.md')
            if md_file.exists():
                try:
                    meta, _ = parse_md_file(md_file)
                    total_cells += meta.get('n_obs') or 0
                except Exception:
                    pass

        folders.append({
            'name': folder_path.name,
            'group_id': group_id,
            'browse_id': browse_id,
            'dataset_count': dataset_count,
            'duckdb_count': duckdb_count,
            'total_cells': total_cells or None,
            'all_converted': duckdb_count == dataset_count,
            'some_converted': duckdb_count > 0,
            'update_pending': update_pending,
            'last_modified': last_modified,
            'description': description,
        })

    folders.sort(key=lambda f: f['last_modified'], reverse=True)
    return folders


def get_datasets_in_folder(folder_path: Path) -> List[Dict]:
    """Get list of datasets in a folder."""
    datasets = []

    for h5ad_file in sorted(folder_path.rglob('*.h5ad')):
        rel_path = h5ad_file.relative_to(folder_path)
        subfolder = str(rel_path.parent) if rel_path.parent != Path('.') else None

        md_file = h5ad_file.with_suffix('.md')
        duckdb_file = h5ad_file.with_suffix('.duckdb')

        has_metadata = md_file.exists()
        has_duckdb = duckdb_file.exists()
        update_pending = _is_update_pending(h5ad_file)

        n_obs, n_vars, layers, embeddings, file_id = None, None, [], [], None
        if has_metadata:
            try:
                meta, _ = parse_md_file(md_file)
                n_obs = meta.get('n_obs')
                n_vars = meta.get('n_vars')
                layers = list(meta.get('layers_include') or [])
                embeddings = list(meta.get('obsm_include') or [])
                file_id = _read_file_id(md_file)
            except Exception:
                pass

        browse_id = file_id if file_id else quote(str(rel_path.with_suffix('')), safe='')
        h5ad_size = format_size(h5ad_file.stat().st_size)

        datasets.append({
            'name': h5ad_file.stem,
            'file_id': file_id,
            'browse_id': browse_id,
            'subfolder': subfolder,
            'n_obs': n_obs,
            'n_vars': n_vars,
            'layers': layers,
            'embeddings': embeddings,
            'h5ad_size': h5ad_size,
            'has_metadata': has_metadata,
            'has_duckdb': has_duckdb,
            'update_pending': update_pending,
        })

    return datasets


def format_size(size_bytes: int) -> str:
    size: float = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _format_layer_stats(raw_stats: dict) -> List[Dict]:
    rows = []
    for layer_name, stats in raw_stats.items():
        rows.append({
            'name': layer_name,
            'dtype': stats.get('dtype', ''),
            'min': format_number(stats.get('min_value', 0)),
            'min_nz': format_number(stats.get('min_nonzero', 0)),
            'max': format_number(stats.get('max_value', 0)),
            'median': format_number(stats.get('median', 0)),
            'q90': format_number(stats.get('q90', 0)),
            'pct_zeros': f"{stats.get('pct_zeros', 0):.1f}",
        })
    return rows


# ── routes ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    data_folder = request.app.state.data_folder
    folders = get_folder_list(data_folder)
    total_datasets = sum(f['dataset_count'] for f in folders)
    total_folders = len(folders)
    total_cells = sum(f['total_cells'] for f in folders if f['total_cells']) or None

    return templates.TemplateResponse("index.html", {
        "request": request,
        "folders": folders,
        "total_datasets": total_datasets,
        "total_folders": total_folders,
        "total_cells": total_cells,
    })


@router.get("/browse/{browse_id}", response_class=HTMLResponse)
async def folder_view(request: Request, browse_id: str):
    data_folder = request.app.state.data_folder
    browse_id_decoded = unquote(browse_id)

    folder_path = _find_folder(data_folder, browse_id_decoded)
    if not folder_path:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "folders": [], "error": f"Collection '{browse_id_decoded}' not found"},
            status_code=404
        )

    datasets = get_datasets_in_folder(folder_path)
    group_id = _read_group_id(folder_path)
    folder_browse_id = group_id if group_id else quote(folder_path.name, safe='')

    description_html = None
    index_md = folder_path / 'index.md'
    if index_md.exists():
        try:
            _, body = parse_md_file(index_md)
            if body.strip():
                description_html = markdown.markdown(body, extensions=['tables', 'fenced_code'])
        except Exception:
            pass

    return templates.TemplateResponse("folder.html", {
        "request": request,
        "folder_name": folder_path.name,
        "folder_browse_id": folder_browse_id,
        "group_id": group_id,
        "description_html": description_html,
        "datasets": datasets,
    })


@router.get("/browse/{folder_browse_id}/{dataset_browse_id:path}", response_class=HTMLResponse)
async def dataset_view(request: Request, folder_browse_id: str, dataset_browse_id: str):
    data_folder = request.app.state.data_folder

    folder_browse_id_decoded = unquote(folder_browse_id)
    dataset_browse_id_decoded = unquote(dataset_browse_id)

    folder_path = _find_folder(data_folder, folder_browse_id_decoded)
    if not folder_path:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "folders": [], "error": "Collection not found"},
            status_code=404
        )

    h5ad_file = _find_dataset(folder_path, dataset_browse_id_decoded)
    if not h5ad_file:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "folders": [], "error": "Dataset not found"},
            status_code=404
        )

    md_file = h5ad_file.with_suffix('.md')
    duckdb_file = h5ad_file.with_suffix('.duckdb')

    metadata_dict = {}
    md_body = ""
    markdown_content = None
    if md_file.exists():
        try:
            metadata_dict, md_body = parse_md_file(md_file)
            markdown_content = markdown.markdown(md_body, extensions=['tables', 'fenced_code'])
        except Exception as e:
            markdown_content = f"<p>Error loading metadata: {e}</p>"

    info = None
    obs_columns = []
    layer_stats_rows = []
    if metadata_dict:
        layers = list(metadata_dict.get('layers_include') or [])
        obsm_keys = list(metadata_dict.get('obsm_include') or [])
        file_id = _read_file_id(md_file)
        group_id = _read_group_id(folder_path)

        info = {
            'n_cells': metadata_dict.get('n_obs'),
            'n_genes': metadata_dict.get('n_vars'),
            'n_obs_columns': 0,
            'layers': layers,
            'obsm_keys': obsm_keys,
            'file_id': file_id,
            'group_id': group_id,
            'title': metadata_dict.get('title'),
        }

        if (info['n_cells'] is None or info['n_genes'] is None) and md_body:
            import re
            match = re.search(r'(\d{1,3}(?:,\d{3})*)\s+cells\s*[×x]\s*(\d{1,3}(?:,\d{3})*)\s+genes', md_body)
            if match:
                try:
                    if info['n_cells'] is None:
                        info['n_cells'] = int(match.group(1).replace(',', ''))
                    if info['n_genes'] is None:
                        info['n_genes'] = int(match.group(2).replace(',', ''))
                except ValueError:
                    pass

        for col in metadata_dict.get('obsnum_include', []):
            obs_columns.append({'name': col, 'type': 'num'})
        for col in metadata_dict.get('obscat_include', []):
            obs_columns.append({'name': col, 'type': 'cat'})
        info['n_obs_columns'] = len(obs_columns)

        raw_stats = metadata_dict.get('layer_stats', {})
        if raw_stats:
            layer_stats_rows = _format_layer_stats(raw_stats)

    # Build canonical short db_id for API links
    file_id = _read_file_id(md_file)
    group_id = _read_group_id(folder_path)
    if group_id and file_id:
        db_id = f"{group_id}/{file_id}"
    else:
        rel = h5ad_file.relative_to(data_folder)
        db_id = str(rel.with_suffix(''))

    folder_browse_id_clean = group_id if group_id else quote(folder_path.name, safe='')

    return templates.TemplateResponse("dataset.html", {
        "request": request,
        "folder_name": folder_path.name,
        "folder_browse_id": folder_browse_id_clean,
        "group_id": group_id,
        "dataset_name": h5ad_file.stem,
        "db_id": db_id,
        "info": info,
        "markdown_content": markdown_content,
        "obs_columns": obs_columns,
        "layer_stats_rows": layer_stats_rows,
        "has_duckdb": duckdb_file.exists(),
        "update_pending": _is_update_pending(h5ad_file),
    })
