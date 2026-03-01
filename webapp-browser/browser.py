"""
Web browser routes for Anthive

Provides HTML interface for browsing h5ad datasets
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

# Router for HTML browser routes
router = APIRouter()

# Templates directory
templates_dir = script_dir / 'templates'
templates = Jinja2Templates(directory=str(templates_dir))


def get_folder_list(data_folder: Path) -> List[Dict]:
    """Get list of top-level folders with dataset counts"""
    folders = []

    for folder_path in sorted(data_folder.iterdir()):
        if not folder_path.is_dir():
            continue
        if folder_path.name.startswith('.'):
            continue

        # Count h5ad files in this folder (including subfolders)
        h5ad_files = list(folder_path.rglob('*.h5ad'))
        dataset_count = len(h5ad_files)

        if dataset_count == 0:
            continue

        # Check conversion status
        duckdb_files = list(folder_path.rglob('*.duckdb'))
        all_converted = len(duckdb_files) == dataset_count
        some_converted = len(duckdb_files) > 0

        # Try to get description from index.md
        index_md = folder_path / 'index.md'
        description = None
        if index_md.exists():
            try:
                metadata, body = parse_md_file(index_md)
                # Extract first paragraph from markdown
                lines = body.strip().split('\n')
                for line in lines:
                    if line.strip() and not line.startswith('#'):
                        description = line.strip()[:100]
                        break
            except:
                pass

        # Calculate total cells from metadata files
        total_cells = 0
        for h5ad_file in h5ad_files:
            md_file = h5ad_file.with_suffix('.md')
            if md_file.exists():
                try:
                    metadata, _ = parse_md_file(md_file)
                    n_obs = metadata.get('n_obs')
                    if n_obs is not None:
                        total_cells += n_obs
                except:
                    pass
        total_cells = total_cells if total_cells > 0 else None

        folders.append({
            'name': folder_path.name,
            'url_encoded': quote(folder_path.name, safe=''),
            'dataset_count': dataset_count,
            'total_cells': total_cells,
            'all_converted': all_converted,
            'some_converted': some_converted,
            'description': description
        })

    return folders


def get_datasets_in_folder(data_folder: Path, folder_name: str) -> List[Dict]:
    """Get list of datasets in a specific folder"""
    # Decode folder name from URL
    folder_name_decoded = unquote(folder_name)
    folder_path = data_folder / folder_name_decoded

    if not folder_path.exists() or not folder_path.is_dir():
        return []

    datasets = []

    for h5ad_file in sorted(folder_path.rglob('*.h5ad')):
        # Get relative path within folder
        rel_path = h5ad_file.relative_to(folder_path)
        subfolder = str(rel_path.parent) if rel_path.parent != Path('.') else None

        # Check for metadata
        md_file = h5ad_file.with_suffix('.md')
        duckdb_file = h5ad_file.with_suffix('.duckdb')

        has_metadata = md_file.exists()
        has_duckdb = duckdb_file.exists()

        # Load basic info from metadata
        n_obs, n_vars, layers, embeddings = None, None, None, None
        if has_metadata:
            try:
                metadata, _ = parse_md_file(md_file)
                # Get info from metadata structure (this matches ant-check output)
                n_obs = metadata.get('n_obs')
                n_vars = metadata.get('n_vars')
                layers = list(metadata.get('layers_include', {}).keys()) if metadata.get('layers_include') else []
                embeddings = list(metadata.get('obsm_include', {}).keys()) if metadata.get('obsm_include') else []
            except:
                pass

        # Format file size
        h5ad_size = None
        if h5ad_file.exists():
            size_bytes = h5ad_file.stat().st_size
            h5ad_size = format_size(size_bytes)

        datasets.append({
            'name': h5ad_file.stem,  # filename without .h5ad
            'url_encoded': quote(str(rel_path.with_suffix('')), safe=''),  # path without extension
            'subfolder': subfolder,
            'n_obs': n_obs,
            'n_vars': n_vars,
            'layers': layers,
            'embeddings': embeddings,
            'h5ad_size': h5ad_size,
            'has_metadata': has_metadata,
            'has_duckdb': has_duckdb
        })

    return datasets


def format_size(size_bytes: int) -> str:
    """Format file size for display"""
    size: float = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Home page - list of folders"""
    data_folder = request.app.state.data_folder

    folders = get_folder_list(data_folder)

    total_datasets = sum(f['dataset_count'] for f in folders)
    total_folders = len(folders)
    total_cells = sum(f['total_cells'] for f in folders if f['total_cells'])

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "folders": folders,
            "total_datasets": total_datasets,
            "total_folders": total_folders,
            "total_cells": total_cells if total_cells else None
        }
    )


