"""
Anthive .md file sanity checks.

Checks all metadata .md files in a folder tree for:
  - index.md:  presence and validity of group_id (3 chars), no cross-group duplicates
  - data .md:  presence and validity of id (3 chars), no within-group duplicates
  - data .md:  the ``api:`` facet mapping and the lineage keys
    (``lib/facet_lint.py``: closed ``ant_*`` namespace, mapping rules,
    ``reanalysis_of`` / ``is_secondary`` / ``donor_group``); with
    ``with_data=True`` also the rules that read the dataset's ``.duckdb``.

Auto-generates missing IDs when fix=True (default).
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set


class MdCheckReport:
    """Accumulator for md-check results."""

    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.fixed: List[str] = []
        self.checked: int = 0
        self.index_count: int = 0
        self.data_count: int = 0
        # facet lint: rule id -> files that violate it (relative paths)
        self.rule_files: Dict[str, Set[str]] = {}
        self.data_checked: int = 0
        self.data_unavailable: List[str] = []

    def add_finding(self, rel_path: str, finding) -> None:
        """Record one ``facet_lint.Finding`` for ``rel_path``."""
        message = f"{rel_path}: [{finding.rule}] {finding.message}"
        if finding.severity == 'error':
            self.errors.append(message)
        else:
            self.warnings.append(message)
        self.rule_files.setdefault(finding.rule, set()).add(rel_path)

    def rule_summary(self) -> List[str]:
        """One line per violated facet-lint rule: files affected."""
        from facet_lint import RULES  # noqa: PLC0415
        lines = []
        for rule in RULES:
            files = self.rule_files.get(rule)
            if files:
                lines.append(f"{rule:28s} {len(files):4d} file(s)  {RULES[rule]}")
        return lines

    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def collect_log_messages(self) -> List[str]:
        messages = []
        for e in self.errors:
            messages.append(f"✗ {e}")
        for w in self.warnings:
            messages.append(f"⚠ {w}")
        return messages

    def print_summary(self) -> None:
        if not self.errors and not self.warnings and not self.fixed:
            return
        print(f"\n{'='*70}")
        print("WARNINGS & ERRORS")
        print(f"{'='*70}")
        if self.errors:
            print()
            for e in self.errors:
                print(f"  ✗ {e}")
        if self.warnings:
            print()
            for w in self.warnings:
                print(f"  ⚠ {w}")
        if self.fixed:
            print(f"\nIDs auto-generated ({len(self.fixed)}):")
            for f in self.fixed:
                print(f"  ✓ {f}")


def generate_group_id(name: str) -> str:
    """Generate a 3-char group_id from a collection name like 'Zielonka2025'.

    Strategy:
      - Extract trailing 4-digit year
      - Take first letter of each CamelCase word in the stem as initials
      - Combine: up to 3 initials, then year digits to fill to 3 chars

    Examples:
      Zielonka2025          → Z25
      AlbertiniZielonka2025 → AZ5
      LuChen2025            → LC5
      SalaFrigerio2019      → SF9
      Simpson2025           → S25
    """
    year_match = re.search(r'(\d{4})$', name)
    year = year_match.group(1) if year_match else ''
    stem = name[:year_match.start()] if year_match else name

    parts = re.findall(r'[A-Z][a-z0-9]*', stem)
    if not parts and stem:
        parts = [stem]

    initials = ''.join(p[0].upper() for p in parts if p)
    if not initials:
        initials = name[0].upper() if name else 'X'

    n_initials = min(len(initials), 3)
    n_year = 3 - n_initials
    year_suffix = year[-n_year:] if year and n_year > 0 else ''

    result = (initials[:n_initials] + year_suffix).upper()
    return result.ljust(3, 'X')[:3]


def generate_file_id(filename_stem: str) -> str:
    """Generate a 3-char id from an h5ad filename stem.

    Strategy: split by underscores/dashes, take first letter of the
    first three non-numeric word parts.

    Examples:
      Seattle_PFC_MG             → SPM
      seurat_obj_final           → SOF
      seurat_obj_trisomy_initial → SOT
    """
    parts = re.split(r'[_\-\.\s]+', filename_stem)

    # Expand CamelCase within parts
    all_parts: List[str] = []
    for part in parts:
        camel = re.findall(r'[A-Z][a-z]*|[a-z]+', part)
        all_parts.extend(camel if camel else ([part] if part else []))

    word_parts = [p for p in all_parts if p and not p.isdigit()]
    if not word_parts:
        return filename_stem[:3].upper().ljust(3, 'X')

    if len(word_parts) >= 3:
        return ''.join(p[0].upper() for p in word_parts[:3])
    elif len(word_parts) == 2:
        return (word_parts[0][0] + word_parts[1][:2]).upper().ljust(3, 'X')[:3]
    else:
        return word_parts[0][:3].upper().ljust(3, 'X')[:3]


def _top_level_name(md_path: Path, folder: Path) -> str:
    """Return the name of the top-level subdirectory under folder.

    e.g. folder=h5adstore, md_path=h5adstore/LuChen2025/visium/index.md → 'LuChen2025'
    """
    parts = md_path.relative_to(folder).parts
    return parts[0] if parts else md_path.parent.name


def check_md_folder(
    folder: Path,
    report: MdCheckReport,
    fix: bool = True,
    verbose: bool = False,
    namespace_mode: Optional[str] = None,
    with_data: bool = False,
    known_db_ids: Optional[Set[str]] = None,
) -> None:
    """Check all anthive .md files under folder.

    Anthive files recognised:
      - index.md anywhere in the tree     → needs group_id
      - <stem>.md where <stem>.h5ad exists → needs id

    group_id is always derived from the **top-level folder** under the scan
    root, so nested index.md files (e.g. LuChen2025/visium/index.md) share
    the same group_id as the parent collection (LC5).  Duplicate group_ids
    are only flagged when two *different* top-level folders resolve to the
    same ID.

    Other .md files (docs, READMEs, etc.) are ignored.

    Args:
        folder:  Root directory to scan
        report:  Accumulator for results (modified in-place)
        fix:     Write missing IDs back to the .md files (default True)
        verbose: Print a status line for every file processed
        namespace_mode: ``warn`` or ``error`` for ``ant_*`` targets that are
            not registered facets (default ``facet_lint.NAMESPACE_DEFAULT_MODE``)
        with_data: Also run the facet rules that read each dataset's
            ``.duckdb`` (source columns and values); datasets without a
            local ``.duckdb`` are listed in ``report.data_unavailable``
        known_db_ids: Dataset ids that ``reanalysis_of`` may name. Default:
            the ids found under ``folder`` (``<group_id>/<id>``)
    """
    lib_dir = Path(__file__).parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    from metadata import parse_md_file, write_md_file
    import facet_lint  # noqa: PLC0415
    namespace_mode = namespace_mode or facet_lint.NAMESPACE_DEFAULT_MODE

    index_files: List[Path] = []
    data_files: List[Path] = []

    for md_path in sorted(folder.rglob('*.md')):
        # `_`-prefixed folders are staging / archive, excluded everywhere
        if any(part.startswith('_') for part in md_path.relative_to(folder).parts[:-1]):
            continue
        if md_path.name == 'index.md':
            index_files.append(md_path)
        elif md_path.with_suffix('.h5ad').exists():
            data_files.append(md_path)
        # other .md files are silently skipped

    report.checked = len(index_files) + len(data_files)
    report.index_count = len(index_files)
    report.data_count = len(data_files)

    # ── index.md files ────────────────────────────────────────────────────────

    # Map: normalised gid → top-level folder name that first claimed it
    group_ids: Dict[str, str] = {}
    # Map: top-level folder → its group_id (for <group_id>/<id> dataset ids)
    folder_group_id: Dict[str, str] = {}

    for md_path in index_files:
        try:
            metadata, body = parse_md_file(md_path)
        except Exception as e:
            report.errors.append(f"{md_path.relative_to(folder)}: failed to parse: {e}")
            continue

        # group_id is always derived from the top-level folder name
        top_level = _top_level_name(md_path, folder)
        group_id = metadata.get('group_id')
        changed = False

        if not group_id:
            group_id = generate_group_id(top_level)
            metadata['group_id'] = group_id
            changed = True
            report.fixed.append(
                f"{md_path.relative_to(folder)}: "
                f"group_id = '{group_id}' (generated from '{top_level}')"
            )

        gid_str = str(group_id)
        if md_path.parent == folder / top_level:
            folder_group_id[top_level] = gid_str
        else:
            folder_group_id.setdefault(top_level, gid_str)
        if len(gid_str) != 3:
            report.errors.append(
                f"{md_path.relative_to(folder)}: "
                f"group_id '{gid_str}' is not exactly 3 characters"
            )

        gid_upper = gid_str.upper()
        if gid_upper in group_ids:
            existing_top = group_ids[gid_upper]
            if existing_top != top_level:
                # Two different top-level folders share the same group_id
                report.errors.append(
                    f"★ DUPLICATE group_id '{gid_upper}':  "
                    f"{existing_top}  vs  {top_level}"
                )
        else:
            group_ids[gid_upper] = top_level

        if changed and fix:
            try:
                write_md_file(md_path, metadata, body)
            except Exception as e:
                report.errors.append(
                    f"{md_path.relative_to(folder)}: failed to write: {e}"
                )

        if verbose:
            mark = '+' if changed else '✓'
            print(f"  {mark} {md_path.relative_to(folder)}  (group_id={group_id})")

    # ── data .md files ────────────────────────────────────────────────────────

    # Track ids per parent directory for within-group duplicate detection
    dir_ids: Dict[Path, Dict[str, Path]] = {}
    # (relative path, md path, frontmatter) for the facet and lineage lint
    linted: List[tuple] = []

    for md_path in data_files:
        try:
            metadata, body = parse_md_file(md_path)
        except Exception as e:
            report.errors.append(f"{md_path.relative_to(folder)}: failed to parse: {e}")
            continue

        file_id = metadata.get('id')
        changed = False

        if not file_id:
            file_id = generate_file_id(md_path.stem)
            metadata['id'] = file_id
            changed = True
            report.fixed.append(
                f"{md_path.relative_to(folder)}: "
                f"id = '{file_id}' (generated from '{md_path.stem}')"
            )

        id_str = str(file_id)
        if len(id_str) != 3:
            report.errors.append(
                f"{md_path.relative_to(folder)}: "
                f"id '{id_str}' is not exactly 3 characters"
            )

        parent = md_path.parent
        id_upper = id_str.upper()
        if parent not in dir_ids:
            dir_ids[parent] = {}

        if id_upper in dir_ids[parent]:
            existing = dir_ids[parent][id_upper]
            report.errors.append(
                f"★ DUPLICATE id '{id_upper}' in "
                f"{parent.relative_to(folder)}:  "
                f"{existing.name}  vs  {md_path.name}"
            )
        else:
            dir_ids[parent][id_upper] = md_path

        if changed and fix:
            try:
                write_md_file(md_path, metadata, body)
            except Exception as e:
                report.errors.append(
                    f"{md_path.relative_to(folder)}: failed to write: {e}"
                )

        if verbose:
            mark = '+' if changed else '✓'
            print(f"  {mark} {md_path.relative_to(folder)}  (id={file_id})")

        group_id_here = metadata.get('group_id') or folder_group_id.get(_top_level_name(md_path, folder))
        linted.append((str(md_path.relative_to(folder)), md_path, metadata,
                       f"{group_id_here}/{file_id}" if group_id_here else None))

    # ── facet mapping and lineage lint ───────────────────────────────────────

    if known_db_ids is None:
        known_db_ids = {db_id for _, _, _, db_id in linted if db_id}
    for rel_path, md_path, metadata, _db_id in linted:
        api = metadata.get('api') or {}
        for finding in facet_lint.lint_api(api, namespace_mode=namespace_mode):
            report.add_finding(rel_path, finding)
        for finding in facet_lint.lint_lineage(metadata, known_db_ids):
            report.add_finding(rel_path, finding)
        if not with_data or not api:
            continue
        duckdb_path = md_path.with_suffix('.duckdb')
        reader = facet_lint.duckdb_reader(duckdb_path) if duckdb_path.exists() else None
        if reader is None:
            report.data_unavailable.append(rel_path)
            continue
        columns, distinct_values, close = reader
        try:
            for finding in facet_lint.lint_api_data(api, columns, distinct_values):
                report.add_finding(rel_path, finding)
            report.data_checked += 1
        finally:
            close()


def collect_db_ids(store_root: Path) -> Set[str]:
    """``<group_id>/<id>`` of every data ``.md`` under ``store_root``.

    The group id comes from the collection-root ``index.md`` (or the data
    ``.md``'s own ``group_id``). Underscore folders are skipped. Used for
    ``reanalysis_of`` targets when only part of the store is checked.
    """
    lib_dir = Path(__file__).parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    from metadata import parse_md_file  # noqa: PLC0415

    store_root = Path(store_root)
    group_of: Dict[str, Optional[str]] = {}
    ids: Set[str] = set()
    for md_path in sorted(store_root.rglob('*.md')):
        parts = md_path.relative_to(store_root).parts
        if any(part.startswith('_') for part in parts[:-1]) or len(parts) < 2:
            continue
        if md_path.name == 'index.md' or not md_path.with_suffix('.h5ad').exists():
            continue
        top = parts[0]
        if top not in group_of:
            index = store_root / top / 'index.md'
            try:
                group_of[top] = parse_md_file(index)[0].get('group_id') if index.exists() else None
            except Exception:
                group_of[top] = None
        try:
            metadata, _ = parse_md_file(md_path)
        except Exception:
            continue
        group_id = metadata.get('group_id') or group_of[top]
        if group_id and metadata.get('id'):
            ids.add(f"{group_id}/{metadata['id']}")
    return ids


def write_log_entry(folder: Path, tool_name: str, messages: List[str]) -> None:
    """Append warnings/errors to today's log file in the current working directory."""
    if not messages:
        return
    now = datetime.now()
    log_dir = Path.cwd() / 'tmp' if (Path.cwd() / 'tmp').is_dir() else Path.cwd()
    log_path = log_dir / f"ant-{now.strftime('%Y%m%d')}.log"
    timestamp = now.strftime('%H:%M:%S')
    with open(log_path, 'a') as lf:
        lf.write(f"\n{timestamp}  {tool_name}  {folder}\n")
        for msg in messages:
            lf.write(f"  {msg}\n")
