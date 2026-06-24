"""
Helpers for looking up h5adstore files by db_id.

Shared between ant-geneset, ant-score, and any other tool that needs to
resolve a canonical db_id (e.g. 'S25/PhM') to a file on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

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
    """
    target = db_id_or_path.strip().strip('/')

    # Path form — duckdb or h5ad. Resolve and match against entry paths.
    candidate = Path(db_id_or_path)
    if candidate.suffix in ('.h5ad', '.duckdb'):
        try:
            resolved = candidate.resolve()
            if resolved.is_file():
                for entry in iter_h5adstore(root):
                    try:
                        if resolved in (entry.h5ad_path.resolve(),
                                        entry.duckdb_path.resolve()):
                            return entry
                    except OSError:
                        continue
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
