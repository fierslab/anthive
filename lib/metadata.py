"""
Metadata management for anthive

Functions for reading and writing .md files with YAML preambles
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


class MetadataError(Exception):
    """Metadata-related errors"""
    pass


def parse_md_file(md_path: Path) -> Tuple[Dict[str, Any], str]:
    """
    Parse markdown file with YAML preamble

    Args:
        md_path: Path to .md file

    Returns:
        Tuple of (yaml_dict, markdown_body)
        If file doesn't exist, returns (empty dict, empty string)
    """
    if not md_path.exists():
        return {}, ""

    try:
        with open(md_path, 'r') as file:
            content = file.read()

        # Check for YAML preamble delimiters
        if not content.startswith('---\n'):
            # No preamble, entire file is markdown
            return {}, content

        # Find end of preamble
        match = re.search(r'^---\n(.*?)\n---\n', content, re.DOTALL | re.MULTILINE)

        if not match:
            # Malformed preamble
            return {}, content

        yaml_content = match.group(1)
        markdown_body = content[match.end():]

        # Parse YAML
        yaml_dict = yaml.safe_load(yaml_content)
        if yaml_dict is None:
            yaml_dict = {}

        return yaml_dict, markdown_body.strip()

    except yaml.YAMLError as error:
        raise MetadataError(
            f"ERROR: [Metadata] Failed to parse YAML preamble\n"
            f"Details: Invalid YAML syntax in {md_path}\n"
            f"Location: {md_path}\n"
            f"Error: {error}\n"
            f"Action: Fix YAML syntax or regenerate metadata file"
        )
    except Exception as error:
        raise MetadataError(
            f"ERROR: [Metadata] Failed to read metadata file\n"
            f"Location: {md_path}\n"
            f"Error: {error}"
        )


def write_md_file(md_path: Path, yaml_dict: Dict[str, Any], markdown_body: str) -> None:
    """
    Write markdown file with YAML preamble

    Args:
        md_path: Path to .md file
        yaml_dict: Dictionary to write as YAML preamble
        markdown_body: Markdown content
    """
    try:
        with open(md_path, 'w') as file:
            file.write('---\n')
            yaml.dump(yaml_dict, file, default_flow_style=False, sort_keys=False, allow_unicode=True)
            file.write('---\n\n')
            file.write(markdown_body)
            if not markdown_body.endswith('\n'):
                file.write('\n')
    except Exception as error:
        raise MetadataError(
            f"ERROR: [Metadata] Failed to write metadata file\n"
            f"Location: {md_path}\n"
            f"Error: {error}"
        )


def ensure_in_list(yaml_dict: Dict[str, Any], key: str, value: str) -> bool:
    """
    Ensure value is in list, add if missing

    Args:
        yaml_dict: Metadata dictionary
        key: List key
        value: Value to ensure exists

    Returns:
        True if value was added, False if already present
    """
    if key not in yaml_dict:
        yaml_dict[key] = []

    if not isinstance(yaml_dict[key], list):
        yaml_dict[key] = []

    if value not in yaml_dict[key]:
        yaml_dict[key].append(value)
        return True

    return False


def ensure_in_mapping(yaml_dict: Dict[str, Any], key: str, name: str, default_value: str) -> bool:
    """
    Ensure name is in mapping, add with default value if missing

    Args:
        yaml_dict: Metadata dictionary
        key: Mapping key
        name: Name to ensure exists
        default_value: Default value if adding

    Returns:
        True if name was added, False if already present
    """
    if key not in yaml_dict:
        yaml_dict[key] = {}

    if not isinstance(yaml_dict[key], dict):
        yaml_dict[key] = {}

    if name not in yaml_dict[key]:
        yaml_dict[key][name] = default_value
        return True

    return False


def remove_from_lists(yaml_dict: Dict[str, Any], list_keys: List[str], value: str) -> None:
    """
    Remove value from all specified lists if present

    Used to enforce mutual exclusivity

    Args:
        yaml_dict: Metadata dictionary
        list_keys: List of keys to check/remove from
        value: Value to remove
    """
    for key in list_keys:
        if key in yaml_dict and isinstance(yaml_dict[key], list):
            if value in yaml_dict[key]:
                yaml_dict[key].remove(value)


def get_obs_column_assignment(yaml_dict: Dict[str, Any], column_name: str) -> Optional[str]:
    """
    Get which list an obs column is assigned to

    Args:
        yaml_dict: Metadata dictionary
        column_name: Column name to check

    Returns:
        'obsnum_include', 'obscat_include', 'obs_exclude', or None
    """
    if 'obsnum_include' in yaml_dict and column_name in yaml_dict['obsnum_include']:
        return 'obsnum_include'
    if 'obscat_include' in yaml_dict and column_name in yaml_dict['obscat_include']:
        return 'obscat_include'
    if 'obs_exclude' in yaml_dict and column_name in yaml_dict['obs_exclude']:
        return 'obs_exclude'
    return None


def add_to_obs_exclude(yaml_dict: Dict[str, Any], column_name: str) -> bool:
    """
    Add column to obs_exclude if not already in any obs list

    Args:
        yaml_dict: Metadata dictionary
        column_name: Column to exclude

    Returns:
        True if added, False if already in a list
    """
    existing = get_obs_column_assignment(yaml_dict, column_name)
    if existing:
        return False

    ensure_in_list(yaml_dict, 'obs_exclude', column_name)
    return True


def get_default_embedding_name(obsm_key: str) -> str:
    """
    Get default friendly name for embedding

    Removes 'X_' prefix if present and capitalizes

    Args:
        obsm_key: Original obsm key (e.g., 'X_umap')

    Returns:
        Friendly name (e.g., 'Umap')
    """
    name = obsm_key
    if name.startswith('X_'):
        name = name[2:]
    return name.capitalize()


def initialize_file_metadata(basename: str, version: str, checksum: str) -> Dict[str, Any]:
    """
    Create initial metadata dictionary for a new file

    Args:
        basename: Base filename (without .h5ad)
        version: Anthive version
        checksum: SHA256 checksum

    Returns:
        Initialized metadata dictionary
    """
    current_year = datetime.now().year
    current_date = datetime.now().strftime('%Y-%m-%d')

    return {
        'title': basename,
        'version': version,
        'sha256': checksum,
        'year': current_year,
        'date': current_date,
        # Biology & study annotation (fill in as needed; null = unknown)
        # organism: human | mouse | rat | zebrafish | macaque | drosophila | ...
        'organism': None,
        # tissue: brain | lung | liver | kidney | ... (string or list)
        'tissue': None,
        # celltype: only if the dataset contains a single cell type
        'celltype': None,
        # disease: healthy | alzheimer | parkinson | cancer | ischemia | ... (str or list)
        'disease': None,
        # condition: control | treated | knockout | wildtype | ... (str or list)
        'condition': None,
        # region: cortex | hippocampus | striatum | prefrontal_cortex | ... (str or list)
        'region': None,
        # sex: male | female | mixed
        'sex': None,
        # age: include unit — e.g. 87y (years), 18m (months), 8w (weeks), P30 (postnatal days)
        'age': None,
        # strain: e.g. C57BL/6, BALB/c, 129S1 (mouse); leave null for human
        'strain': None,
        # technology: 10x_chromium | smart-seq2 | visium | xenium | multiome | snRNA-seq | ...
        'technology': None,
        # Data access
        # accession: GEO/SRA/ArrayExpress ID, e.g. GSE123456 or PRJNA12345
        'accession': None,
        # doi: list of DOIs (without https://doi.org/ prefix)
        'doi': [],
        # link: list of generic URLs (project pages, supplementary data, etc.)
        'link': [],
        # UI curation hints (null = auto-selected)
        # top_genes: genes to highlight — null falls back to top 10 by mean expression
        'top_genes': None,
        # top_cat: categorical columns to highlight — null falls back to first 3 in obscat_include
        # first entry is treated as the primary cluster/annotation column
        'top_cat': None,
        # top_num: numerical columns to highlight — null falls back to first 3 in obsnum_include
        'top_num': None,
        'obsnum_include': [],
        'obscat_include': [],
        'obs_exclude': [],
        'var_include': [],
        'var_exclude': [],
        'layers_include': {},
        'layers_exclude': [],
        'obsm_include': {},
        'obsm_exclude': []
    }


def initialize_collection_metadata(group_name: str) -> Dict[str, Any]:
    """
    Create initial metadata dictionary for a new collection (index.md)

    Args:
        group_name: Name of the collection (typically directory name)

    Returns:
        Initialized metadata dictionary
    """
    current_year = datetime.now().year

    return {
        'group': group_name,
        'experiment': 'na',
        'authors': [],
        'year': current_year
    }
