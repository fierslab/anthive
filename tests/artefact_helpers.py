"""Builders for the artefact store tests: a store tree with collections, artefacts, head records and
manifests written exactly as the pipeline will write them (doc/DESIGN_artefacts.md 2.3 to 2.6), a strict
fake S3 for HEAD and presigned GETs, and an independent SigV4 query-signature verifier."""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qsl, quote, urlsplit

import artefacts

CONTENT_TYPES = {'.png': 'image/png', '.pdf': 'application/pdf', '.tsv': 'text/tab-separated-values',
                 '.csv': 'text/csv', '.html': 'text/html', '.svg': 'image/svg+xml', '.txt': 'text/plain',
                 '.ipynb': 'application/x-ipynb+json'}


def write_index_md(store: Path, folder: str, **meta: Any) -> Path:
    """``<store>/<folder>/index.md`` with the given front matter (and a short body)."""
    import yaml
    path = store / folder / 'index.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\n' + yaml.safe_dump(meta, sort_keys=False) + '---\nCollection notes.\n')
    return path


def write_dataset_md(store: Path, folder: str, stem: str, file_id: str) -> Path:
    path = store / folder / f'{stem}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nid: {file_id}\ntitle: {stem}\n---\n')
    return path


def manifest_for(folder: str, group_id: str, scope: str, kind: str, artefact_id: str, version: int,
                 files: Dict[str, bytes], access: str = 'inherit', title: Optional[str] = None,
                 roles: Optional[Dict[str, str]] = None, **overrides: Any) -> Dict[str, Any]:
    db_id = None if scope == artefacts.GROUP_SCOPE else f'{group_id}/{scope}'
    entries = []
    for index, (name, body) in enumerate(files.items()):
        suffix = '.' + name.rsplit('.', 1)[-1] if '.' in name else ''
        entries.append({'name': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                        'content_type': CONTENT_TYPES.get(suffix, 'application/octet-stream'),
                        'role': (roles or {}).get(name, 'main' if index == 0 else 'supporting')})
    manifest = {
        'schema': artefacts.SCHEMA,
        'ref': artefacts.format_ref(group_id, scope, artefact_id, version),
        'artefact_id': artefact_id, 'version': version, 'kind': kind,
        'group': folder, 'group_id': group_id, 'scope': scope, 'db_id': db_id,
        'title': title or f'{artefact_id} version {version}',
        'description': 'A test artefact. ' * 5,
        'change_note': None if version == 1 else 'Changed.',
        'supersedes': None if version == 1 else version - 1,
        'access': access,
        'files': entries,
        'made_from': [{'db_id': db_id, 'dataset_version': '1.0.0'}] if db_id else [],
        'declared': {'author': {'name': 'A. Analyst'}, 'created': '2026-10-12',
                     'code': {'none': 'test'}, 'software': [{'name': 'python', 'version': '3.12'}]},
        'recorded': {'uploaded_by': 'alice', 'published': '2026-10-16T10:00:00Z', 'site_id': 'test',
                     'server_version': '2.47.0', 'checks': {}},
        'de': None,
    }
    manifest.update(overrides)
    return manifest


def write_artefact(store: Path, folder: str, group_id: str, scope: str, kind: str, artefact_id: str,
                   versions: Sequence[Dict[str, Any]], owner: str = 'alice', local_files: bool = True,
                   previews: bool = False) -> Dict[str, Any]:
    """Write one artefact. Each ``versions`` item: ``files`` ({name: bytes}), optional ``access``, ``state``
    (live / purged = soft, files kept / deleted = tombstone), ``purged_by``, ``purged_by_admin``, ``manifest`` overrides. Returns
    ``{'path': ArtefactPath, 'manifests': {n: dict}, 'files': {n: {name: bytes}}}``."""
    path = artefacts.ArtefactPath(folder, scope, kind, artefact_id)
    root = store / str(path.rel)
    root.mkdir(parents=True, exist_ok=True)
    head_versions: List[Dict[str, Any]] = []
    manifests: Dict[int, Dict[str, Any]] = {}
    all_files: Dict[int, Dict[str, bytes]] = {}
    for number, spec in enumerate(versions, start=1):
        state = spec.get('state', 'live')
        manifest = manifest_for(folder, group_id, scope, kind, artefact_id, number, spec['files'],
                                access=spec.get('access', 'inherit'), **spec.get('manifest', {}))
        raw = (json.dumps(manifest, indent=2) + '\n').encode()
        entry: Dict[str, Any] = {'version': number, 'state': state,
                                 'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'published': '2026-10-16'}
        if state in ('purged', 'deleted'):
            entry.update({'purged': '2026-10-17', 'purged_by': spec.get('purged_by', owner),
                          'purge_reason': 'wrong contrast'})
            if spec.get('purged_by_admin'):
                entry['purged_by_admin'] = True
        if state == 'deleted':                      # a tombstone: no files, no manifest
            entry.update({'deleted': '2026-10-18', 'deleted_by': spec.get('deleted_by', 'root'),
                          'delete_reason': 'removed for good', 'deleted_by_admin': True})
        else:
            version_dir = root / f'v{number}'
            (version_dir / 'files').mkdir(parents=True, exist_ok=True)
            (version_dir / 'manifest.json').write_bytes(raw)
            if local_files:
                for name, body in spec['files'].items():
                    target = version_dir / 'files' / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
            if previews:
                for name in spec['files']:
                    target = version_dir / 'preview' / f'{name}.webp'
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b'RIFF\x00\x00\x00\x00WEBPVP8 preview')
        head_versions.append(entry)
        manifests[number] = manifest
        all_files[number] = dict(spec['files'])
    live = [v['version'] for v in head_versions if v['state'] == 'live']
    head = {'artefact_id': artefact_id, 'kind': kind, 'scope': scope, 'owner': owner, 'created': '2026-10-14',
            'title': manifests[max(live)]['title'] if live else manifests[1]['title'],
            'latest': max(live) if live else None, 'versions': head_versions}
    (root / artefacts.HEAD_RECORD_NAME).write_text(artefacts.render_head_record(head))
    return {'path': path, 'manifests': manifests, 'files': all_files, 'head': head, 'root': root}


