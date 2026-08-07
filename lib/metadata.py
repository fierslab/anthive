"""
Metadata management for anthive

Functions for reading and writing the `.md` YAML preambles that hold
anthive-catalog fields (`id`, `sha256`, `n_obs`, `layer_stats`,
`obscat_include`, `default_layer`, …).

DEPRECATED: as of the 2026-06 h5adstore migration, experimental
annotation (organism, tissue, cell_type, technology, …) NO LONGER
lives in `.md` frontmatter. It lives in `<basename>.h5ad.mus` sidecars
plus folder-level `.env` cascade — read via `lib/mus.py`. The
`ANNOTATION_FIELDS`, `ANNOTATION_LIST_FIELDS`, `ANNOTATION_CONVENTIONS`,
and `validate_annotation_field()` symbols below are kept for backward
compatibility with `tests/test_metadata.py` and any legacy importer
that hasn't been migrated yet. New code should use `lib.mus`.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


# ── DEPRECATED — use lib/mus.py for experiment annotation ────────────────
# These describe the pre-migration `annotation:` block in `.md` frontmatter.
# Vocabulary is now upstream at codeberg.org/mfiers/mus.
ANNOTATION_FIELDS = [
    'organism', 'tissue', 'celltype', 'disease', 'condition',
    'region', 'sex', 'age', 'strain', 'technology', 'technology_category',
    'accession', 'doi', 'link',
]

ANNOTATION_LIST_FIELDS = {'doi', 'link', 'tissue', 'disease', 'condition', 'region', 'technology'}

ANNOTATION_CONVENTIONS = {
    'sex': {
        'choices': ['male', 'female', 'mixed'],
    },
    'organism': {
        'lowercase': True,
    },
    'technology_category': {
        'choices': [
            'single-cell/nucleus transcriptomics',
            'spatial transcriptomics',
            'bulk transcriptomics',
            'proteomics',
        ],
    },
}


def validate_annotation_field(field: str, value: Any) -> Optional[str]:
    """DEPRECATED — see lib/mus.validate_value() and lib/mus.validate_annotation().

    Validate an annotation field value against the (legacy) anthive-side
    conventions. Returns error string if invalid, else None. New code
    should validate against the joint vocabulary via `lib/mus.py`.
    """
    conv = ANNOTATION_CONVENTIONS.get(field)
    if not conv:
        return None
    if 'choices' in conv:
        if str(value).lower() not in [c.lower() for c in conv['choices']]:
            return f"'{field}' must be one of: {', '.join(conv['choices'])} (got '{value}')"
    if conv.get('lowercase') and isinstance(value, str) and value != value.lower():
        return f"'{field}' must be lowercase (got '{value}')"
    return None


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
    Write markdown file with YAML preamble — IDEMPOTENT.

    If the serialised content matches what's already on disk byte-for-byte,
    the file is NOT touched (mtime preserved). This breaks the snowball
    in ``ant-build-all`` where re-running ``ant-h5ad-check`` was bumping
    every .md's mtime, which then made every .duckdb look stale
    (.md-newer-than-duckdb gate), which triggered a full-catalog
    reconvert on the next pass.

    Args:
        md_path: Path to .md file
        yaml_dict: Dictionary to write as YAML preamble
        markdown_body: Markdown content
    """
    try:
        # Build the new content in memory first so we can diff against
        # what's already on disk.
        from io import StringIO
        buf = StringIO()
        buf.write('---\n')
        yaml.dump(yaml_dict, buf, default_flow_style=False, sort_keys=False, allow_unicode=True)
        buf.write('---\n\n')
        buf.write(markdown_body)
        if not markdown_body.endswith('\n'):
            buf.write('\n')
        new_content = buf.getvalue()

        # No-op when the file already has identical content. Preserves
        # the mtime, which is what downstream stale-checks (ant-build-all
        # + ant-serve's metadata-cache fingerprint) rely on.
        if md_path.exists():
            try:
                existing = md_path.read_text(encoding='utf-8')
                if existing == new_content:
                    return
            except OSError:
                pass  # fall through to write

        with open(md_path, 'w') as file:
            file.write(new_content)
    except Exception as error:
        raise MetadataError(
            f"ERROR: [Metadata] Failed to write metadata file\n"
            f"Location: {md_path}\n"
            f"Error: {error}"
        )


# ── priority (per-dataset retention priority for tiered local storage) ──
#
# Integer 0..100 stored as the `priority:` key in the data .md frontmatter.
# Read by `bin/ops/ant-sync` to decide which `.duckdb` files to pull / evict
# under the configured `sync_policy` (anthive.yaml). Higher = keep local.
#
#   0   → effectively inactive; never pulled, always evicted
#   50  → default for .md files without an explicit priority
#   100 → always-must-have; never evicted unless cap forces it
#
# Scope is per-dataset (per .md file), not per-group. Editable via
# `bin/ops/ant-priority`.

PRIORITY_DEFAULT = 50
PRIORITY_MIN = 0
PRIORITY_MAX = 100


