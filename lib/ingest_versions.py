"""Previous versions of a dataset: archive on update, list, purge.

When an upload replaces a dataset that is already published, the source
files of the version being replaced are hard-linked (never moved) into
``<store>/<group>[/<sub>]/_versions/<basename>/<version>/`` and the catalog
copies on S3 are copied to the matching key
``<catalog_prefix><group>[/<sub>]/_versions/<basename>/<version>/<file>``.
Underscore folders are skipped by the catalogue scan of ant-serve,
``ant-sync``, ``md_check`` and the catalog manifest, so an archived version is
never served or synced; it is there to be restored by hand.

Kept per version: the ``.h5ad``, ``.md``, ``.h5ad.mus``, ``.scores.parquet``
(scores of the old cells) and a copy of the ``.de.parquet``. Derived files
(``.duckdb``, layer parquets) are rebuilt from the h5ad and are not kept.

:func:`plan_purge` / :func:`purge` remove archived versions (never the
current one). The S3 side only ever deletes keys under the version's own
``_versions/<basename>/<version>/`` prefix, one key at a time.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

VERSIONS_DIR = '_versions'
#: Suffixes moved into the version folder (relative to ``<basename>``).
ARCHIVED_SUFFIXES = ('.h5ad', '.md', '.h5ad.mus', '.scores.parquet')
#: Suffixes copied (not moved): they stay with the current version too.
COPIED_SUFFIXES = ('.de.parquet',)
#: Catalog files copied on S3 for a version.
S3_ARCHIVED_SUFFIXES = ('.h5ad', '.h5ad.mus')

_VERSION_RE = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')


def version_key(version: str) -> Tuple[int, int, int]:
    """Sort key of an ``X.Y.Z`` version; malformed versions sort first."""
    if not _VERSION_RE.fullmatch(version or ''):
        return (-1, -1, -1)
    major, minor, patch = (int(part) for part in version.split('.'))
    return (major, minor, patch)


def version_dir(dataset_dir: Path, basename: str, version: str) -> Path:
    """Local folder of one archived version."""
    return dataset_dir / VERSIONS_DIR / basename / version


def version_s3_prefix(catalog_prefix: str, rel_dir: str, basename: str, version: str) -> str:
    """S3 prefix (ending in ``/``) of one archived version."""
    return f'{catalog_prefix}{rel_dir.strip("/")}/{VERSIONS_DIR}/{basename}/{version}/'


def archive_plan(dataset_dir: Path, basename: str, version: str,
                 data_changed: bool = True) -> Tuple[List[Tuple[Path, Path]], List[Path]]:
    """What archiving the current version means, without touching anything.

    Returns ``(links, remove_after)``: ``(current file, file in the version
    folder)`` pairs to hard-link (the current files stay in place until the
    swap commits), and the current files to remove once the new version is
    in place (the old ``.scores.parquet`` when the data changed: those are
    scores of the old cells). Raises FileExistsError when the version folder
    already holds files, so an archived version is never overwritten.
    """
    target = version_dir(dataset_dir, basename, version)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f'{target} already exists; refusing to overwrite an archived version')
    links: List[Tuple[Path, Path]] = []
    remove_after: List[Path] = []
    for suffix in ARCHIVED_SUFFIXES + COPIED_SUFFIXES:
        source = dataset_dir / f'{basename}{suffix}'
        if source.exists():
            links.append((source, target / source.name))
            if suffix == '.scores.parquet' and data_changed:
                remove_after.append(source)
    return links, remove_after


def link_or_copy(source: Path, destination: Path) -> None:
    """Hard link (same file system, no extra space), else copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def archive_local(dataset_dir: Path, basename: str, version: str, data_changed: bool = True) -> List[Path]:
    """Link the current version's source files into its version folder (nothing is moved).

    Convenience wrapper over :func:`archive_plan` for callers without a swap
    journal; the pipeline applies the plan through its journal instead.
    """
    links, _ = archive_plan(dataset_dir, basename, version, data_changed)
    for source, destination in links:
        link_or_copy(source, destination)
    return [destination for _, destination in links]


def archive_s3(s3: Any, bucket: str, catalog_prefix: str, rel_dir: str, basename: str, version: str,
               local_files: Sequence[Path], include_h5ad: bool = True) -> List[str]:
    """Copy the catalog objects of the current version to its ``_versions`` prefix on S3.

    A catalog object that is not on S3 is uploaded from the archived local
    file instead. The archived ``.md`` is uploaded too, so the version can be
    restored from S3 alone. A metadata-only update (``include_h5ad`` false)
    copies the sidecar and ``.md`` only: its h5ad is the current one (the
    ``.md`` records the same ``sha256``). Returns the keys written.
    """
    prefix = version_s3_prefix(catalog_prefix, rel_dir, basename, version)
    by_name = {path.name: path for path in local_files}
    written: List[str] = []
    for suffix in S3_ARCHIVED_SUFFIXES:
        if suffix == '.h5ad' and not include_h5ad:
            continue
        name = f'{basename}{suffix}'
        source_key = f'{catalog_prefix}{rel_dir.strip("/")}/{name}'
        target_key = prefix + name
        try:
            s3.head_object(Bucket=bucket, Key=source_key)
            on_s3 = True
        except Exception:  # noqa: BLE001 - absent on S3: fall back to the local copy
            on_s3 = False
        if on_s3:                                    # a failing copy raises: the caller retries later
            s3.copy({'Bucket': bucket, 'Key': source_key}, bucket, target_key)
            written.append(target_key)
            continue
        if name in by_name:
            s3.upload_file(str(by_name[name]), bucket, target_key)
            written.append(target_key)
    md_name = f'{basename}.md'
    if md_name in by_name:
        s3.upload_file(str(by_name[md_name]), bucket, prefix + md_name)
        written.append(prefix + md_name)
    return written


