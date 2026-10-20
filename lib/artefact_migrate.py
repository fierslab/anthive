"""Migration of DE source tables (``<Group>/de_sources/``) into ``de_table`` artefacts (design section 13, A3).

Before artefacts existed the DE-only route kept the tables it imported under
``<Group>/de_sources/<basename>/<folder>/<file>`` (and older imports under ``<Group>/de_sources/<folder>/<file>``),
each with its ``<file>.mus``. This module turns each table into version 1 of a ``de_table`` artefact of its
dataset, without importing anything again (the study is already in the dataset's duckdb):

* the dataset: ``de_dataset_id`` / ``de_dataset_h5ad`` of the sidecar, else the ``<basename>`` folder;
* the study id: ``de_study_id``, else the file name (the import's rule); whether the served duckdb has it;
* ``made_from``: the dataset version whose changelog first names the study, else the current version (marked);
* ``declared``: from the sidecar's ``de_*`` keys; ``code: {none: "imported before artefacts existed"}``;
  ``recorded.uploaded_by: migration``;
* files: the table byte for byte, and a cleaned sidecar with only the ``de_*`` keys, ``sha256`` and
  ``data_project`` (paths, ELN ids and S3 keys of the original upload are dropped and listed in the plan).

:func:`plan` reads only (never the network, never a lock file). :func:`apply` writes the artefacts: each file
to S3 under the version key (HEAD first, never overwriting), the manifest last, the head record, local copies,
and commits the head records and manifests by path. ``de_sources/`` is never removed here: that is a separate,
reviewed commit after the served check (design 13).

Input: the store root (and for apply an S3 client, bucket and catalog prefix). Output: a plan (list of dicts)
and, for apply, the artefact files.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import artefact_de
import artefacts

KEEP_SIDECAR_KEYS = tuple(artefact_de.DE_KEYS) + ('de_map_', 'de_row_count', 'sha256', 'data_project')
MIGRATION_USER = 'migration'


@dataclass
class Item:
    """One table to migrate (or a reason it cannot be)."""
    group: str
    table: Path
    sidecar: Path
    db_id: Optional[str] = None
    md: Optional[Path] = None
    study_id: Optional[str] = None
    artefact_id: Optional[str] = None
    made_from_version: Optional[str] = None
    made_from_basis: str = ''
    study_in_duckdb: Optional[bool] = None
    rows: Optional[int] = None
    fingerprint: Optional[str] = None
    dropped_keys: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    skip: Optional[str] = None

    def ref(self) -> Optional[str]:
        if not self.db_id or not self.artefact_id:
            return None
        group_id, file_id = self.db_id.split('/')
        return artefacts.format_ref(group_id, file_id, self.artefact_id)

    def as_dict(self, store_root: Path) -> Dict[str, Any]:
        return {'group': self.group, 'table': str(self.table.relative_to(store_root)),
                'sidecar': str(self.sidecar.relative_to(store_root)), 'db_id': self.db_id, 'ref': self.ref(),
                'study_id': self.study_id, 'study_in_duckdb': self.study_in_duckdb, 'rows': self.rows,
                'fingerprint': self.fingerprint,
                'made_from': self.made_from_version, 'made_from_basis': self.made_from_basis,
                'sidecar_keys_dropped': self.dropped_keys, 'size': self.table.stat().st_size,
                'sha256': hashlib.sha256(self.table.read_bytes()).hexdigest(), 'problems': self.problems,
                'skip': self.skip}


def _datasets(store_root: Path) -> Dict[str, Tuple[str, Path]]:
    """``group/basename -> (db_id, md)`` for every served (not inactive) dataset."""
    import dataset_deposit  # noqa: PLC0415
    from h5adstore import catalog_dataset_mds  # noqa: PLC0415

    out: Dict[str, Tuple[str, Path]] = {}
    for _item, resolved, rel in catalog_dataset_mds(store_root):
        if len(rel.parts) != 2:
            continue
        db_id = dataset_deposit.dataset_db_id(store_root, rel.parts[0], resolved.stem)
        if db_id and artefacts.DB_ID_RE.fullmatch(db_id):
            out[f'{rel.parts[0]}/{resolved.stem}'] = (db_id, resolved)
    return out


def clean_sidecar(text: str) -> Tuple[str, List[str]]:
    """The sidecar with only the DE keys (and sha256, data_project); returns (text, dropped key names)."""
    from ingest_pipeline import parse_sidecar_text  # noqa: PLC0415
    meta = parse_sidecar_text(text)
    kept, dropped = [], []
    for key, value in meta.items():
        if key.startswith(KEEP_SIDECAR_KEYS) or key in KEEP_SIDECAR_KEYS:
            kept.append(f'{key}={value}')
        else:
            dropped.append(key)
    return '\n'.join(kept) + '\n', sorted(dropped)


def _study_rows(duckdb_path: Path, study: str) -> Tuple[Optional[bool], Optional[int]]:
    if not duckdb_path.is_file():
        return None, None
    import duckdb  # noqa: PLC0415
    try:
        connection = duckdb.connect(str(duckdb_path), read_only=True)
    except Exception:  # noqa: BLE001 - held by a writer: unknown
        return None, None
    try:
        tables = {row[0] for row in connection.execute('SELECT table_name FROM information_schema.tables').fetchall()}
        if '__de_study' not in tables:
            return False, 0
        present = connection.execute('SELECT count(*) FROM __de_study WHERE study_id = ?', [study]).fetchone()[0]
        rows = connection.execute('SELECT count(*) FROM __de WHERE study_id = ?', [study]).fetchone()[0]
        return bool(present), int(rows)
    finally:
        connection.close()


def names_study(text: str, study: str) -> bool:
    """Does a changelog line name ``study`` as a whole id (not as part of a longer id such as
    ``<study>_v2``)?"""
    return re.search(rf'(?<![A-Za-z0-9_.\-]){re.escape(study)}(?![A-Za-z0-9_\-]|\.[A-Za-z0-9])', text) is not None


def _fingerprint(duckdb_path: Path, study: str) -> Optional[str]:
    """The study's fingerprint (``artefact_pipeline.study_fingerprints``), so a later purge of the migrated
    artefact knows these rows are its own; None when the duckdb cannot be read."""
    try:
        from artefact_pipeline import study_fingerprints  # noqa: PLC0415
        return study_fingerprints(duckdb_path, [study]).get(study)
    except Exception:  # noqa: BLE001 - held by a writer: no fingerprint, a purge then asks an admin
        return None


def _made_from(meta: Dict[str, Any], study: str) -> Tuple[Optional[str], str]:
    for entry in meta.get('changelog') or []:
        if not isinstance(entry, dict):
            continue
        changes = entry.get('changes') or []
        changes = changes if isinstance(changes, list) else [changes]
        if any(names_study(str(change), study) for change in changes):
            return str(entry.get('version')), 'the changelog entry that names the study'
    return (str(meta['version']) if meta.get('version') else None,
            'the current version (no changelog entry names the study)')


def _retired(group_dir: Path, declared_id: Optional[str], basename: Optional[str]) -> Optional[str]:
    """The ``_``-prefixed folder (``_deleted_<date>/``, ...) holding a retired dataset with this file id or
    basename, else None."""
    from metadata import parse_md_file  # noqa: PLC0415
    for folder in sorted(path for path in group_dir.iterdir() if path.is_dir() and path.name.startswith('_')):
        for md in sorted(folder.glob('*.md')):
            if md.name == 'index.md' or md.name.endswith('.index.md'):
                continue
            if basename and md.stem == basename:
                return folder.name
            try:
                meta = parse_md_file(md)[0] or {}
            except Exception:  # noqa: BLE001
                continue
            if declared_id and str(meta.get('id') or '').upper() == declared_id:
                return folder.name
    return None


def plan(store_root: Path) -> List[Item]:
    """Every table under ``<Group>/de_sources/`` and what its migration would be. Reads only."""
    from artefact_deposit import alias_artefact_id  # noqa: PLC0415
    from ingest_pipeline import de_study_id, parse_sidecar_text  # noqa: PLC0415
    from metadata import parse_md_file  # noqa: PLC0415

    store_root = Path(store_root)
    datasets = _datasets(store_root)
    by_file_id: Dict[Tuple[str, str], Tuple[str, Path]] = {}
    for key, (db_id, md) in datasets.items():
        by_file_id[(key.split('/')[0], db_id.split('/')[1].upper())] = (db_id, md)
    items: List[Item] = []
    taken: Dict[str, str] = {}
    for group_dir in sorted(path for path in store_root.iterdir() if path.is_dir()):
        sources = group_dir / 'de_sources'
        if group_dir.name.startswith(('_', '.')) or not sources.is_dir():
            continue
        for table in sorted(sources.rglob('*')):
            if not table.is_file() or not artefact_de.is_table(table.name):
                continue
            sidecar = table.with_name(table.name + '.mus')
            item = Item(group=group_dir.name, table=table, sidecar=sidecar)
            items.append(item)
            if not sidecar.is_file():
                item.skip = 'no sidecar next to the table'
                continue
            meta = parse_sidecar_text(sidecar.read_text(encoding='utf-8', errors='replace'))
            rel = table.relative_to(sources).parts
            target = None
            declared, basename = None, None
            if meta.get('de_dataset_id'):
                declared = meta['de_dataset_id'].split('/')[-1].upper()
                target = by_file_id.get((group_dir.name, declared))
            elif meta.get('de_dataset_h5ad'):
                basename = meta['de_dataset_h5ad'][:-len('.h5ad')]
                target = datasets.get(f'{group_dir.name}/{basename}')
            elif len(rel) == 3:
                basename = rel[0]
                target = datasets.get(f'{group_dir.name}/{rel[0]}')
            if target is None:
                retired = _retired(group_dir, declared, basename)
                item.skip = (f'target dataset retired ({group_dir.name}/{retired}/): no artefact for it'
                             if retired else 'the dataset it belongs to is not served (or the sidecar names none)')
                continue
            item.db_id, item.md = target
            item.study_id = de_study_id(meta, table.name)
            item.artefact_id = alias_artefact_id(item.study_id)
            ref = item.ref()
            if ref in taken:
                item.skip = f'{ref} is also the artefact of {taken[ref]}'
                continue
            taken[ref] = str(table.relative_to(store_root))
            md_meta = parse_md_file(item.md)[0] or {}
            item.made_from_version, item.made_from_basis = _made_from(md_meta, item.study_id)
            item.study_in_duckdb, item.rows = _study_rows(item.md.with_suffix('.duckdb'), item.study_id)
            if item.study_in_duckdb:
                item.fingerprint = _fingerprint(item.md.with_suffix('.duckdb'), item.study_id)
            if item.study_in_duckdb is False:
                item.problems.append('the dataset duckdb does not hold this study (migrated without an import '
                                     'record)')
            item.dropped_keys = clean_sidecar(sidecar.read_text(encoding='utf-8', errors='replace'))[1]
            group_id, file_id = item.db_id.split('/')
            path = artefacts.ArtefactPath(group_dir.name, file_id, 'de_table', item.artefact_id)
            if (store_root / str(path.head_rel)).exists():
                item.skip = f'{ref} exists already (migrated before)'
    return items


def _manifest(store_root: Path, item: Item, files: Sequence[Tuple[str, bytes]], meta: Dict[str, str],
              site_id: str, server_version: str) -> Tuple[artefacts.ArtefactPath, Dict[str, Any]]:
    import artefact_deposit as rules  # noqa: PLC0415
    group_id, file_id = item.db_id.split('/')
    path = artefacts.ArtefactPath(item.group, file_id, 'de_table', item.artefact_id)
    md_meta = rules.read_md_meta(item.md)
    software = meta.get('de_software_version') or meta.get('de_method') or 'unknown'
    created = str(meta.get('created') or '')[:10]
    table_name, sidecar_name = files[0][0], files[1][0]
    manifest = {
        'schema': artefacts.SCHEMA, 'ref': artefacts.format_ref(group_id, file_id, item.artefact_id, 1),
        'artefact_id': item.artefact_id, 'version': 1, 'kind': 'de_table', 'group': item.group,
        'group_id': group_id, 'scope': file_id, 'db_id': item.db_id,
        'title': f'DE table {item.study_id}'[:artefacts.TITLE_MAX_CHARS],
        'description': (meta.get('de_description') or f'Differential expression results {item.study_id} of '
                        f'{item.db_id}.') + '\n\nMigrated from de_sources/ (imported before artefacts existed).',
        'change_note': None, 'supersedes': None, 'access': 'inherit',
        'files': [{'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                   'content_type': rules.content_type_for(name), 'role': 'main' if index == 0 else 'sidecar'}
                  for index, (name, data) in enumerate(files)],
        'made_from': [{'db_id': item.db_id, 'dataset_version': item.made_from_version,
                       'h5ad_sha256': rules.h5ad_sha256_at(md_meta, item.made_from_version or '')}],
        'declared': {'author': {'name': meta.get('contributor') or 'unknown (migrated)'},
                     'created': created if re.fullmatch(r'\d{4}-\d{2}-\d{2}', created) else None,
                     'software': [{'name': software, 'version': meta.get('de_software_version') or 'unknown'}],
                     'code': {'none': 'imported before artefacts existed'}},
        'recorded': {'uploaded_by': MIGRATION_USER, 'uploaded_by_name': 'ant-artefacts-migrate-de',
                     'deposit_id': None, 'published': _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                     'site_id': site_id, 'server_version': server_version, 'checks': {},
                     'migrated_from': str(item.table.relative_to(store_root)),
                     'made_from_basis': item.made_from_basis},
        'de': {'tables': [{'file': table_name, 'sidecar': sidecar_name, 'study_id': item.study_id,
                           **{key: meta[key] for key in artefact_de.DE_KEYS if meta.get(key)}}],
               'imported': {'study_ids': [item.study_id] if item.study_in_duckdb else [],
                            'rows': {item.study_id: item.rows} if item.study_in_duckdb else {},
                            'fingerprints': ({item.study_id: item.fingerprint}
                                             if item.study_in_duckdb and item.fingerprint else {}),
                            'imported_at': None, 'dataset_version_after': None, 'replaced': [], 'archived': [],
                            'note': 'imported by the DE-only route before artefacts existed'}},
        'previews': [],                  # a migration makes no previews (ant-sync expects the list)
    }
    problems = artefacts.validate_manifest(manifest, path, group_id, 1)
    if problems:
        raise ValueError(f'{item.table}: the manifest would not be valid: {"; ".join(problems)}')
    return path, manifest


def _absent(s3: Any, bucket: str, key: str) -> Optional[Dict[str, Any]]:
    """The HEAD of ``key``, or None when it is absent (404 only: any other error, 403 or 503 included, raises:
    a migration never writes on a guess)."""
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        response = getattr(exc, 'response', None)
        code = str(((response.get('Error') or {}).get('Code')) or '') if isinstance(response, dict) else ''
        if code in ('404', 'NoSuchKey', 'NotFound'):
            return None
        raise RuntimeError(f'HEAD {key} failed ({code or exc}); nothing written') from exc


def _put_once(s3: Any, bucket: str, key: str, data: bytes, metadata: Dict[str, str]) -> None:
    """Write ``key`` unless it exists; an existing key with the same sha256 is fine (a resumed run), any other
    content stops the migration."""
    head = _absent(s3, bucket, key)
    if head is not None:
        if (head.get('Metadata') or {}).get('sha256') != metadata['sha256']:
            raise RuntimeError(f'{key} exists with other content; not overwritten')
        return
    s3.put_object(Bucket=bucket, Key=key, Body=data, Metadata=metadata)


def _write_local_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise RuntimeError(f'{path} exists with other content; not overwritten')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def apply(store_root: Path, items: Sequence[Item], s3: Any, bucket: str, catalog_prefix: str,
          site_id: str = '', server_version: str = '', commit: Optional[Callable[[List[str], str], None]] = None,
          log: Callable[[str], None] = print) -> List[str]:
    """Write the artefacts of ``items`` (not skipped). Returns the refs written. Never overwrites a key."""
    from ingest_pipeline import parse_sidecar_text  # noqa: PLC0415
    store_root = Path(store_root)
    written: List[str] = []
    for item in items:
        if item.skip:
            continue
        text = item.sidecar.read_text(encoding='utf-8', errors='replace')
        cleaned, _dropped = clean_sidecar(text)
        meta = parse_sidecar_text(text)
        folder = item.table.parent.name
        table_name = f'{folder}/{item.table.name}' if artefacts.valid_segment(folder) else item.table.name
        files = [(table_name, item.table.read_bytes()), (table_name + '.mus', cleaned.encode('utf-8'))]
        path, manifest = _manifest(store_root, item, files, meta, site_id, server_version)
        if (store_root / str(path.head_rel)).exists() \
                or _absent(s3, bucket, artefacts.s3_key(catalog_prefix, path.head_rel)) is not None:
            raise RuntimeError(f'{item.ref()} exists already (head record); not migrated again')
        for name, data in files:
            key = artefacts.s3_key(catalog_prefix, path.file_rel(1, name))
            sha = hashlib.sha256(data).hexdigest()
            _put_once(s3, bucket, key, data, {'sha256': sha, 'artefact': manifest['ref']})
            if len(data) <= 2 * 1024 * 1024:
                _write_local_once(store_root / str(path.file_rel(1, name)), data)
        raw = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
        manifest_sha = hashlib.sha256(raw).hexdigest()
        manifest_key = artefacts.s3_key(catalog_prefix, path.manifest_rel(1))
        if _absent(s3, bucket, manifest_key) is not None:
            raise RuntimeError(f'{manifest_key} exists; a manifest is never overwritten')
        if (store_root / str(path.manifest_rel(1))).exists():
            raise RuntimeError(f'{path.manifest_rel(1)} exists locally; not overwritten')
        s3.put_object(Bucket=bucket, Key=manifest_key, Body=raw,
                      Metadata={'sha256': manifest_sha, 'artefact': manifest['ref']})
        _write_local_once(store_root / str(path.manifest_rel(1)), raw)
        today = _dt.date.today().isoformat()
        head = {'artefact_id': item.artefact_id, 'kind': 'de_table', 'scope': path.scope, 'owner': MIGRATION_USER,
                'created': today, 'title': manifest['title'], 'latest': 1,
                'versions': [{'version': 1, 'state': 'live', 'manifest_sha256': manifest_sha, 'published': today}]}
        assert not artefacts.validate_head_record(head, path)
        text_head = artefacts.render_head_record(head).encode('utf-8')
        (store_root / str(path.head_rel)).write_bytes(text_head)
        s3.put_object(Bucket=bucket, Key=artefacts.s3_key(catalog_prefix, path.head_rel), Body=text_head,
                      Metadata={'sha256': hashlib.sha256(text_head).hexdigest()})
        if commit is not None:
            commit([str(path.head_rel), str(path.manifest_rel(1))], f'artefact {manifest["ref"]} migrated from '
                                                                    f'{item.table.relative_to(store_root)}')
        log(f'migrated {item.table.relative_to(store_root)} -> {manifest["ref"]}')
        written.append(item.ref())
    return written
