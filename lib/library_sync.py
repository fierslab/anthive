"""S3 sync for the paper library PDFs and the literature index.

The library records (``.md``) live in git; their PDFs and the precomputed
``library_chunks.parquet`` live in S3, next to the h5adstore objects:

    <s3.prefix>/_library/papers/<sub/folders>/<citekey>.pdf
    <s3.prefix>/_library_index/library_chunks.parquet

The leading ``_`` keeps both prefixes out of the h5adstore sync and the S3
catalog scan. The PDF layout mirrors the folders under ``<library>/papers``.
A paper that moved to another folder is uploaded under its new key; the old
key is reported as S3-only and never deleted automatically.

Same change detection as ant-sync: size first, then the ``original-mtime``
object metadata for same-size files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from library_service import INDEX_FILENAME, INDEX_S3_PREFIX, LIBRARY_S3_PREFIX

MTIME_METADATA_KEY = 'original-mtime'


@dataclass
class Area:
    name: str
    local_root: Path
    key_prefix: str            # under s3.prefix, no trailing slash
    accept: Callable[[str], bool]


@dataclass
class AreaReport:
    name: str
    transferred: List[str] = field(default_factory=list)
    up_to_date: int = 0
    remote_only: List[str] = field(default_factory=list)
    local_only: List[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None


def _pdf_rel(rel: str) -> bool:
    parts = rel.split('/')
    return rel.lower().endswith('.pdf') and not any(p.startswith(('_', '.')) for p in parts)


def library_areas(library_dir: Optional[Path], index_dir: Optional[Path]) -> List[Area]:
    areas: List[Area] = []
    if library_dir is not None:
        papers = Path(library_dir) / 'papers'
        areas.append(Area('library papers', papers if papers.is_dir() else Path(library_dir),
                          LIBRARY_S3_PREFIX, _pdf_rel))
    if index_dir is not None:
        areas.append(Area('literature index', Path(index_dir), INDEX_S3_PREFIX,
                          lambda rel: rel in (INDEX_FILENAME, INDEX_FILENAME.replace('.parquet', '.json'))))
    return areas


def _full_prefix(s3_prefix: str, key_prefix: str) -> str:
    return '/'.join(p for p in (s3_prefix.strip('/'), key_prefix.strip('/')) if p) + '/'


def local_files(area: Area) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    if not area.local_root.is_dir():
        return out
    for path in sorted(area.local_root.rglob('*')):
        if path.is_file():
            rel = path.relative_to(area.local_root).as_posix()
            if area.accept(rel):
                out[rel] = path
    return out


def remote_objects(client, bucket: str, s3_prefix: str, area: Area) -> Dict[str, int]:
    prefix = _full_prefix(s3_prefix, area.key_prefix)
    out: Dict[str, int] = {}
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            rel = obj['Key'][len(prefix):]
            if rel and area.accept(rel):
                out[rel] = obj['Size']
    return out


def _remote_mtime(client, bucket: str, key: str) -> Optional[int]:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001
        return None
    value = (head.get('Metadata') or {}).get(MTIME_METADATA_KEY)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def push_area(client, bucket: str, s3_prefix: str, area: Area, *, dry_run: bool = False,
              say: Callable[[str], None] = print) -> AreaReport:
    report = AreaReport(area.name)
    if not area.local_root.is_dir():
        report.skipped_reason = f"{area.local_root} does not exist"
        return report
    local = local_files(area)
    remote = remote_objects(client, bucket, s3_prefix, area)
    prefix = _full_prefix(s3_prefix, area.key_prefix)
    for rel, path in local.items():
        size, mtime = path.stat().st_size, int(path.stat().st_mtime)
        if remote.get(rel) == size:
            remote_mtime = _remote_mtime(client, bucket, prefix + rel)
            if remote_mtime is not None and mtime <= remote_mtime:
                report.up_to_date += 1
                continue
        action = 'UPDATE' if rel in remote else 'UPLOAD'
        say(f"[{action}] {area.key_prefix}/{rel}")
        if not dry_run:
            client.upload_file(str(path), bucket, prefix + rel,
                               ExtraArgs={'Metadata': {MTIME_METADATA_KEY: str(mtime)}})
        report.transferred.append(rel)
    report.remote_only = sorted(set(remote) - set(local))
    for rel in report.remote_only:
        say(f"[S3 ONLY] {area.key_prefix}/{rel}  (not deleted; a moved or removed file?)")
    return report


def pull_area(client, bucket: str, s3_prefix: str, area: Area, *, dry_run: bool = False,
              create_root: bool = True, say: Callable[[str], None] = print) -> AreaReport:
    report = AreaReport(area.name)
    if not area.local_root.is_dir():
        if not create_root:
            report.skipped_reason = f"{area.local_root} does not exist"
            return report
        if not dry_run:
            area.local_root.mkdir(parents=True, exist_ok=True)
    local = local_files(area)
    remote = remote_objects(client, bucket, s3_prefix, area)
    prefix = _full_prefix(s3_prefix, area.key_prefix)
    for rel, size in sorted(remote.items()):
        target = area.local_root / rel
        remote_mtime = _remote_mtime(client, bucket, prefix + rel)
        if target.is_file() and target.stat().st_size == size and (
                remote_mtime is None or int(target.stat().st_mtime) >= remote_mtime):
            report.up_to_date += 1
            continue
        say(f"[DOWNLOAD] {area.key_prefix}/{rel}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + '.partial')
            client.download_file(bucket, prefix + rel, str(tmp))
            os.replace(tmp, target)
            if remote_mtime is not None:
                os.utime(target, (remote_mtime, remote_mtime))
        report.transferred.append(rel)
    report.local_only = sorted(set(local) - set(remote))
    return report


def summarize(reports: List[AreaReport], verb: str) -> List[str]:
    lines = []
    for r in reports:
        if r.skipped_reason:
            lines.append(f"{r.name}: skipped ({r.skipped_reason})")
            continue
        extra = ''
        if r.remote_only:
            extra += f", {len(r.remote_only)} only on S3"
        if r.local_only:
            extra += f", {len(r.local_only)} only local"
        lines.append(f"{r.name}: {verb} {len(r.transferred)}, up to date {r.up_to_date}{extra}")
    return lines
