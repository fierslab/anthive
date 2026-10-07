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


# ── two-way PDF sync (ant sync library) ──────────────────────────────────
#
# Each site keeps a manifest of what both sides looked like at its last
# sync (<library>/.git/anthive-sync.json, or .anthive-sync.json when the
# library is not a plain git checkout). Comparing the current local files
# and S3 objects with that manifest tells which side changed:
#
#   local   remote  manifest   action
#   new     -       -          upload
#   -       new     -          download
#   same    same    -          record (first sync of identical files)
#   differ  differ  -          conflict
#   changed same    yes        upload
#   same    changed yes        download
#   changed changed yes        conflict (unless both now equal)
#   same    gone    yes        report; delete local with --propagate-deletes
#   gone    same    yes        report; delete remote with --propagate-deletes
#   moved (same sha, new path, old path gone) on one side -> move on the other
#
# A conflict keeps the local file and saves the remote one next to it as
# <name>.conflict-YYYYMMDD.pdf; --prefer local|remote resolves it. Remote
# changes are detected from the ETag and the sha256 stored in the object
# metadata, so nothing is downloaded to compare.

import datetime as _dt
import hashlib
import json
import re
import subprocess

SHA_METADATA_KEY = 'sha256'
MANIFEST_NAME = '.anthive-sync.json'
CONFLICT_RE = re.compile(r'\.conflict-\d{8}(-\d+)?\.pdf$', re.I)


def _sync_pdf_rel(rel: str) -> bool:
    return _pdf_rel(rel) and not CONFLICT_RE.search(rel)


def manifest_path(library_dir: Path) -> Path:
    meta_dir = Path(library_dir) / '.git'
    return meta_dir / MANIFEST_NAME.lstrip('.') if meta_dir.is_dir() else Path(library_dir) / MANIFEST_NAME


def load_manifest(library_dir: Path) -> Dict[str, Dict]:
    path = manifest_path(library_dir)
    try:
        return (json.loads(path.read_text()) or {}).get('files', {})
    except (OSError, ValueError):
        return {}


def save_manifest(library_dir: Path, files: Dict[str, Dict]) -> None:
    path = manifest_path(library_dir)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps({'version': 1, 'files': files}, indent=1, sort_keys=True))
    os.replace(tmp, path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class LocalPdf:
    path: Path
    sha: str
    size: int
    mtime: int


@dataclass
class RemotePdf:
    key: str
    size: int
    etag: str
    sha: Optional[str]
    mtime: Optional[int]


@dataclass
class Action:
    kind: str          # upload, download, conflict, record, move_remote, move_local,
                       # delete_local, delete_remote, report, resolve_local, resolve_remote, forget
    rel: str
    detail: str = ''
    old: Optional[str] = None


@dataclass
class PdfSyncReport:
    actions: List[Action] = field(default_factory=list)
    changed_local: List[Path] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)


