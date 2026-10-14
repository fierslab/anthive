"""Fakes for the S3 watch route tests: an in-memory S3, a tool runner and an ant-serve stand-in."""
from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

PREFIX = '__incoming_test/'
CATALOG = '__cat/'
BUCKET = 'bucket'


class S3Error(Exception):
    """botocore ClientError look-alike (only ``response['Error']['Code']``)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {'Error': {'Code': code}}


class FakeS3:
    """Enough of the boto3 S3 client for the pipeline: list, head, get, put, copy, delete_object."""

    def __init__(self):
        self.objects: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []
        self.list_error: Optional[Exception] = None
        self.drop_metadata = False
        self.corrupt_upload = False          # store reversed bytes (same size, other MD5)
        self.if_match_supported = True
        self.copy_error: Optional[Exception] = None
        self.before_delete = None           # hook(key) called right before a delete is decided

    def put(self, key: str, body: bytes, mtime: Optional[float] = None, metadata: Optional[dict] = None) -> str:
        etag = hashlib.md5(body).hexdigest()
        self.objects[key] = {'body': body, 'etag': etag, 'mtime': mtime if mtime is not None else time.time(),
                             'metadata': dict(metadata or {})}
        return etag

    def put_file(self, key: str, path: Path, mtime: Optional[float] = None) -> str:
        return self.put(key, Path(path).read_bytes(), mtime)

    # boto3 surface
    def get_paginator(self, name: str):
        assert name == 'list_objects_v2'
        fake = self

        class Paginator:
            def paginate(self, Bucket: str, Prefix: str = ''):  # noqa: N803
                if fake.list_error:
                    raise fake.list_error
                fake.calls.append(('list', Prefix))
                contents = [{'Key': key, 'ETag': f'"{obj["etag"]}"', 'Size': len(obj['body']),
                             'LastModified': obj['mtime']} for key, obj in sorted(fake.objects.items())
                            if key.startswith(Prefix)]
                return [{'Contents': contents}]
        return Paginator()

    def head_object(self, Bucket: str, Key: str):  # noqa: N803
        self.calls.append(('head', Key))
        if Key not in self.objects:
            raise KeyError(Key)
        obj = self.objects[Key]
        return {'ETag': f'"{obj["etag"]}"', 'ContentLength': len(obj['body']), 'Metadata': dict(obj['metadata'])}

    def download_file(self, bucket: str, key: str, dest: str, Config: Any = None) -> None:  # noqa: N803
        self.calls.append(('download', key))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.objects[key]['body'])

    def upload_file(self, src: str, bucket: str, key: str, Config: Any = None, ExtraArgs: Any = None) -> None:  # noqa: N803
        self.calls.append(('upload', key))
        metadata = {} if self.drop_metadata else dict((ExtraArgs or {}).get('Metadata') or {})
        body = Path(src).read_bytes()
        self.put(key, body[::-1] if self.corrupt_upload else body, metadata=metadata)

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:  # noqa: N803
        self.calls.append(('put', Key))
        self.put(Key, Body if isinstance(Body, bytes) else Body.encode())

    def copy(self, CopySource: dict, Bucket: str, Key: str, **kwargs: Any) -> None:  # noqa: N803
        if self.copy_error:
            raise self.copy_error
        self.calls.append(('copy', CopySource['Key'], Key))
        source = self.objects[CopySource['Key']]
        self.put(Key, source['body'], metadata=source['metadata'])

    def copy_object(self, Bucket: str, Key: str, CopySource: dict, Metadata: Optional[dict] = None,  # noqa: N803
                    MetadataDirective: str = 'COPY') -> None:  # noqa: N803
        self.calls.append(('copy_object', CopySource['Key'], Key))
        source = self.objects[CopySource['Key']]
        etag = source['etag']
        self.objects[Key] = {**source, 'metadata': dict(Metadata or {}) if MetadataDirective == 'REPLACE'
                             else dict(source['metadata']), 'etag': etag}

    def delete_object(self, Bucket: str, Key: str, IfMatch: Optional[str] = None) -> None:  # noqa: N803
        if self.before_delete:
            self.before_delete(Key)
        if IfMatch is not None:
            if not self.if_match_supported:
                raise S3Error('NotImplemented')
            if Key not in self.objects:
                raise S3Error('NoSuchKey')
            if f'"{self.objects[Key]["etag"]}"' != IfMatch:
                raise S3Error('PreconditionFailed')
        self.calls.append(('delete', Key))
        self.objects.pop(Key, None)

    def delete_objects(self, **kwargs: Any) -> None:
        raise AssertionError('delete_objects must never be called by the watch route')

    @property
    def deleted(self) -> List[str]:
        return [call[1] for call in self.calls if call[0] == 'delete']


class FakeRunner:
    """Pretends to be ant-convert, ant-ingest-check, the post steps and git."""

    def __init__(self, report: Optional[dict] = None, convert_ok: bool = True):
        self.commands: List[List[str]] = []
        self.report = report or {'result': 'pass', 'publish': 'auto', 'held_reasons': [], 'checks': []}
        self.convert_ok = convert_ok
        self.convert_code: Optional[int] = None      # e.g. -9: killed
        self.check_code: Optional[int] = None
        self.git_commit_code = 0
        self.log_path = None
        self.failing_steps: set = set()

    def run(self, label: str, cmd, cwd=None, timeout=None):
        cmd = [str(part) for part in cmd]
        self.commands.append(cmd)
        tool = Path(cmd[0]).name
        if tool == 'ant-convert':
            if self.convert_code is not None:
                return self.convert_code, 'killed'
            if not self.convert_ok:
                return 1, 'convert exploded'
            h5ad = Path(cmd[1])
            h5ad.with_suffix('.duckdb').write_bytes(b'duckdb:' + h5ad.read_bytes()[:16])
            (h5ad.parent / f'{h5ad.stem}.layer.X.by_gene.parquet').write_bytes(b'parquet')
            return 0, 'converted'
        if tool == 'ant-ingest-check':
            if self.check_code is not None:
                return self.check_code, 'killed'
            out = Path(cmd[cmd.index('--json') + 1])
            report = dict(self.report)
            report.setdefault('dataset', {})
            out.write_text(json.dumps(report))
            return (1 if report.get('result') == 'fail' else 0), 'checked'
        if label.startswith('post:') and label[5:] in self.failing_steps:
            return 1, 'step failed'
        if tool == 'git' and 'rev-parse' in cmd:
            return 0, 'abc123'
        if tool == 'git' and 'commit' in cmd:
            return self.git_commit_code, 'commit output'
        if tool == 'git' and 'status' in cmd:
            return 0, ' M changed.md'
        return 0, ''

    def tools(self) -> List[str]:
        return [Path(cmd[0]).name for cmd in self.commands]


class FakeApi:
    """ant-serve stand-in: rescan lists the datasets found in the store."""

    def __init__(self, store: Path, down: bool = False):
        self.store = store
        self.calls: List[str] = []
        self.down = down
        self.hide: set = set()
        self.on_rescan = None
        self.stale_info = False
        self.release_fails = False

    def _ids(self) -> List[str]:
        import yaml
        ids = []
        for md in self.store.rglob('*.md'):
            if md.name == 'index.md' or any(part.startswith('_') for part in md.relative_to(self.store).parts):
                continue
            text = md.read_text()
            meta = yaml.safe_load(text.split('---\n')[1]) or {}
            index = md.parent / 'index.md'
            if not index.exists():
                index = md.parent.parent / 'index.md'
            group = (yaml.safe_load(index.read_text().split('---\n')[1]) or {}).get('group_id') if index.exists() else None
            if group and meta.get('id') and md.with_suffix('.duckdb').exists():
                ids.append(f'{group}/{meta["id"]}')
        return [db_id for db_id in ids if db_id not in self.hide]

    def rescan(self):
        from ingest_pipeline import TransientError
        self.calls.append('rescan')
        if self.on_rescan:
            self.on_rescan()
        if self.down:
            raise TransientError('server down')
        return {'databases': self._ids()}

    def release(self, db_id):
        from ingest_pipeline import TransientError
        self.calls.append(f'release:{db_id}')
        if self.release_fails:
            raise TransientError('release failed')
        return {}

    def info(self, db_id):
        import yaml
        from ingest_pipeline import TransientError
        self.calls.append(f'info:{db_id}')
        if self.down:
            raise TransientError('server down')
        for md in self.store.rglob('*.md'):
            if md.name == 'index.md' or any(part.startswith('_') for part in md.relative_to(self.store).parts):
                continue
            meta = yaml.safe_load(md.read_text().split('---\n')[1]) or {}
            if db_id.endswith('/' + str(meta.get('id'))):
                if self.stale_info:
                    return {'version': '0.0.1', 'sha256': 'stale'}
                return {'version': str(meta.get('version')), 'sha256': meta.get('sha256')}
        raise TransientError(f'{db_id} unknown')

    def restore(self, db_id):
        self.calls.append(f'restore:{db_id}')
        return {}


def make_h5ad(path: Path, n_cells: int = 6, seed: int = 0, extra_obs: Optional[str] = None) -> Path:
    """A tiny valid h5ad (log-norm X, integer counts layer, obs, a UMAP)."""
    import anndata
    import pandas as pd
    import scipy.sparse as sp

    rng = np.random.default_rng(seed)
    counts = rng.poisson(2.0, size=(n_cells, 5)).astype(np.float32)
    lognorm = np.log1p(counts / np.maximum(counts.sum(1, keepdims=True), 1) * 1e4).astype(np.float32)
    obs = pd.DataFrame({'donor': pd.Categorical([f'D{i % 3}' for i in range(n_cells)]),
                        'n_counts': counts.sum(1)}, index=[f'cell{i}' for i in range(n_cells)])
    if extra_obs:
        obs[extra_obs] = pd.Categorical(['x'] * n_cells)
    adata = anndata.AnnData(X=sp.csr_matrix(lognorm), obs=obs,
                            var=pd.DataFrame(index=['TREM2', 'APOE', 'CX3CR1', 'P2RY12', 'GFAP']))
    adata.layers['counts'] = sp.csr_matrix(counts)
    adata.obsm['X_umap'] = rng.normal(size=(n_cells, 2))
    path.parent.mkdir(parents=True, exist_ok=True)
    adata.write_h5ad(path)
    return path


def sidecar_text(sha: str, **extra: str) -> str:
    fields = {'organism': 'human', 'tissue': 'brain', 'cell_type': 'microglial cell', 'technology': '10x_chromium',
              'suspension_type': 'nucleus', 'access': 'internal', 'source': 'external', 'sha256': sha,
              'title': 'Watch test microglia', 'data_use': 'open', 'license': 'CC-BY-4.0'}
    fields.update(extra)
    return ''.join(f'{key}={value}\n' for key, value in fields.items() if value is not None)


def sha_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


INDEX_SEED = '---\ngroup: WatchTest2026\ngroup_id: WT6\nyear: 2026\ndoi: 10.0/test\npaper_title: Test\n' \
             'authors:\n- A Person\ndefault_dataset: micro\n---\n\n# WatchTest2026\n'
