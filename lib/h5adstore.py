"""
Helpers for looking up h5adstore files by db_id.

Shared between ant-geneset, ant-score, and any other tool that needs to
resolve a canonical db_id (e.g. 'S25/PhM') to a file on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

try:
    from metadata import parse_md_file
except ImportError:
    from lib.metadata import parse_md_file  # type: ignore[no-redef]


@dataclass
class H5adEntry:
    """One dataset in the h5adstore."""
    db_id: str          # canonical "{group_id}/{file_id}"  e.g. S25/PhM
    md_path: Path       # the data .md (absolute)
    h5ad_path: Path     # the .h5ad next to the .md (may not exist locally)
    duckdb_path: Path   # the .duckdb next to the .md (may not exist locally)
    group_id: str
    file_id: str
    study: str          # top-level folder name, e.g. Sierksma2025
    stem: str           # file stem, e.g. Pharmacogenomics

    @property
    def readable_id(self) -> str:
        """The path-style id, e.g. Sierksma2025/Pharmacogenomics."""
        return f"{self.study}/{self.stem}"


# Folder-level documents that are never datasets (catalog_manifest uses the same rule).
NON_DATASET_MD_NAMES = frozenset({'index.md', 'README.md'})


def is_dataset_md_rel(rel: Path) -> bool:
    """True when a store-relative ``.md`` path can describe a dataset.

    Not a dataset: files at the store root (CLAUDE.md, CONVENTIONS.md,
    HANDOFF.md, ...), anything under a ``static/`` folder (documents such as
    ``static/<name>.h5ad.md`` that ship with a collection), ``index.md`` and
    ``README.md``, and anything inside a ``_``-prefixed folder.
    """
    parts = Path(rel).parts
    if len(parts) < 2 or not parts[-1].endswith('.md'):
        return False
    if parts[-1] in NON_DATASET_MD_NAMES:
        return False
    if 'static' in parts[:-1]:
        return False
    return not any(part.startswith('_') for part in parts[:-1])


def catalog_dataset_mds(root: Path, include_inactive: bool = False) -> Iterator[Tuple[Path, Path, Path]]:
    """The dataset ``.md`` files of a store, as ant-serve's catalog scan sees them.

    Walks ``root`` following symlinks, skips ``_``-prefixed folders and keeps
    the ``.md`` files ``is_dataset_md_rel`` accepts; datasets with a
    ``.inactive`` sidecar are skipped unless ``include_inactive``. Yields
    ``(path as walked, resolved path, path relative to root)``. Shared by
    ant-serve (``_scan_databases``) and ``ant-stack check``, so both count
    the same datasets.
    """
    root = Path(root)

    def walk(folder: Path) -> Iterator[Tuple[Path, Path, Path]]:
        try:
            items = sorted(folder.iterdir())
        except (PermissionError, OSError):
            return
        for item in items:
            try:
                resolved = item.resolve()
                if resolved.is_file() and resolved.suffix == '.md':
                    rel = item.relative_to(root)
                    if not is_dataset_md_rel(rel):
                        continue
                    if not include_inactive and resolved.with_suffix('.inactive').exists():
                        continue
                    yield item, resolved, rel
                elif resolved.is_dir() and not item.name.startswith('_'):
                    yield from walk(item)
            except (PermissionError, OSError, ValueError):
                continue

    if root.is_dir():
        yield from walk(root)


def iter_h5adstore(root: Path) -> Iterator[H5adEntry]:
    """Walk the h5adstore yielding one H5adEntry per data .md file.

    Skips index.md files. Both group_id (from index.md) and file id (from the
    data .md) must be present — entries lacking either are skipped.
    """
    if not root.exists():
        return
    for md_path in sorted(root.rglob('*.md')):
        if md_path.name == 'index.md':
            continue
        try:
            data_meta, _ = parse_md_file(md_path)
        except Exception:
            continue
        index_meta, _ = parse_md_file(md_path.parent / 'index.md')
        file_id = str(data_meta.get('id') or '').strip()
        group_id = str(index_meta.get('group_id') or '').strip()
        if not file_id or not group_id:
            continue
        rel = md_path.relative_to(root)
        study = rel.parts[0]
        stem = md_path.stem
        h5ad_path = md_path.with_suffix('.h5ad')
        duckdb_path = md_path.with_suffix('.duckdb')
        yield H5adEntry(
            db_id=f"{group_id}/{file_id}",
            md_path=md_path,
            h5ad_path=h5ad_path,
            duckdb_path=duckdb_path,
            group_id=group_id,
            file_id=file_id,
            study=study,
            stem=stem,
        )


def find_h5ad(root: Path, db_id_or_path: str) -> Optional[H5adEntry]:
    """
    Resolve a db_id to an H5adEntry. Accepts three forms:
      - canonical:   'S25/PhM'
      - readable :   'Sierksma2025/Pharmacogenomics'
      - file path:   '/abs/path/to/Foo.h5ad' or '.../Foo.duckdb' (also
                     relative paths if they resolve to a real file).
    The path form is what ``ant-ingest`` passes when invoking
    ``ant-de-ingest`` (it carries the target_duckdb Path through directly);
    without this branch every S3-ingested DE deposit fails with
    "unknown db_id".

    The path form does NOT go through ``iter_h5adstore`` — that helper is
    strict about requiring an explicit ``id:`` in the .md and a ``group_id:``
    in the index.md, and silently skips entries that lack either. Plenty of
    legitimate datasets in the catalog (e.g. Sierksma2020) have no explicit
    ``id:`` and rely on ant-serve's auto-generator — those wouldn't be found
    via iter_h5adstore even though they're in the catalog. So we synthesise
    the H5adEntry directly from the resolved file path, falling back to
    ``md_check.generate_file_id`` / ``generate_group_id`` for the missing
    ID components.
    """
    target = db_id_or_path.strip().strip('/')

    # Path form — duckdb or h5ad. Resolve and synthesize directly from
    # the file's location + sibling .md without going through
    # iter_h5adstore (which filters out entries lacking an explicit id).
    candidate = Path(db_id_or_path)
    if candidate.suffix in ('.h5ad', '.duckdb'):
        root_resolved = root.resolve()
        try:
            resolved = candidate.resolve()
            if resolved.is_file():
                md_path = resolved.with_suffix('.md')
                if md_path.exists():
                    try:
                        rel = md_path.relative_to(root_resolved)
                    except ValueError:
                        rel = None
                    if rel is not None and len(rel.parts) >= 1:
                        # Local import to avoid a circular import at module load.
                        from md_check import generate_file_id, generate_group_id  # noqa: E402
                        data_meta, _ = parse_md_file(md_path)
                        index_meta, _ = parse_md_file(md_path.parent / 'index.md')
                        if not index_meta:
                            index_meta, _ = parse_md_file(root_resolved / rel.parts[0] / 'index.md')
                        study = rel.parts[0]
                        stem = md_path.stem
                        file_id = (
                            str(data_meta.get('id') or '').strip()
                            or generate_file_id(stem)
                        )
                        group_id = (
                            str(index_meta.get('group_id') or '').strip()
                            or generate_group_id(study)
                        )
                        return H5adEntry(
                            db_id=f"{group_id}/{file_id}",
                            md_path=md_path,
                            h5ad_path=md_path.with_suffix('.h5ad'),
                            duckdb_path=md_path.with_suffix('.duckdb'),
                            group_id=group_id,
                            file_id=file_id,
                            study=study,
                            stem=stem,
                        )
        except OSError:
            pass

    # ID forms.
    for entry in iter_h5adstore(root):
        if entry.db_id == target:
            return entry
        if entry.readable_id == target:
            return entry
    return None


def all_db_ids(root: Path) -> List[str]:
    """Return every canonical db_id in the store (for validation lookups)."""
    return [e.db_id for e in iter_h5adstore(root)]
