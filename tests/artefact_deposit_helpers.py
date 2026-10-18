"""Fakes and builders for the artefact deposit tests (phase A2): a fake S3 that fails on any call the
artefact route must never make, a store with collections and datasets, the deposit service, the request
context and the watcher's publisher."""
from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import artefact_deposit
import artefact_pipeline
import artefacts
import dataset_deposit as dd
import ingest_watch
from tests.deposit_helpers import DepositFakeS3
from tests.ingest_watch_helpers import BUCKET, PREFIX, S3Error

CATALOG = '__ingest_test_catalog/'
STAGING = '__deposits_test/'
SITE = 'testsite'
STAGING_PREFIX = f'{STAGING}{SITE}/'

ANALYST = dd.Identity(user='ana', name='Ana Lyst', email='ana@example.org')
OTHER = dd.Identity(user='otto', name='Otto Other', email='otto@example.org')
ADMIN = dd.Identity(user='root', name='Root Admin', email='root@example.org', is_admin=True)

H5AD_SHA = 'a' * 64
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 200
PDF = b'%PDF-1.7\n' + b'x' * 300


class ArtefactFakeS3(DepositFakeS3):
    """The deposit fake S3 with 404-shaped HEADs, metadata on PutObject, and a hard failure on any S3 call
    that is not implemented here (ACLs, bucket policies, batch deletes, ...)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        super().__init__(clock)
        self.presign_log: List[tuple] = []
        self.content_types: Dict[str, str] = {}

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f'the artefact route must not call S3 {name}')

    def head_object(self, Bucket: str, Key: str):  # noqa: N803
        self.calls.append(('head', Key))
        if Key not in self.objects:
            raise S3Error('404')
        obj = self.objects[Key]
        return {'ETag': f'"{obj["etag"]}"', 'ContentLength': len(obj['body']), 'Metadata': dict(obj['metadata'])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, Metadata: Optional[dict] = None,  # noqa: N803
                   ContentType: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:  # noqa: N803
        assert not kwargs, f'unexpected PutObject arguments {sorted(kwargs)}'
        self.calls.append(('put', Key))
        body = Body if isinstance(Body, bytes) else Body.encode()
        etag = self.put(Key, body, self.clock(), metadata=Metadata)
        if ContentType:
            self.content_types[Key] = ContentType
        return {'ETag': f'"{etag}"'}

    def copy_object(self, Bucket: str, Key: str, CopySource: dict, Metadata: Optional[dict] = None,  # noqa: N803
                    MetadataDirective: str = 'COPY', CopySourceIfMatch: Optional[str] = None,  # noqa: N803
                    ContentType: Optional[str] = None) -> None:  # noqa: N803
        super().copy_object(Bucket=Bucket, Key=Key, CopySource=CopySource, Metadata=Metadata,
                            MetadataDirective=MetadataDirective, CopySourceIfMatch=CopySourceIfMatch)
        if ContentType:
            self.content_types[Key] = ContentType

    def generate_presigned_url(self, operation: str, Params: dict, ExpiresIn: int):  # noqa: N803
        self.presign_log.append((operation, Params['Key']))
        return super().generate_presigned_url(operation, Params, ExpiresIn)

    def keys(self, prefix: str = '') -> List[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_store(root: Path) -> Path:
    """Public2021 (PU1): pub (public, 1.0.0 then 1.1.0) and int (internal); Atlas2026 (AT6) analysis, internal;
    Open2026 (OP6) analysis, public."""
    write(root / 'Public2021' / 'index.md', '---\ngroup: Public2021\ngroup_id: PU1\ndefault_dataset: pub\n---\n')
    write(root / 'Public2021' / 'pub.md',
          '---\nid: pub\ntitle: Public cells\nversion: 1.1.0\nsha256: ' + 'b' * 64 + '\nchangelog:\n'
          '- version: 1.0.0\n  date: 2026-01-01\n  changes: [first]\n'
          '- version: 1.1.0\n  date: 2026-02-01\n  previous_sha256: ' + H5AD_SHA + '\n  changes: [data]\n---\n')
    (root / 'Public2021' / 'pub.h5ad').write_bytes(b'h5ad')
    write(root / 'Public2021' / 'pub.h5ad.mus', 'access=public\norganism=human\n')
    write(root / 'Public2021' / 'int.md', '---\nid: int\ntitle: Internal cells\nversion: 1.0.0\n---\n')
    (root / 'Public2021' / 'int.h5ad').write_bytes(b'h5ad')
    write(root / 'Public2021' / 'int.h5ad.mus', 'access=internal\n')
    for folder, group_id, access in (('Atlas2026', 'AT6', 'internal'), ('Open2026', 'OP6', 'public')):
        write(root / folder / 'index.md', f'---\ngroup: {folder}\ngroup_id: {group_id}\ncollection_kind: analysis\n'
                                          f'title: {folder} analyses\nauthors: [Mark Fiers]\nyear: 2026\n'
                                          f'access: {access}\n---\n')
    return root


SERVED = {'PU1/pub': {'access': 'public'}, 'PU1/int': {'access': 'internal'}}


def make_service(tmp_path: Path, clock: List[float], s3: Optional[ArtefactFakeS3] = None,
                 artefact_settings: Optional[Dict[str, Any]] = None, **overrides: Any):
    s3 = s3 or ArtefactFakeS3(lambda: clock[0])
    store_root = tmp_path / 'store'
    if not store_root.exists():
        make_store(store_root)
    settings = dd.DepositSettings(enabled=overrides.pop('datasets_enabled', False), bucket=BUCKET, prefix=PREFIX,
                                  staging_prefix=STAGING_PREFIX, site_id=SITE,
                                  s3={'access_key_id': 'k', 'secret_access_key': 's'}, **overrides)
    state_dir = tmp_path / 'state'
    store = dd.DepositStore(state_dir / dd.DB_FILE)
    merged = {'artefacts_enabled': True, 'artefacts_max_file_gb': 100.0, 'artefacts_max_deposit_gb': 200.0,
              'artefacts_max_files': 50, 'artefacts_max_versions': 100, 'artefacts_max_stored_gb_per_user': 1000.0,
              'artefacts_hold_public': True, 'artefacts_inline_max_mb': 2.0, 'artefacts_content_check_max_gb': 2.0,
              **(artefact_settings or {})}
    service = artefact_deposit.ArtefactDepositService(settings, store, s3, store_root, state_dir,
                                                      clock=lambda: clock[0], artefact_settings=merged,
                                                      background=lambda work: work())
    return service, s3


def context(store_root: Path, viewer: Optional[artefacts.Viewer] = None,
            served: Optional[Dict[str, Dict[str, Any]]] = None) -> artefact_deposit.ArtefactContext:
    served = SERVED if served is None else served
    collections, _ = artefacts.scan_collections(store_root, {'Public2021': {'PU1'}})
    index = artefacts.ArtefactIndex.build(store_root, collections)
    folders = {db_id: 'Public2021' for db_id in served}
    policy = artefacts.AccessPolicy(collections, served, {'Public2021': list(served)})
    mds = {db_id: store_root / 'Public2021' / f'{db_id.split("/")[1]}.md' for db_id in served}
    return artefact_deposit.ArtefactContext(store_root=store_root, collections=collections, index=index,
                                            policy=policy, viewer=viewer or artefacts.Viewer(sees_internal=True,
                                                                                             user='ana'),
                                            dataset_md=mds, dataset_folder=folders)


def declared() -> Dict[str, Any]:
    return {'author': {'name': 'Ana Lyst', 'orcid': '0000-0002-1825-0097'}, 'created': '2026-10-12',
            'code': {'repository': 'https://gitlab.example/x', 'commit': '3f2a9c1'},
            'parameters': {'alpha': 0.05}, 'software': [{'name': 'python', 'version': '3.12.4'}]}


def file_entry(name: str, body: bytes, role: Optional[str] = None) -> Dict[str, Any]:
    entry = {'name': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'kind': 'artefact_file'}
    if role:
        entry['role'] = role
    return entry


def artefact_body(files: Dict[str, bytes], artefact_id: str = 'volcano', kind: str = 'figure', scope: str = 'pub',
                  data_project: str = 'Public2021', **block: Any) -> Dict[str, Any]:
    made_from = [] if scope == '_group' else [{'db_id': f'PU1/{scope}', 'dataset_version': '1.1.0'}]
    artefact = {'kind': kind, 'artefact_id': artefact_id, 'title': 'Volcano plot, treated vs control',
                'description': 'A volcano plot of the treated against the control donors, from the test data.',
                'access': 'inherit', 'made_from': made_from, 'declared': declared()}
    artefact.update(block)
    return {'deposit_kind': 'artefact', 'data_project': data_project, 'scope': scope, 'artefact': artefact,
            'files': [file_entry(name, body) for name, body in files.items()]}


def upload(s3: ArtefactFakeS3, answer: Dict[str, Any], payloads: Dict[str, bytes]) -> None:
    for entry in answer['files']:
        body = payloads[entry['name']]
        if entry['method'] == 'put':
            s3.client_put(entry['url'], body, entry['headers'])
            continue
        for part in entry['parts']:
            start = (part['part_number'] - 1) * entry['part_size']
            s3.client_upload_part(part['url'], body[start:start + part['size']])


def deposit(service, s3, identity, files: Dict[str, bytes], ctx=None, **kwargs: Any) -> Dict[str, Any]:
    """Create, upload and complete one artefact deposit; returns the create answer."""
    ctx = ctx or context(service.store_root, artefacts.Viewer(sees_internal=True, is_admin=identity.is_admin,
                                                               user=identity.user))
    answer = service.create(identity, artefact_body(files, **kwargs), ctx)
    upload(s3, answer, files)
    done = service.complete(identity, answer['deposit_id'])
    assert done['state'] == 'uploaded', done
    return answer


class FakeApi:
    """ant-serve for the publisher: rescan and GET succeed and are recorded."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def post_with_retry(self, path: str) -> Dict[str, Any]:
        self.calls.append(('POST', path))
        return {'status': 'success'}

    def _request(self, path: str, method: str = 'POST', timeout: float = 600) -> Dict[str, Any]:
        self.calls.append((method, path))
        return {'ok': True}


