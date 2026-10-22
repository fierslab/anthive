"""Fakes for the dataset deposit tests: an S3 with multipart uploads and presigned URLs, and a client that uses them."""
from __future__ import annotations

import hashlib
import io
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import dataset_deposit as dd
from tests.ingest_watch_helpers import BUCKET, PREFIX, FakeS3, S3Error

STORE_GROUP = 'Old2020'
STORE_DATASET = 'micro'
STORE_DB_ID = 'OL2/mic'


class DepositFakeS3(FakeS3):
    """FakeS3 plus CreateMultipartUpload / UploadPart / ListParts / Complete / Abort, GetObject and presigning.

    Presigned URLs are opaque tokens remembered with their signed parameters
    (``ContentLength``, ``Metadata``) and expiry; :meth:`client_put` and
    :meth:`client_upload_part` behave like S3 does for such a URL: a different
    size or missing signed header is refused (signature mismatch), an expired
    URL is refused.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        super().__init__()
        self.clock = clock
        self.uploads: Dict[str, Dict[str, Any]] = {}
        self.presigned: Dict[str, Dict[str, Any]] = {}

    # server side (boto3 surface) -------------------------------------------
    def create_multipart_upload(self, Bucket: str, Key: str, Metadata: Optional[dict] = None):  # noqa: N803
        upload_id = 'up_' + secrets.token_hex(6)
        self.uploads[upload_id] = {'key': Key, 'metadata': dict(Metadata or {}), 'parts': {}}
        self.calls.append(('create_multipart', Key))
        return {'UploadId': upload_id}

    def generate_presigned_url(self, operation: str, Params: dict, ExpiresIn: int):  # noqa: N803
        token = secrets.token_hex(8)
        self.presigned[token] = {'operation': operation, 'params': dict(Params), 'expires': self.clock() + ExpiresIn}
        query = {'token': token, 'X-Amz-Expires': ExpiresIn}
        return f'https://s3.test/{Params["Bucket"]}/{Params["Key"]}?{urlencode(query)}'

    def list_parts(self, Bucket: str, Key: str, UploadId: str, PartNumberMarker: int = 0,  # noqa: N803
                   MaxParts: int = 1000):  # noqa: N803
        if UploadId not in self.uploads:
            raise S3Error('NoSuchUpload')
        parts = [{'PartNumber': number, 'Size': len(body), 'ETag': f'"{hashlib.md5(body).hexdigest()}"'}
                 for number, body in sorted(self.uploads[UploadId]['parts'].items()) if number > PartNumberMarker]
        return {'Parts': parts[:MaxParts], 'IsTruncated': len(parts) > MaxParts,
                'NextPartNumberMarker': parts[MaxParts - 1]['PartNumber'] if len(parts) > MaxParts else 0}

    def complete_multipart_upload(self, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict):  # noqa: N803
        upload = self.uploads.pop(UploadId)
        bodies = []
        for part in MultipartUpload['Parts']:
            body = upload['parts'][part['PartNumber']]
            assert f'"{hashlib.md5(body).hexdigest()}"' == part['ETag']
            bodies.append(body)
        data = b''.join(bodies)
        digest = hashlib.md5(b''.join(hashlib.md5(body).digest() for body in bodies)).hexdigest()
        self.objects[Key] = {'body': data, 'etag': f'{digest}-{len(bodies)}', 'mtime': self.clock(),
                             'metadata': dict(upload['metadata'])}
        self.calls.append(('complete_multipart', Key))
        return {}

    def abort_multipart_upload(self, Bucket: str, Key: str, UploadId: str):  # noqa: N803
        if UploadId not in self.uploads:
            raise S3Error('NoSuchUpload')
        self.uploads.pop(UploadId)
        self.calls.append(('abort', Key))
        return {}

    #: True: behave like a store that ignores IfMatch / CopySourceIfMatch (accepts and does the request anyway)
    ignore_conditions = False

    def _condition(self, key: str, condition: Optional[str]) -> None:
        if condition is not None and not self.ignore_conditions and \
                f'"{self.objects[key]["etag"]}"' != condition:
            raise S3Error('PreconditionFailed')

    def get_object(self, Bucket: str, Key: str, IfMatch: Optional[str] = None,  # noqa: N803
                   Range: Optional[str] = None):  # noqa: N803
        if Key not in self.objects:
            raise S3Error('NoSuchKey')
        self._condition(Key, IfMatch)
        body = self.objects[Key]['body']
        if Range is not None:                       # bytes=<first>-<last>, inclusive
            first, last = (int(part) for part in Range.split('=')[1].split('-'))
            body = body[first:last + 1]
        return {'Body': io.BytesIO(body)}

    def copy_object(self, Bucket: str, Key: str, CopySource: dict, Metadata: Optional[dict] = None,  # noqa: N803
                    MetadataDirective: str = 'COPY', CopySourceIfMatch: Optional[str] = None,  # noqa: N803
                    ContentType: Optional[str] = None) -> None:  # noqa: N803
        self._condition(CopySource['Key'], CopySourceIfMatch)
        if self.before_copy:
            self.before_copy(CopySource['Key'])
        super().copy_object(Bucket=Bucket, Key=Key, CopySource=CopySource, Metadata=Metadata,
                            MetadataDirective=MetadataDirective)

    def copy(self, CopySource: dict, Bucket: str, Key: str, ExtraArgs: Optional[dict] = None, **kwargs: Any):  # noqa: N803
        extra = dict(ExtraArgs or {})
        self.copy_extra_args = extra
        self.copy_object(Bucket=Bucket, Key=Key, CopySource=CopySource, Metadata=extra.get('Metadata'),
                         MetadataDirective=extra.get('MetadataDirective', 'COPY'),
                         CopySourceIfMatch=extra.get('CopySourceIfMatch'))

    before_copy = None

    # the depositor's side: what an HTTP PUT to a presigned URL does ---------
    def _signed(self, url: str, operation: str) -> Dict[str, Any]:
        token = parse_qs(urlsplit(url).query)['token'][0]
        entry = self.presigned[token]
        assert entry['operation'] == operation
        if self.clock() > entry['expires']:
            raise S3Error('AccessDenied')               # Request has expired
        return entry['params']

    def client_put(self, url: str, body: bytes, headers: Dict[str, str]) -> str:
        params = self._signed(url, 'put_object')
        if int(params['ContentLength']) != len(body) or int(headers.get('Content-Length', len(body))) != len(body):
            raise S3Error('SignatureDoesNotMatch')
        for name, value in (params.get('Metadata') or {}).items():
            if headers.get(f'x-amz-meta-{name}') != value:
                raise S3Error('SignatureDoesNotMatch')
        return self.put(params['Key'], body, self.clock(), metadata=params.get('Metadata'))

    def client_upload_part(self, url: str, body: bytes) -> str:
        params = self._signed(url, 'upload_part')
        if int(params['ContentLength']) != len(body):
            raise S3Error('SignatureDoesNotMatch')
        self.uploads[params['UploadId']]['parts'][params['PartNumber']] = body
        return hashlib.md5(body).hexdigest()


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sidecar_for(h5ad_sha: str, **extra: str) -> bytes:
    fields = {'sha256': h5ad_sha, 'organism': 'human', 'tissue': 'brain', 'cell_type': 'microglial cell',
              'technology': '10x_chromium_3p_v3', 'suspension_type': 'nucleus', 'access': 'internal', 'source': 'external',
              'license': 'CC-BY-4.0', 'data_use': 'open'}
    fields.update(extra)
    return ''.join(f'{key}={value}\n' for key, value in fields.items() if value is not None).encode()


def file_entry(name: str, body: bytes, kind: str) -> Dict[str, Any]:
    return {'name': name, 'size': len(body), 'sha256': sha256_bytes(body), 'kind': kind}


def upload(s3: DepositFakeS3, instructions: Dict[str, Any], payloads: Dict[str, bytes]) -> None:
    """Upload every file the way a client follows the instructions of POST /datasets/deposit."""
    for entry in instructions['files']:
        body = payloads[entry['name']]
        if entry['method'] == 'put':
            s3.client_put(entry['url'], body, entry['headers'])
            continue
        for part in entry['parts']:
            start = (part['part_number'] - 1) * entry['part_size']
            s3.client_upload_part(part['url'], body[start:start + part['size']])


def make_store(root: Path) -> Path:
    """A store with one published dataset Old2020/micro (db id OL2/mic)."""
    group = root / STORE_GROUP
    group.mkdir(parents=True)
    (group / 'index.md').write_text('---\ngroup: Old2020\ngroup_id: OL2\n---\n# Old2020\n')
    (group / f'{STORE_DATASET}.md').write_text('---\nid: mic\ntitle: Old microglia\nversion: 1.0.0\n---\n')
    (group / f'{STORE_DATASET}.h5ad').write_bytes(b'old h5ad')
    return root


def make_service(tmp_path: Path, clock: List[float], s3: Optional[DepositFakeS3] = None,
                 **overrides: Any) -> dd.DepositService:
    s3 = s3 or DepositFakeS3(lambda: clock[0])
    store_root = tmp_path / 'store'
    if not store_root.exists():
        make_store(store_root)
    settings = dd.DepositSettings(enabled=True, bucket=BUCKET, prefix=PREFIX,
                                  s3={'access_key_id': 'k', 'secret_access_key': 's'},
                                  **{key: value for key, value in overrides.items()})
    state_dir = tmp_path / 'state'
    store = dd.DepositStore(state_dir / dd.DB_FILE)
    # the server's service class (artefacts off): every 2.46 deposit test also guards the 2.47 subclass
    import artefact_deposit  # noqa: PLC0415
    return artefact_deposit.ArtefactDepositService(settings, store, s3, store_root, state_dir, clock=lambda: clock[0])


ALICE = dd.Identity(user='alice', name='Alice Smith', email='alice@example.org')
BOB = dd.Identity(user='bob', name='Bob Jones', email='bob@example.org')
ADMIN = dd.Identity(user='root', name='Root Admin', email='root@example.org', is_admin=True)


def simple_deposit(h5ad: bytes = b'H5AD-DATA' * 100, group: str = 'New2026', basename: str = 'cells',
                   sidecar_extra: Optional[Dict[str, str]] = None, extra_files: Optional[Dict[str, tuple]] = None,
                   update_of: Optional[str] = None, with_h5ad: bool = True):
    """(request body, payloads) for one deposit: h5ad, sidecar, seed .md and index.md."""
    sidecar = sidecar_for(sha256_bytes(h5ad), **(sidecar_extra or {}))
    seed = b'---\ntitle: Test cells\nreplicate_unit: donor\n---\n\n' + b'Cells from a test. ' * 20
    index = b'---\ngroup: New2026\ngroup_id: NW6\nyear: 2026\n---\n# New2026\n'
    payloads = {f'{basename}.h5ad.mus': sidecar, f'{basename}.md': seed, 'index.md': index}
    files = [file_entry(f'{basename}.h5ad.mus', sidecar, 'mus'), file_entry(f'{basename}.md', seed, 'seed_md'),
             file_entry('index.md', index, 'index_md')]
    if with_h5ad:
        payloads[f'{basename}.h5ad'] = h5ad
        files.insert(0, file_entry(f'{basename}.h5ad', h5ad, 'h5ad'))
    for name, (body, kind) in (extra_files or {}).items():
        payloads[name] = body
        files.append(file_entry(name, body, kind))
    body = {'data_project': group, 'basename': basename, 'files': files}
    if update_of:
        body['update_of'] = update_of
    return body, payloads