def get_priority(yaml_dict: Dict[str, Any], default: int = PRIORITY_DEFAULT) -> int:
    """Return the integer `priority:` from a parsed .md, or ``default``.

    Tolerates a missing key, ``None`` value, or a value that parses as
    int via ``int()``. Out-of-range values are clamped into [0, 100]
    and the in-memory dict is NOT mutated — the caller decides whether
    to write the clamped value back.
    """
    raw = yaml_dict.get('priority')
    if raw is None:
        return int(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return max(PRIORITY_MIN, min(PRIORITY_MAX, value))


def set_priority(yaml_dict: Dict[str, Any], value: int) -> int:
    """Write `priority: <int>` into the dict in place. Returns the value stored.

    Raises ``ValueError`` for non-integer or out-of-range input. Use this
    rather than direct dict access so the [0, 100] invariant is enforced
    at the boundary that writes the .md file.
    """
    try:
        i = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"priority must be an integer, got {value!r}") from exc
    if i < PRIORITY_MIN or i > PRIORITY_MAX:
        raise ValueError(
            f"priority must be in [{PRIORITY_MIN}, {PRIORITY_MAX}], got {i}"
        )
    yaml_dict['priority'] = i
    return i


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


_RESERVED_VIEW_COLS = {'cell_id', 'cell_name'}


def get_api_config(yaml_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Return the validated ``api:`` section from a .md's YAML frontmatter.

    Shape:
        api:
          obs_rename:
            <internal_col>: <public_col>       # column-level rename
          obs_hidden:
            - <internal_col>                    # drop the col entirely
          obs_value_rename:
            <internal_col>:                     # value-level rename
              <old_value>: <new_value>          # per (col, old-value)
          obs_value_hidden:
            <internal_col>:                     # NULL specific values
              - <value>

    Semantics:
      * ``obs_rename``: internal → public column name. A rename implicitly
        hides the internal name (there is no way to reach the old name
        from the API once it's renamed).
      * ``obs_hidden``: cols that get no public alias — dropped from the
        view entirely.
      * ``obs_value_rename``: rewrites individual values inside a
        categorical column via a per-column mapping. Multiple old values
        can map to the same new value ("M"→"male", "male"→"male") — the
        collapse behavior lets you clean up mixed vocabularies.
      * ``obs_value_hidden``: values that get NULL'd. Applied BEFORE
        value_rename, so a hidden value never enters the rename branch.

    The key of ``obs_value_*`` is always the INTERNAL col name (matches
    ``obs_rename``'s key namespace). Value-level ops apply to categorical
    cols only (obscat) — silently no-op on obsnum since numeric values
    don't have vocabularies.

    Returns:
        Dict with keys 'obs_rename' (dict), 'obs_hidden' (list),
        'obs_value_rename' (dict of dicts), 'obs_value_hidden' (dict of
        lists). All always present; empty container when not configured.

    Raises:
        MetadataError on validation failures:
          - two obs_rename entries mapping to the same public name
          - a rename target that's a reserved column name
          - obs_value_rename entry that's not str → str
          - obs_value_hidden entry that's not a list
    """
    api = yaml_dict.get('api') or {}
    if not isinstance(api, dict):
        raise MetadataError(
            f"ERROR: [Metadata] 'api:' must be a mapping, got {type(api).__name__}"
        )
    rename = api.get('obs_rename') or {}
    hidden = api.get('obs_hidden') or []
    value_rename = api.get('obs_value_rename') or {}
    value_hidden = api.get('obs_value_hidden') or {}

    if rename and not isinstance(rename, dict):
        raise MetadataError("ERROR: [Metadata] 'api.obs_rename' must be a mapping")
    if hidden and not isinstance(hidden, list):
        raise MetadataError("ERROR: [Metadata] 'api.obs_hidden' must be a list")
    if value_rename and not isinstance(value_rename, dict):
        raise MetadataError("ERROR: [Metadata] 'api.obs_value_rename' must be a mapping")
    if value_hidden and not isinstance(value_hidden, dict):
        raise MetadataError("ERROR: [Metadata] 'api.obs_value_hidden' must be a mapping")

    # No duplicate public targets for column-level rename
    seen_targets = {}
    for internal, public in rename.items():
        if not isinstance(public, str) or not public:
            raise MetadataError(
                f"ERROR: [Metadata] api.obs_rename['{internal}'] must be a non-empty string"
            )
        if public in _RESERVED_VIEW_COLS:
            raise MetadataError(
                f"ERROR: [Metadata] api.obs_rename['{internal}'] target '{public}' "
                f"collides with a reserved view column name"
            )
        if public in seen_targets:
            raise MetadataError(
                f"ERROR: [Metadata] api.obs_rename collision — "
                f"both '{seen_targets[public]}' and '{internal}' map to '{public}'"
            )
        seen_targets[public] = internal

    # Value-level renames: per-column dict of str → str
    for col, mapping in value_rename.items():
        if not isinstance(mapping, dict):
            raise MetadataError(
                f"ERROR: [Metadata] api.obs_value_rename['{col}'] must be a mapping"
            )
        for old_val, new_val in mapping.items():
            if new_val is not None and not isinstance(new_val, str):
                # allow int/float new values by coercing to string,
                # since DuckDB stores obscat values as VARCHAR anyway.
                mapping[old_val] = str(new_val)

    # Value-level hidden: per-column list
    for col, vals in value_hidden.items():
        if not isinstance(vals, list):
            raise MetadataError(
                f"ERROR: [Metadata] api.obs_value_hidden['{col}'] must be a list"
            )

    return {
        'obs_rename': dict(rename),
        'obs_hidden': list(hidden),
        'obs_value_rename': dict(value_rename),
        'obs_value_hidden': dict(value_hidden),
    }


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
        'annotation': {
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
            # accession: GEO/SRA/ArrayExpress ID, e.g. GSE123456 or PRJNA12345
            'accession': None,
            # doi: list of DOIs (without https://doi.org/ prefix)
            'doi': [],
            # link: list of generic URLs (project pages, supplementary data, etc.)
            'link': [],
        },
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
        'year': current_year,
        'annotation': {},
        'default_dataset': None,
    }