class FakeGit:
    """Records git commands; answers like git on a clean repository."""

    def __init__(self) -> None:
        self.commands: List[List[str]] = []

    def __call__(self, cmd):
        cmd = [str(part) for part in cmd]
        self.commands.append(cmd)

        class Result:
            returncode = 0
            stdout = 'abc123\n' if 'rev-parse' in cmd else ''
            stderr = ''
        return Result()


def make_publisher(tmp_path: Path, s3: ArtefactFakeS3, clock: List[float], settings: Optional[Dict[str, Any]] = None,
                   **kwargs: Any):
    db = ingest_watch.StateDB(tmp_path / 'state' / ingest_watch.DB_FILE)
    git = FakeGit()
    api = FakeApi()
    merged = {'artefacts_enabled': True, 'artefacts_max_file_gb': 100.0, 'artefacts_max_deposit_gb': 200.0,
              'artefacts_max_files': 50, 'artefacts_max_versions': 100, 'artefacts_hold_public': True,
              'artefacts_inline_max_mb': 2.0, 'artefacts_content_check_max_gb': 2.0, **(settings or {})}
    (tmp_path / 'store' / '.git').mkdir(exist_ok=True)
    publisher = artefact_pipeline.ArtefactPublisher(
        store_root=tmp_path / 'store', state_dir=tmp_path / 'state', work_root=tmp_path / 'work', s3=s3,
        bucket=BUCKET, incoming_prefix=PREFIX, catalog_prefix=CATALOG, artefact_settings=merged, db=db, api=api,
        settle_seconds=0, log=lambda message: None, clock=lambda: clock[0], run=git, site_id=SITE,
        server_version='2.47.0', **kwargs)
    return publisher, db, git, api


def listing(s3: ArtefactFakeS3, prefix: str = PREFIX) -> List[Dict[str, Any]]:
    return [{'Key': key, 'ETag': f'"{obj["etag"]}"', 'Size': len(obj['body']), 'LastModified': obj['mtime']}
            for key, obj in sorted(s3.objects.items()) if key.startswith(prefix)]


def poll_until_final(publisher, s3, clock, polls: int = 4) -> None:
    for _ in range(polls):
        clock[0] += 10
        publisher.poll(listing(s3))


def watch_row(db, deposit_id: str) -> Dict[str, Any]:
    return db.get(f'artefact:{deposit_id}') or {}