class S3Missing(Exception):
    def __init__(self) -> None:
        super().__init__('Not Found')
        self.response = {'Error': {'Code': '404'}, 'ResponseMetadata': {'HTTPStatusCode': 404}}


class S3Down(Exception):
    def __init__(self) -> None:
        super().__init__('connection refused')
        self.response = {'Error': {'Code': 'RequestTimeout'}, 'ResponseMetadata': {'HTTPStatusCode': 500}}


class StrictFakeS3:
    """Answers only HEAD and presigned GETs; any other S3 call (put, ACL, delete, copy) fails the test."""

    def __init__(self, bucket: str = 'labbucket') -> None:
        self.bucket = bucket
        self.objects: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []
        self.down = False

    def put(self, key: str, body: bytes, sha256: Optional[str] = None) -> None:
        self.objects[key] = {'body': body, 'metadata': {'sha256': sha256} if sha256 else {}}

    def head_object(self, Bucket: str, Key: str) -> Dict[str, Any]:  # noqa: N803
        self.calls.append(('head_object', Bucket, Key))
        if self.down:
            raise S3Down()
        if Bucket != self.bucket or Key not in self.objects:
            raise S3Missing()
        obj = self.objects[Key]
        return {'ContentLength': len(obj['body']), 'Metadata': dict(obj['metadata']), 'ETag': '"x"'}

    def generate_presigned_url(self, ClientMethod: str, Params: Dict[str, Any], ExpiresIn: int,  # noqa: N803
                               HttpMethod: Optional[str] = None) -> str:
        self.calls.append(('generate_presigned_url', ClientMethod, dict(Params), ExpiresIn, HttpMethod))
        return f'https://s3.test/{Params["Bucket"]}/{Params["Key"]}?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=sig'

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f'the artefact read side must not call S3 {name}')


def verify_sigv4_query(url: str, method: str, secret_key: str, now: _dt.datetime) -> bool:
    """Independent check of an S3 presigned (SigV4 query) URL, as the object store does it: recompute the
    signature over method, path, query (without the signature) and host; check the expiry."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    signature = params.pop('X-Amz-Signature', '')
    if params.get('X-Amz-Algorithm') != 'AWS4-HMAC-SHA256':
        return False

    def enc(value: str) -> str:
        return quote(value, safe='-_.~')

    canonical_query = '&'.join(f'{enc(k)}={enc(v)}' for k, v in sorted(params.items()))
    signed_headers = params.get('X-Amz-SignedHeaders', '')
    if signed_headers != 'host':
        return False
    canonical_request = '\n'.join([method, quote(parts.path, safe='/-_.~'), canonical_query,
                                   f'host:{parts.netloc}\n', signed_headers, 'UNSIGNED-PAYLOAD'])
    amz_date = params['X-Amz-Date']
    _key_id, date, region, service, terminator = params['X-Amz-Credential'].split('/')
    scope = '/'.join([date, region, service, terminator])
    string_to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                                hashlib.sha256(canonical_request.encode()).hexdigest()])
    key = ('AWS4' + secret_key).encode()
    for part in (date, region, service, terminator):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    issued = _dt.datetime.strptime(amz_date, '%Y%m%dT%H%M%SZ').replace(tzinfo=_dt.timezone.utc)
    within = now <= issued + _dt.timedelta(seconds=int(params['X-Amz-Expires']))
    return hmac.compare_digest(expected, signature) and within