def list_local_versions(dataset_dir: Path, basename: str) -> List[str]:
    """Archived versions of a dataset in the store, oldest first."""
    root = dataset_dir / VERSIONS_DIR / basename
    if not root.is_dir():
        return []
    return sorted((entry.name for entry in root.iterdir() if entry.is_dir()), key=version_key)


def list_s3_versions(s3: Any, bucket: str, catalog_prefix: str, rel_dir: str, basename: str) -> Dict[str, List[str]]:
    """``{version: [keys]}`` of the archived versions of a dataset on S3."""
    root = f'{catalog_prefix}{rel_dir.strip("/")}/{VERSIONS_DIR}/{basename}/'
    found: Dict[str, List[str]] = {}
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=root):
        for obj in page.get('Contents', []) or []:
            rest = obj['Key'][len(root):]
            if '/' not in rest:
                continue
            found.setdefault(rest.split('/', 1)[0], []).append(obj['Key'])
    return found


@dataclass
class PurgePlan:
    """What a purge of one dataset removes."""
    db_id: str
    dataset_dir: Path
    rel_dir: str
    basename: str
    current_version: str
    keep: int
    remove_versions: List[str] = field(default_factory=list)
    local_dirs: List[Path] = field(default_factory=list)
    s3_keys: List[str] = field(default_factory=list)


def plan_purge(db_id: str, dataset_dir: Path, rel_dir: str, basename: str, current_version: str, keep: int,
               s3: Any = None, bucket: str = '', catalog_prefix: str = '') -> PurgePlan:
    """Plan the removal of archived versions so that ``keep`` versions remain (the current one counts).

    Args:
        keep: Versions to keep including the current one (``1``: only the current).
    """
    if keep < 1:
        raise ValueError('--keep must be at least 1 (the current version is never purged)')
    local = list_local_versions(dataset_dir, basename)
    remote = list_s3_versions(s3, bucket, catalog_prefix, rel_dir, basename) if s3 is not None else {}
    archived = sorted(set(local) | set(remote), key=version_key)
    archived = [version for version in archived if version != current_version]
    keep_archived = keep - 1
    remove = archived[:max(0, len(archived) - keep_archived)]
    plan = PurgePlan(db_id, dataset_dir, rel_dir, basename, current_version, keep, remove_versions=remove)
    for version in remove:
        folder = version_dir(dataset_dir, basename, version)
        if folder.is_dir():
            plan.local_dirs.append(folder)
        prefix = version_s3_prefix(catalog_prefix, rel_dir, basename, version)
        for key in remote.get(version, []):
            if not key.startswith(prefix):
                raise RuntimeError(f'refusing to purge {key}: outside {prefix}')
            plan.s3_keys.append(key)
    return plan


def purge(plan: PurgePlan, s3: Any = None, bucket: str = '',
          record: Optional[Callable[[str, str, str], None]] = None) -> Dict[str, int]:
    """Carry out a :class:`PurgePlan`. ``record(kind, target, version)`` is called per removal."""
    removed = {'local_dirs': 0, 's3_keys': 0}
    versions_root = plan.dataset_dir / VERSIONS_DIR / plan.basename
    for folder in plan.local_dirs:
        if versions_root.resolve() not in folder.resolve().parents:
            raise RuntimeError(f'refusing to remove {folder}: outside {versions_root}')
        shutil.rmtree(folder)
        removed['local_dirs'] += 1
        if record:
            record('local', str(folder), folder.name)
    for key in plan.s3_keys:
        if f'/{VERSIONS_DIR}/{plan.basename}/' not in key:
            raise RuntimeError(f'refusing to delete {key}: not a {VERSIONS_DIR} key')
        s3.delete_object(Bucket=bucket, Key=key)
        removed['s3_keys'] += 1
        if record:
            record('s3', key, key.split('/')[-2])
    if versions_root.is_dir() and not any(versions_root.iterdir()):
        versions_root.rmdir()
    return removed


def find_datasets(store_root: Path) -> List[Dict[str, Any]]:
    """Every dataset ``.md`` in the store (underscore and dot folders skipped) with its db id.

    Returns dicts with ``db_id``, ``dataset_dir``, ``rel_dir``, ``basename``
    and ``version``. The group id comes from the nearest ``index.md`` above
    the ``.md`` (the group folder, or the parent of a subfolder).
    """
    from metadata import parse_md_file  # noqa: PLC0415

    store_root = Path(store_root)
    found: List[Dict[str, Any]] = []
    for md_path in sorted(store_root.rglob('*.md')):
        rel = md_path.relative_to(store_root)
        if md_path.name in ('index.md', 'README.md') or len(rel.parts) < 2:
            continue
        if any(part.startswith(('_', '.')) for part in rel.parts[:-1]):
            continue
        try:
            meta = parse_md_file(md_path)[0]
        except Exception:  # noqa: BLE001 - unreadable .md: not a purge candidate
            continue
        group_id = None
        for folder in (md_path.parent, md_path.parent.parent):
            index = folder / 'index.md'
            if index.exists() and store_root in index.parents:
                try:
                    group_id = parse_md_file(index)[0].get('group_id')
                except Exception:  # noqa: BLE001
                    group_id = None
                break
        if not group_id or not meta.get('id'):
            continue
        found.append({'db_id': f'{group_id}/{meta["id"]}', 'dataset_dir': md_path.parent,
                      'rel_dir': str(rel.parent), 'basename': md_path.stem,
                      'version': str(meta.get('version') or '')})
    return found
