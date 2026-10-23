"""The artefact store in ``ant-sync pull`` (``doc/DESIGN_artefacts.md`` section 11, phase A4).

The watcher writes every artefact object to S3 itself (with ``x-amz-meta-sha256``) and commits the head
records (``artefact.md``) and manifests to the h5adstore repository. ant-sync therefore never pushes anything
under ``<Group>/static/artefacts/`` and never writes a head record or a manifest (they arrive with the
h5adstore checkout; writing them here would make the next ``git pull`` refuse to overwrite untracked files).
What a pull does, driven by the local head records and manifests:

* inline files of live versions (``figure`` artefacts, and any file of at most ``artefacts_inline_max_mb``)
  and the previews the manifest lists are downloaded when missing or different, and verified against the
  sha256 the manifest records;
* other files only when ``--artefacts PATTERN`` selects their artefact (``fnmatch`` against
  ``<group_id>/<scope>/<artefact_id>`` and ``...@v<N>``), with the same check;
* the local files and previews of deleted versions (an admin's hard purge) are deleted; soft-purged versions keep theirs;
* a live version whose manifest is not in the checkout yet (or does not match the head record) is skipped
  with a note: pull the h5adstore repository first.

Input: the store root, an S3 client, the bucket and the artefacts' catalog prefix. Output: a plan (list of
:class:`Action`) and, with :func:`apply`, the files. ``--dry-run`` stops after the plan.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence

import artefacts

CHUNK = 1024 * 1024


@dataclass
class Action:
    """One step of an artefact pull: ``download`` (key to rel, verified), ``delete`` (a purged version's
    local folder) or ``note`` (nothing done, said why)."""
    action: str
    rel: str
    key: Optional[str] = None
    size: int = 0
    sha256: Optional[str] = None
    reason: str = ''


class UnsafePath(ValueError):
    """A path below ``static/artefacts/`` that goes through a symbolic link or leaves the artefact tree."""


def checked_path(store_root: Path, rel: str) -> Path:
    """The local path of ``rel`` (``<group>/static/artefacts/...``) when neither ``<group>/static`` nor any
    component below it is a symbolic link (lstat, also components that exist only in part) and the
    resolved path stays inside the resolved ``<group>/static/artefacts``; else :class:`UnsafePath` (security
    review round 3: a committed symlink must never make a pull delete or write outside the store)."""
    parts = PurePosixPath(rel).parts
    if len(parts) < 4 or tuple(parts[1:3]) != artefacts.ARTEFACTS_PARTS or '..' in parts or parts[0].startswith('.'):
        raise UnsafePath(f'{rel}: outside static/artefacts/')
    static = Path(store_root) / parts[0] / artefacts.ARTEFACTS_PARTS[0]
    base = static / artefacts.ARTEFACTS_PARTS[1]
    # the group folder itself may be a link (a mirror site's store); <group>/static and below may not
    if static.is_symlink() or base.is_symlink():
        raise UnsafePath(f'{rel}: {static.name} or static/artefacts is a symbolic link')
    current = base
    for part in parts[3:]:
        current = current / part
        if current.is_symlink():
            raise UnsafePath(f'{rel}: {current.relative_to(store_root)} is a symbolic link')
    resolved_base = base.resolve()
    resolved = current.resolve(strict=False)
    if resolved != resolved_base and resolved_base not in resolved.parents:
        raise UnsafePath(f'{rel}: resolves outside {base.relative_to(store_root)}')
    return current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _group_ids(store_root: Path) -> Dict[str, str]:
    """``folder -> group_id`` from each ``<Group>/index.md``."""
    out: Dict[str, str] = {}
    for index in sorted(store_root.glob('*/index.md')):
        try:
            meta = artefacts.parse_front_matter(index.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001 - an unreadable index names no group id
            continue
        if meta.get('group_id'):
            out[index.parent.name] = str(meta['group_id'])
    return out


def head_records(store_root: Path) -> List[artefacts.ArtefactPath]:
    """Every artefact with a head record in the checkout (collections only: ``_`` and ``.`` folders are not
    collections; ``_group`` scopes are included)."""
    out = []
    for head in sorted(store_root.glob(f'*/{artefacts.ARTEFACTS_PARTS[0]}/{artefacts.ARTEFACTS_PARTS[1]}/*/*/*/'
                                       f'{artefacts.HEAD_RECORD_NAME}')):
        group, scope, kind, artefact_id = head.parts[-7], head.parts[-4], head.parts[-3], head.parts[-2]
        if group.startswith(('_', '.')):
            continue
        try:
            out.append(artefacts.ArtefactPath(group, scope, kind, artefact_id))
        except ValueError:
            continue
    return out


def selected(pattern: Optional[str], ref: str, version: int) -> bool:
    if not pattern:
        return False
    return any(fnmatch.fnmatchcase(candidate, item.strip())
               for item in pattern.split(',') if item.strip()
               for candidate in (ref, f'{ref}@v{version}'))


def is_git_tracked_record(rel: str) -> bool:
    """Whether ``rel`` is a head record (``artefact.md``) or a version manifest (``v<N>/manifest.json``): files
    that come with the h5adstore ``git pull`` and are never downloaded (an untracked copy blocks the pull)."""
    parts = PurePosixPath(rel).parts
    if parts and parts[-1] == artefacts.HEAD_RECORD_NAME:
        return True
    return (len(parts) >= 2 and parts[-1] == artefacts.MANIFEST_NAME and parts[-2][:1] == 'v'
            and parts[-2][1:].isdigit())


def plan(store_root: Path, s3: Any, bucket: str, catalog_prefix: str, inline_max_bytes: int,
         pattern: Optional[str] = None) -> List[Action]:
    """What a pull would do (reads the checkout, lists preview prefixes and HEADs previews; writes nothing)."""
    store_root = Path(store_root)
    group_ids = _group_ids(store_root)
    actions: List[Action] = []
    for path in head_records(store_root):
        try:
            checked_path(store_root, str(path.head_rel))
        except UnsafePath as exc:
            actions.append(Action('note', str(path.head_rel), reason=f'skipped: {exc}'))
            continue
        try:
            head = artefacts.parse_head_record((store_root / str(path.head_rel)).read_text(encoding='utf-8'))
        except Exception as exc:  # noqa: BLE001
            actions.append(Action('note', str(path.head_rel), reason=f'head record unreadable ({exc})'))
            continue
        if artefacts.validate_head_record(head, path):
            actions.append(Action('note', str(path.head_rel), reason='head record not valid; skipped'))
            continue
        group_id = group_ids.get(path.group)
        ref = artefacts.format_ref(group_id, path.scope, path.artefact_id) if group_id else str(path.rel)
        for entry in head.get('versions') or []:
            number = int(entry['version'])
            version_dir = store_root / str(path.version_rel(number))
            if entry.get('state') == 'deleted':
                for folder in (version_dir / artefacts.FILES_DIR, version_dir / artefacts.PREVIEW_DIR):
                    if folder.exists():
                        actions.append(Action('delete', str(folder.relative_to(store_root)),
                                              reason=f'{ref}@v{number} is deleted'))
                continue
            if entry.get('state') not in ('live', 'purged'):     # a soft-purged version keeps its files
                continue
            try:
                manifest_path = checked_path(store_root, str(path.manifest_rel(number)))
            except UnsafePath as exc:
                actions.append(Action('note', str(path.manifest_rel(number)), reason=f'skipped: {exc}'))
                continue
            if not manifest_path.is_file() or _sha256(manifest_path) != entry.get('manifest_sha256'):
                actions.append(Action('note', str(path.manifest_rel(number)),
                                      reason=f'{ref}@v{number}: the manifest is not in the checkout (or differs '
                                             f'from the head record); pull the h5adstore repository first'))
                continue
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            wanted_all = selected(pattern, ref, number)
            for item in manifest.get('files') or []:
                inline = head.get('kind') == 'figure' or int(item['size']) <= inline_max_bytes
                if not (inline or wanted_all):
                    continue
                rel = path.file_rel(number, item['name'])
                local = store_root / str(rel)
                if local.is_file() and local.stat().st_size == int(item['size']) and _sha256(local) == item['sha256']:
                    continue
                actions.append(Action('download', str(rel), artefacts.s3_key(catalog_prefix, rel), int(item['size']),
                                      item['sha256'], 'inline' if inline else 'selected'))
            # previews are checked against the sha256 the manifest records (review round 3), never against the
            # object's own metadata; a preview the manifest does not list is not pulled
            if 'previews' not in manifest:
                actions.append(Action('note', str(path.version_rel(number)),
                                      reason=f'{ref}@v{number}: the manifest records no previews (written before '
                                             f'previews were recorded); previews of this version are not pulled'))
            for preview in manifest.get('previews') or []:
                try:
                    rel = path.preview_rel(number, str(preview['name']))
                    sha, size = str(preview['sha256']), int(preview.get('size') or 0)
                except (KeyError, TypeError, ValueError):
                    actions.append(Action('note', str(path.version_rel(number)), reason='a preview entry of the '
                                                                                        'manifest is not valid'))
                    continue
                local = store_root / str(rel)
                if local.is_file() and _sha256(local) == sha:
                    continue
                actions.append(Action('download', str(rel), artefacts.s3_key(catalog_prefix, rel), size, sha,
                                      'preview'))
    return actions


def apply(actions: Sequence[Action], store_root: Path, s3: Any, bucket: str, *, dry_run: bool = False,
          min_free_bytes: Optional[int] = None, log: Callable[[str], None] = print) -> Dict[str, int]:
    """Carry out a plan. Downloads go to a temp file, are verified against their sha256 and renamed; a
    mismatch keeps nothing. Deletes stay inside ``static/artefacts/``."""
    store_root = Path(store_root)
    counts = {'downloaded': 0, 'deleted': 0, 'failed': 0, 'skipped_no_room': 0, 'notes': 0}
    for action in actions:
        rel_parts = PurePosixPath(action.rel).parts
        if len(rel_parts) < 4 or tuple(rel_parts[1:3]) != artefacts.ARTEFACTS_PARTS or '..' in rel_parts:
            raise ValueError(f'refusing {action.action} outside static/artefacts/: {action.rel}')
        if action.action == 'note':
            counts['notes'] += 1
            log(f'[note]     {action.rel}: {action.reason}')
            continue
        try:
            target = checked_path(store_root, action.rel)
        except UnsafePath as exc:
            log(f'  REFUSED {action.rel}: {exc}')
            counts['failed'] += 1
            continue
        if action.action == 'download' and is_git_tracked_record(action.rel):
            log(f'  REFUSED {action.rel}: head records and manifests come from the h5adstore git repository')
            counts['failed'] += 1
            continue
        if action.action == 'delete':
            log(f'[{"would delete" if dry_run else "DELETE"}] {action.rel}  ({action.reason})')
            if not dry_run:
                shutil.rmtree(target, ignore_errors=True)
            counts['deleted'] += 1
            continue
        if min_free_bytes is not None:
            free = shutil.disk_usage(store_root).free
            if free - action.size < min_free_bytes:
                log(f'  SKIP {action.rel}: would push below min_free_space')
                counts['skipped_no_room'] += 1
                continue
        log(f'[{"would download" if dry_run else "DOWNLOAD"}] {action.rel}  ({action.size} bytes, {action.reason})')
        if dry_run:
            counts['downloaded'] += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            checked_path(store_root, action.rel)          # again after mkdir: nothing raced a link in
        except UnsafePath as exc:
            log(f'  REFUSED {action.rel}: {exc}')
            counts['failed'] += 1
            continue
        partial = target.with_name(f'.{target.name}.partial')
        digest = hashlib.sha256()
        try:
            body = s3.get_object(Bucket=bucket, Key=action.key)['Body']
            with open(partial, 'wb') as handle:
                for chunk in iter(lambda: body.read(CHUNK), b''):
                    digest.update(chunk)
                    handle.write(chunk)
        except Exception as exc:  # noqa: BLE001
            partial.unlink(missing_ok=True)
            log(f'  FAILED {action.rel}: {exc}')
            counts['failed'] += 1
            continue
        if digest.hexdigest() != action.sha256:
            partial.unlink(missing_ok=True)
            log(f'  FAILED {action.rel}: sha256 differs from the manifest; nothing kept')
            counts['failed'] += 1
            continue
        if target.is_symlink():
            partial.unlink(missing_ok=True)
            log(f'  REFUSED {action.rel}: the target is a symbolic link')
            counts['failed'] += 1
            continue
        os.replace(partial, target)
        counts['downloaded'] += 1
    return counts