@router.get("/browse/{folder_name}", response_class=HTMLResponse)
async def folder_view(request: Request, folder_name: str):
    """Folder detail page - list of datasets in folder"""
    data_folder = request.app.state.data_folder

    # Decode folder name
    folder_name_decoded = unquote(folder_name)
    folder_path = data_folder / folder_name_decoded

    if not folder_path.exists():
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "folders": [], "error": "Folder not found"},
            status_code=404
        )

    datasets = get_datasets_in_folder(data_folder, folder_name)

    # Try to get folder description
    description = None
    index_md = folder_path / 'index.md'
    if index_md.exists():
        try:
            metadata, body = parse_md_file(index_md)
            lines = body.strip().split('\n')
            for line in lines:
                if line.strip() and not line.startswith('#'):
                    description = line.strip()
                    break
        except:
            pass

    return templates.TemplateResponse(
        "folder.html",
        {
            "request": request,
            "folder_name": folder_name_decoded,
            "folder_url": folder_name,  # Keep encoded for URLs
            "description": description,
            "datasets": datasets
        }
    )


@router.get("/browse/{folder_name}/{dataset_path:path}", response_class=HTMLResponse)
async def dataset_view(request: Request, folder_name: str, dataset_path: str):
    """Dataset detail page"""
    data_folder = request.app.state.data_folder

    # Decode paths
    folder_name_decoded = unquote(folder_name)
    dataset_path_decoded = unquote(dataset_path)

    # Construct full path to h5ad file
    h5ad_file = data_folder / folder_name_decoded / f"{dataset_path_decoded}.h5ad"

    if not h5ad_file.exists():
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "folders": [], "error": "Dataset not found"},
            status_code=404
        )

    # Load metadata
    md_file = h5ad_file.with_suffix('.md')
    duckdb_file = h5ad_file.with_suffix('.duckdb')

    metadata_dict = {}
    md_body = ""
    markdown_content = None
    if md_file.exists():
        try:
            metadata_dict, md_body = parse_md_file(md_file)
            # Convert markdown to HTML
            markdown_content = markdown.markdown(md_body, extensions=['tables', 'fenced_code'])
        except Exception as e:
            markdown_content = f"<p>Error loading metadata: {e}</p>"

    # Get info from API (reuse existing get_database_info logic)
    # For now, extract from metadata
    info = None
    obs_columns = []
    if metadata_dict:
        # Build info structure
        info = {
            'n_cells': metadata_dict.get('n_obs'),  # Get from metadata if available
            'n_genes': metadata_dict.get('n_vars'),  # Get from metadata if available
            'n_obs_columns': 0,
            'n_var_columns': 0,
            'layers': list(metadata_dict.get('layers_include', {}).keys()),
            'obsm_keys': list(metadata_dict.get('obsm_include', {}).keys())
        }

        # Fallback: try to extract n_cells and n_genes from markdown content
        # for older metadata files that don't have these fields
        if (info['n_cells'] is None or info['n_genes'] is None) and md_body:
            import re
            # Look for pattern like "12,345 cells × 67,890 genes" in markdown
            match = re.search(r'(\d{1,3}(?:,\d{3})*)\s+cells\s*×\s*(\d{1,3}(?:,\d{3})*)\s+genes', md_body)
            if match:
                try:
                    if info['n_cells'] is None:
                        info['n_cells'] = int(match.group(1).replace(',', ''))
                    if info['n_genes'] is None:
                        info['n_genes'] = int(match.group(2).replace(',', ''))
                except ValueError:
                    pass

        # Build obs columns list
        obsnum = metadata_dict.get('obsnum_include', [])
        obscat = metadata_dict.get('obscat_include', [])
        for col in obsnum:
            obs_columns.append({'name': col, 'type': 'numerical', 'unique': None})
        for col in obscat:
            obs_columns.append({'name': col, 'type': 'categorical', 'unique': None})

        info['n_obs_columns'] = len(obs_columns)

    # Create database ID for API links (folder/dataset format, URL-encoded)
    db_id = f"{folder_name}/{quote(dataset_path_decoded, safe='')}"

    return templates.TemplateResponse(
        "dataset.html",
        {
            "request": request,
            "folder_name": folder_name_decoded,
            "folder_url": folder_name,
            "dataset_name": h5ad_file.stem,
            "db_id": db_id,
            "info": info,
            "markdown_content": markdown_content,
            "obs_columns": obs_columns,
            "has_duckdb": duckdb_file.exists()
        }
    )
