"""Scratch conversion and sha256 caching for ``ant ingest-check``.

The checks that read the ``.duckdb`` need one. A submission has none, so
``ant-convert`` runs on a scratch copy: the h5ad is hard-linked (or
reflink-copied) into ``<scratch>/convert/<key>/<Group>/`` and the
convert-relevant metadata is copied beside it. The curator's folder and the
store are never touched. The result is cached by ``(h5ad sha256, convert
relevant .md keys)``, so a metadata-only edit that does not change what
``ant-convert`` reads reuses the previous scratch build.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

#: ``.md`` keys that change what ant-convert writes into the .duckdb.
CONVERT_KEYS = ('layers_include', 'layers_exclude', 'layer_description', 'obsnum_include', 'obscat_include',
                'obs_exclude', 'var_include', 'var_exclude', 'obsm_include', 'obsm_exclude',
                'embedding_description')

#: Scratch builds older than this are removed before a new one starts.
SCRATCH_MAX_AGE_DAYS = 7


class ConvertError(RuntimeError):
    """The scratch conversion failed; ``str()`` is a short reason for the report."""


def sha256_cached(path: Path, cache_file: Path) -> str:
    """SHA-256 of a file, cached by (resolved path, size, mtime).

    Args:
        path: The file to hash.
        cache_file: JSON file in the scratch dir holding earlier results.

    Returns:
        The hex digest.
    """
    from checksum import calculate_sha256  # noqa: PLC0415

    stat = path.stat()
    key = f'{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}'
    cache: Dict[str, str] = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except (OSError, ValueError):
            cache = {}
    if key in cache:
        return cache[key]
    digest = calculate_sha256(path, chunk_size=8 * 1024 * 1024)
    cache[key] = digest
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache))
    return digest


def convert_key(h5ad_sha256: str, metadata: Mapping[str, Any]) -> str:
    """Cache key: the h5ad hash plus the convert-relevant ``.md`` keys."""
    relevant = {name: metadata.get(name) for name in CONVERT_KEYS if name in metadata}
    payload = json.dumps(relevant, sort_keys=True, default=str)
    return hashlib.sha256(f'{h5ad_sha256}|{payload}'.encode()).hexdigest()[:20]


def link_or_copy(source: Path, target: Path) -> None:
    """Hard-link ``source`` to ``target``; fall back to a reflink copy (cross-device)."""
    try:
        os.link(source, target)
    except OSError:
        subprocess.run(['cp', '--reflink=auto', str(source), str(target)], check=True)


def prune_scratch(convert_root: Path, max_age_days: int = SCRATCH_MAX_AGE_DAYS) -> int:
    """Remove scratch builds not used for ``max_age_days``. Returns how many."""
    removed = 0
    if not convert_root.is_dir():
        return removed
    cutoff = time.time() - max_age_days * 86400
    for entry in convert_root.iterdir():
        marker = entry / '.used'
        stamp = marker.stat().st_mtime if marker.exists() else entry.stat().st_mtime
        if entry.is_dir() and stamp < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def scratch_convert(h5ad_path: Path, md_path: Path, index_md_path: Optional[Path], mus_path: Optional[Path],
                    metadata: Mapping[str, Any], h5ad_sha256: str, scratch_root: Path,
                    convert_script: Path, group_name: str) -> Path:
    """Convert on a scratch copy and return the scratch ``.duckdb`` path.

    Args:
        h5ad_path: The submitted h5ad (only read, hard-linked).
        md_path: The dataset ``.md`` (copied).
        index_md_path: The group ``index.md`` (copied) so the group id resolves.
        mus_path: The ``.h5ad.mus`` sidecar (copied) or None.
        metadata: Parsed ``.md`` (for the cache key).
        h5ad_sha256: Hash of the h5ad.
        scratch_root: ``config.get_ingest_scratch_dir()``.
        convert_script: Path of ``bin/ingest/ant-convert``.
        group_name: Folder name used inside the scratch tree.

    Returns:
        Path of the converted ``.duckdb`` inside the scratch tree.

    Raises:
        ConvertError: ant-convert failed; the message carries its last lines.
    """
    convert_root = scratch_root / 'convert'
    key = convert_key(h5ad_sha256, metadata)
    work = convert_root / key / group_name
    duckdb_path = work / (h5ad_path.stem + '.duckdb')
    marker = work.parent / '.complete'
    work.mkdir(parents=True, exist_ok=True)
    for source, name in ((md_path, md_path.name), (index_md_path, 'index.md'), (mus_path, h5ad_path.name + '.mus')):
        if source is not None and source.exists():
            shutil.copyfile(source, work / name)
    if marker.exists() and duckdb_path.exists():
        (work.parent / '.used').touch()
        return duckdb_path
    prune_scratch(convert_root)
    scratch_h5ad = work / h5ad_path.name
    if scratch_h5ad.exists():
        scratch_h5ad.unlink()
    link_or_copy(h5ad_path, scratch_h5ad)
    environment = dict(os.environ)
    environment['ANTHIVE_CONVERT_STAGING_DIR'] = str(scratch_root / 'staging')
    environment['ANTHIVE_DUCKDB_TEMP_DIR'] = str(scratch_root / 'spill')
    result = subprocess.run([str(convert_script), str(scratch_h5ad)], capture_output=True, text=True,
                            env=environment, check=False)
    if result.returncode != 0 or not duckdb_path.exists():
        tail = '\n'.join((result.stderr or result.stdout).strip().splitlines()[-6:])
        raise ConvertError(f'ant-convert exited {result.returncode}: {tail}')
    marker.write_text(h5ad_sha256)
    (work.parent / '.used').touch()
    return duckdb_path


def isolated_copy_for_check(duckdb_path: Path, md_path: Path, scratch_root: Path) -> Path:
    """A private copy of a ``.duckdb`` for tools that write while they check.

    ``bin/ingest/ant-duckdb-check`` is not read-only: it builds a missing
    ``__gene_bins`` table in the database, may write ``default_layer`` into
    the ``.md`` and logs into the working directory. It therefore only ever
    runs on a copy under the scratch dir: the database (reflink copy), the
    ``.md`` (copy) and symlinks to the layer parquet files beside it.

    Returns the path of the copy. A database already inside the scratch tree
    (a scratch convert) is returned as is.
    """
    duckdb_path = Path(duckdb_path).resolve()
    scratch_root = Path(scratch_root).resolve()
    if scratch_root in duckdb_path.parents:
        return duckdb_path
    stat = duckdb_path.stat()
    key = hashlib.sha256(f'{duckdb_path}|{stat.st_size}|{stat.st_mtime_ns}'.encode()).hexdigest()[:16]
    folder = scratch_root / 'dbcheck' / key
    copy = folder / duckdb_path.name
    if copy.exists():
        (folder / '.used').touch()
        return copy
    prune_scratch(scratch_root / 'dbcheck')
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '--reflink=auto', str(duckdb_path), str(copy)], check=True)
    if md_path.is_file():
        shutil.copyfile(md_path, folder / (duckdb_path.stem + '.md'))
    for sibling in duckdb_path.parent.glob(f'{duckdb_path.stem}.*.parquet'):
        link = folder / sibling.name
        if not link.exists():
            link.symlink_to(sibling)
    (folder / '.used').touch()
    return copy