def scan_local_pdfs(papers_dir: Path, manifest: Dict[str, Dict]) -> Dict[str, LocalPdf]:
    out: Dict[str, LocalPdf] = {}
    if not papers_dir.is_dir():
        return out
    for path in sorted(papers_dir.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(papers_dir).as_posix()
        if not _sync_pdf_rel(rel):
            continue
        stat = path.stat()
        known = manifest.get(rel) or {}
        if known.get('size') == stat.st_size and known.get('mtime') == int(stat.st_mtime) and known.get('sha'):
            sha = known['sha']        # unchanged since the last sync: no need to re-hash
        else:
            sha = file_sha256(path)
        out[rel] = LocalPdf(path, sha, stat.st_size, int(stat.st_mtime))
    return out


def scan_remote_pdfs(client, bucket: str, s3_prefix: str) -> Dict[str, RemotePdf]:
    prefix = _full_prefix(s3_prefix, LIBRARY_S3_PREFIX)
    out: Dict[str, RemotePdf] = {}
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            rel = obj['Key'][len(prefix):]
            if not rel or not _sync_pdf_rel(rel):
                continue
            try:
                head = client.head_object(Bucket=bucket, Key=obj['Key'])
                meta = head.get('Metadata') or {}
            except Exception:  # noqa: BLE001
                meta = {}
            mtime = meta.get(MTIME_METADATA_KEY)
            out[rel] = RemotePdf(obj['Key'], int(obj['Size']), str(obj.get('ETag', '')).strip('"'),
                                 meta.get(SHA_METADATA_KEY) or None,
                                 int(mtime) if mtime and str(mtime).isdigit() else None)
    return out


def _remote_sha(remote: RemotePdf, known: Optional[Dict]) -> Optional[str]:
    if remote.sha:
        return remote.sha
    if known and known.get('s3_etag') == remote.etag:
        return known.get('sha')
    return None


def plan_pdf_sync(local: Dict[str, LocalPdf], remote: Dict[str, RemotePdf], manifest: Dict[str, Dict], *,
                  propagate_deletes: bool = False, prefer: Optional[str] = None) -> List[Action]:
    """Decide what to do for every PDF path (see the table above)."""
    actions: List[Action] = []
    new_local: Dict[str, str] = {}          # rel -> sha, new on this side only
    new_remote: Dict[str, str] = {}
    gone_local: Dict[str, str] = {}         # rel -> manifest sha, deleted here, unchanged remotely
    gone_remote: Dict[str, str] = {}
    for rel in sorted(set(local) | set(remote) | set(manifest)):
        lo, re_, known = local.get(rel), remote.get(rel), manifest.get(rel)
        if known and known.get('moved_to') and lo is None:
            if re_ is not None:
                actions.append(Action('report', rel, f"old S3 key of a paper moved to {known['moved_to']}"
                                      f"{'' if propagate_deletes else ' (kept; --propagate-deletes removes it)'}"))
                if propagate_deletes:
                    actions[-1] = Action('delete_remote', rel, f"moved to {known['moved_to']}")
            else:
                actions.append(Action('forget', rel))
            continue
        if lo and re_ and not known:
            rsha = _remote_sha(re_, None)
            same = (rsha == lo.sha) if rsha else (re_.size == lo.size)
            actions.append(Action('record', rel, 'identical on both sides') if same
                           else _conflict(rel, prefer, 'different files on both sides, never synced'))
        elif lo and not re_ and not known:
            new_local[rel] = lo.sha
        elif re_ and not lo and not known:
            new_remote[rel] = re_.sha or ''
        elif lo and re_ and known:
            local_changed = lo.sha != known.get('sha')
            remote_changed = re_.etag != known.get('s3_etag') and _remote_sha(re_, known) != known.get('sha')
            if known.get('conflict') and not prefer:
                actions.append(Action('conflict', rel, 'unresolved conflict (use --prefer local|remote)'))
            elif not local_changed and not remote_changed:
                if known.get('conflict'):
                    actions.append(Action('record', rel, 'conflict resolved'))
                continue
            elif local_changed and not remote_changed:
                actions.append(Action('upload', rel, 'changed locally'))
            elif remote_changed and not local_changed:
                actions.append(Action('download', rel, 'changed on S3'))
            else:
                if _remote_sha(re_, known) == lo.sha:
                    actions.append(Action('record', rel, 'same change on both sides'))
                else:
                    actions.append(_conflict(rel, prefer, 'changed on both sides'))
        elif lo and not re_ and known:
            if lo.sha != known.get('sha'):
                actions.append(Action('upload', rel, 'deleted on S3 but changed locally: re-uploaded'))
            else:
                gone_remote[rel] = known.get('sha', '')
        elif re_ and not lo and known:
            remote_changed = re_.etag != known.get('s3_etag') and _remote_sha(re_, known) != known.get('sha')
            if remote_changed:
                actions.append(Action('download', rel, 'deleted locally but changed on S3: restored'))
            else:
                gone_local[rel] = known.get('sha', '')
        else:
            actions.append(Action('forget', rel))
    # moves: a new file on one side with the sha of a file that disappeared there
    for rel, sha in list(new_local.items()):
        old = next((o for o, osha in gone_local.items() if osha and osha == sha), None)
        if old:
            actions.append(Action('move_remote', rel, f"moved locally from {old}", old=old))
            del new_local[rel], gone_local[old]
    for rel, sha in list(new_remote.items()):
        old = next((o for o, osha in gone_remote.items() if sha and osha == sha), None)
        if old:
            actions.append(Action('move_local', rel, f"moved on S3 from {old}", old=old))
            del new_remote[rel], gone_remote[old]
    actions += [Action('upload', rel, 'new locally') for rel in new_local]
    actions += [Action('download', rel, 'new on S3') for rel in new_remote]
    for rel in gone_local:
        actions.append(Action('delete_remote', rel, 'deleted locally') if propagate_deletes
                       else Action('report', rel, 'deleted locally, still on S3 (--propagate-deletes removes it)'))
    for rel in gone_remote:
        actions.append(Action('delete_local', rel, 'deleted on S3') if propagate_deletes
                       else Action('report', rel, 'deleted on S3, still local (--propagate-deletes removes it)'))
    return actions


def _conflict(rel: str, prefer: Optional[str], why: str) -> Action:
    if prefer == 'local':
        return Action('resolve_local', rel, f"{why}: keeping local (--prefer local)")
    if prefer == 'remote':
        return Action('resolve_remote', rel, f"{why}: taking S3 copy (--prefer remote)")
    return Action('conflict', rel, why)


def _upload(client, bucket: str, key: str, pdf: LocalPdf) -> str:
    client.upload_file(str(pdf.path), bucket, key, ExtraArgs={
        'Metadata': {MTIME_METADATA_KEY: str(pdf.mtime), SHA_METADATA_KEY: pdf.sha},
        'ContentType': 'application/pdf'})
    return str(client.head_object(Bucket=bucket, Key=key).get('ETag', '')).strip('"')


def _download(client, bucket: str, remote: RemotePdf, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.partial")
    client.download_file(bucket, remote.key, str(tmp))
    sha = file_sha256(tmp)
    if remote.sha and sha != remote.sha:
        tmp.unlink(missing_ok=True)
        raise IOError(f"{remote.key}: downloaded sha256 {sha[:12]} does not match S3 metadata {remote.sha[:12]}")
    os.replace(tmp, target)
    if remote.mtime:
        os.utime(target, (remote.mtime, remote.mtime))
    return sha


def _entry(path: Path, sha: str, etag: str) -> Dict:
    stat = path.stat()
    return {'sha': sha, 'size': stat.st_size, 'mtime': int(stat.st_mtime), 's3_etag': etag, 's3_sha': sha}


REMOTE_WRITES = {'upload', 'resolve_local', 'move_remote', 'delete_remote'}


def sync_library_pdfs(client, bucket: str, s3_prefix: str, library_dir: Path, *, dry_run: bool = False,
                      propagate_deletes: bool = False, prefer: Optional[str] = None,
                      allow_remote_writes: bool = True,
                      today: Optional[str] = None, say: Callable[[str], None] = print) -> PdfSyncReport:
    """Two-way sync of ``<library>/papers/**.pdf`` with ``_library/papers/`` on S3.
    ``allow_remote_writes=False`` (a read-only site) downloads but reports what
    it would have uploaded or deleted on S3."""
    report = PdfSyncReport()
    library_dir = Path(library_dir)
    papers_dir = library_dir / 'papers'
    if not papers_dir.is_dir():
        report.errors.append(f"{papers_dir} does not exist (clone the library repo first)")
        return report
    manifest = load_manifest(library_dir)
    local = scan_local_pdfs(papers_dir, manifest)
    remote = scan_remote_pdfs(client, bucket, s3_prefix)
    prefix = _full_prefix(s3_prefix, LIBRARY_S3_PREFIX)
    actions = plan_pdf_sync(local, remote, manifest, propagate_deletes=propagate_deletes, prefer=prefer)
    if not allow_remote_writes:
        actions = [Action('report', a.rel, f"would {a.kind.replace('_', ' ')} on S3, but this site is read-only")
                   if a.kind in REMOTE_WRITES else a for a in actions]
    report.actions = actions
    stamp = today or _dt.date.today().strftime('%Y%m%d')
    for action in actions:
        if action.kind not in ('record', 'forget'):
            say(f"[{action.kind.upper()}] {action.rel}" + (f"  ({action.detail})" if action.detail else ''))
        if dry_run:
            continue
        rel = action.rel
        try:
            if action.kind in ('upload', 'resolve_local'):
                manifest[rel] = _entry(local[rel].path, local[rel].sha, _upload(client, bucket, prefix + rel, local[rel]))
            elif action.kind in ('download', 'resolve_remote'):
                target = papers_dir / rel
                sha = _download(client, bucket, remote[rel], target)
                manifest[rel] = _entry(target, sha, remote[rel].etag)
                report.changed_local.append(target)
            elif action.kind == 'record':
                lo, re_ = local[rel], remote[rel]
                etag = re_.etag
                if not re_.sha:   # legacy object: add the sha256 metadata server-side
                    client.copy_object(Bucket=bucket, Key=re_.key, CopySource={'Bucket': bucket, 'Key': re_.key},
                                       Metadata={MTIME_METADATA_KEY: str(re_.mtime or lo.mtime),
                                                 SHA_METADATA_KEY: lo.sha},
                                       MetadataDirective='REPLACE', ContentType='application/pdf')
                    etag = str(client.head_object(Bucket=bucket, Key=re_.key).get('ETag', '')).strip('"')
                manifest[rel] = _entry(lo.path, lo.sha, etag)
            elif action.kind == 'conflict':
                target = papers_dir / rel
                copy = target.with_name(f"{target.stem}.conflict-{stamp}.pdf")
                if not copy.exists() and rel in remote:
                    _download(client, bucket, remote[rel], copy)
                entry = dict(manifest.get(rel) or {})
                entry['conflict'] = True
                manifest[rel] = entry
            elif action.kind == 'move_remote':
                old_key = prefix + action.old
                client.copy_object(Bucket=bucket, Key=prefix + rel, CopySource={'Bucket': bucket, 'Key': old_key},
                                   MetadataDirective='COPY')
                etag = str(client.head_object(Bucket=bucket, Key=prefix + rel).get('ETag', '')).strip('"')
                manifest[rel] = _entry(local[rel].path, local[rel].sha, etag)
                if propagate_deletes:
                    client.delete_object(Bucket=bucket, Key=old_key)
                    manifest.pop(action.old, None)
                else:
                    manifest[action.old] = {**manifest.get(action.old, {}), 'moved_to': rel}
            elif action.kind == 'move_local':
                src, dst = papers_dir / action.old, papers_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
                manifest[rel] = _entry(dst, local[action.old].sha, remote[rel].etag)
                manifest.pop(action.old, None)
                report.changed_local.append(dst)
            elif action.kind == 'delete_remote':
                client.delete_object(Bucket=bucket, Key=prefix + rel)
                manifest.pop(rel, None)
            elif action.kind == 'delete_local':
                (papers_dir / rel).unlink(missing_ok=True)
                manifest.pop(rel, None)
            elif action.kind == 'forget':
                manifest.pop(rel, None)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            report.errors.append(f"{action.kind} {rel}: {type(exc).__name__}: {exc}")
            say(f"  ERROR {action.kind} {rel}: {exc}")
    if not dry_run:
        save_manifest(library_dir, manifest)
    return report


def push_one_pdf(client, bucket: str, s3_prefix: str, library_dir: Path, pdf: Path) -> str:
    """Upload one library PDF (after an API upload) and record it in the manifest."""
    library_dir = Path(library_dir)
    papers_dir = library_dir / 'papers'
    rel = Path(pdf).relative_to(papers_dir).as_posix()
    stat = Path(pdf).stat()
    local = LocalPdf(Path(pdf), file_sha256(Path(pdf)), stat.st_size, int(stat.st_mtime))
    etag = _upload(client, bucket, _full_prefix(s3_prefix, LIBRARY_S3_PREFIX) + rel, local)
    manifest = load_manifest(library_dir)
    manifest[rel] = _entry(local.path, local.sha, etag)
    save_manifest(library_dir, manifest)
    return rel


# ── records (git) and the index (publisher rule) ─────────────────────────

def _git(library_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(library_dir), *args], capture_output=True, text=True, timeout=300)


def git_sync(library_dir: Path, *, push: bool, dry_run: bool = False,
             say: Callable[[str], None] = print) -> Dict[str, str]:
    """Records are git: pull --rebase from the library remote, then push when
    allowed. Never forces; a conflict aborts the rebase and is reported."""
    library_dir = Path(library_dir)
    if not (library_dir / '.git').exists():
        # A plain folder (symlinked records, a copy): never run git in it, not
        # even when it happens to sit inside some other repository.
        return {'status': 'skipped', 'detail': f'{library_dir} is not a git checkout'}
    probe = _git(library_dir, 'rev-parse', '--is-inside-work-tree')
    if probe.returncode != 0 or probe.stdout.strip() != 'true':
        return {'status': 'skipped', 'detail': f'{library_dir} is not a git repository'}
    remotes = _git(library_dir, 'remote').stdout.split()
    if not remotes:
        return {'status': 'skipped', 'detail': 'the library repository has no remote yet'}
    if dry_run:
        return {'status': 'dry_run', 'detail': 'would git pull --rebase' + (' and git push' if push else '')}
    pulled = _git(library_dir, 'pull', '--rebase', '--autostash')
    if pulled.returncode != 0:
        _git(library_dir, 'rebase', '--abort')
        detail = (pulled.stderr or pulled.stdout).strip().splitlines()[-1:] or ['git pull failed']
        say(f"  git pull --rebase failed, nothing changed: {detail[0]}")
        return {'status': 'conflict', 'detail': detail[0]}
    if push:
        pushed = _git(library_dir, 'push')
        if pushed.returncode != 0:
            detail = (pushed.stderr or pushed.stdout).strip().splitlines()[-1:] or ['git push failed']
            return {'status': 'push_failed', 'detail': detail[0]}
        return {'status': 'ok', 'detail': 'pulled and pushed'}
    return {'status': 'ok', 'detail': 'pulled (push disabled: library_git_push is false)'}


def index_area(index_dir: Path) -> Area:
    return Area('literature index', Path(index_dir), INDEX_S3_PREFIX,
                lambda rel: rel in (INDEX_FILENAME, INDEX_FILENAME.replace('.parquet', '.json')))


def pull_index_if_newer(client, bucket: str, s3_prefix: str, index_dir: Path, *, dry_run: bool = False,
                        say: Callable[[str], None] = print) -> bool:
    """Download the published index only when there is none locally or the
    S3 copy is newer. Returns True when a new index arrived."""
    area = index_area(index_dir)
    local = Path(index_dir) / INDEX_FILENAME
    prefix = _full_prefix(s3_prefix, area.key_prefix)
    remote = remote_objects(client, bucket, s3_prefix, area)
    if INDEX_FILENAME not in remote:
        return False
    remote_mtime = _remote_mtime(client, bucket, prefix + INDEX_FILENAME)
    if local.is_file() and (remote_mtime is None or int(local.stat().st_mtime) >= remote_mtime):
        return False
    say(f"[DOWNLOAD] {area.key_prefix}/{INDEX_FILENAME}  ({'missing locally' if not local.is_file() else 'newer on S3'})")
    if dry_run:
        return True
    pull_area(client, bucket, s3_prefix, area, say=lambda msg: None)
    if remote_mtime is not None and local.is_file():
        os.utime(local, (remote_mtime, remote_mtime))
    return True


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
