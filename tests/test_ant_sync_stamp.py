"""ant-sync: x-amz-meta-sha256 on pushed .h5ad objects, and the stamp-sha256 backfill (fake S3, no deletes)."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from tests.deposit_helpers import DepositFakeS3

SCRIPT = Path(__file__).resolve().parent.parent / 'bin' / 'ops' / 'ant-sync'


@pytest.fixture(scope='module')
def ant_sync():
    loader = importlib.machinery.SourceFileLoader('ant_sync_stamp_test', str(SCRIPT))
    spec = importlib.util.spec_from_loader('ant_sync_stamp_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def dataset(store: Path, name: str, body: bytes, record: str | None = 'right') -> Path:
    group = store / 'G2024'
    group.mkdir(parents=True, exist_ok=True)
    h5ad = group / f'{name}.h5ad'
    h5ad.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    if record:
        (group / f'{name}.md').write_text(f'---\nid: {name}\nsha256: {sha if record == "right" else "ab" * 32}\n---\n')
    return h5ad


def test_push_metadata_carries_the_sha256_of_h5ad_files(ant_sync, tmp_path, capsys):
    h5ad = dataset(tmp_path, 'a', b'h5ad bytes', record='wrong')
    metadata = ant_sync._push_metadata(h5ad, 'G2024/a.h5ad', 123)
    assert metadata == {'original-mtime': '123', 'sha256': hashlib.sha256(b'h5ad bytes').hexdigest()}
    assert 'record another sha256' in capsys.readouterr().out
    duckdb = tmp_path / 'G2024' / 'a.duckdb'
    duckdb.write_bytes(b'x')
    assert ant_sync._push_metadata(duckdb, 'G2024/a.duckdb', 5) == {'original-mtime': '5'}


def test_stamp_plan_and_apply(ant_sync, tmp_path):
    s3 = DepositFakeS3()
    store = tmp_path / 'store'
    good = dataset(store, 'good', b'good bytes')
    stale = dataset(store, 'stale', b'stale bytes', record='wrong')
    differs = dataset(store, 'differs', b'local bytes')
    dataset(store, 'stamped', b'stamped bytes')
    for path in (good, stale):
        s3.put(f'G2024/{path.name}', path.read_bytes(), metadata={'original-mtime': '7'})
    s3.put('G2024/differs.h5ad', b'other bytes', metadata={})
    s3.put('G2024/stamped.h5ad', b'stamped bytes', metadata={'sha256': 'x'})
    s3.put('G2024/orphan.h5ad', b'no local', metadata={})
    s3.put('G2024/good.duckdb', b'not an h5ad')
    plan = {entry['rel']: entry for entry in ant_sync.stamp_plan(s3, 'bucket', '', store, None)}
    assert plan['G2024/good.h5ad']['action'] == 'stamp'
    assert plan['G2024/stamped.h5ad']['action'] == 'already'
    assert 'another sha256' in plan['G2024/stale.h5ad']['reason']
    assert 'not the S3 object' in plan['G2024/differs.h5ad']['reason']
    assert 'no local copy' in plan['G2024/orphan.h5ad']['reason']
    assert 'G2024/good.duckdb' not in plan
    etag_before = s3.objects['G2024/good.h5ad']['etag']
    result = ant_sync.stamp_apply(s3, 'bucket', plan['G2024/good.h5ad'])
    assert result['ok'] is True
    obj = s3.objects['G2024/good.h5ad']
    assert obj['metadata'] == {'original-mtime': '7', 'sha256': hashlib.sha256(b'good bytes').hexdigest()}
    assert obj['body'] == b'good bytes' and obj['etag'] == etag_before
    assert not [call for call in s3.calls if call[0] == 'delete']
    assert 'sha256' not in s3.objects['G2024/stale.h5ad']['metadata']
    filtered = ant_sync.stamp_plan(s3, 'bucket', '', store, 'G2024/st*')
    assert {entry['rel'] for entry in filtered} == {'G2024/stale.h5ad', 'G2024/stamped.h5ad'}


def test_stamp_refuses_an_object_that_changed_after_the_plan(ant_sync, tmp_path):
    s3 = DepositFakeS3()
    store = tmp_path / 'store'
    good = dataset(store, 'good', b'good bytes')
    s3.put('G2024/good.h5ad', good.read_bytes(), metadata={})
    entry = ant_sync.stamp_plan(s3, 'bucket', '', store, None)[0]
    s3.put('G2024/good.h5ad', b'changed meanwhile', metadata={})
    with pytest.raises(Exception):
        ant_sync.stamp_apply(s3, 'bucket', entry)
    assert 'sha256' not in s3.objects['G2024/good.h5ad']['metadata']


def test_large_objects_use_the_managed_copy(ant_sync, tmp_path, monkeypatch):
    s3 = DepositFakeS3()
    store = tmp_path / 'store'
    good = dataset(store, 'good', b'good bytes')
    s3.put('G2024/good.h5ad', good.read_bytes(), metadata={})
    entry = ant_sync.stamp_plan(s3, 'bucket', '', store, None)[0]
    monkeypatch.setattr(ant_sync, 'SINGLE_COPY_MAX', 1)
    assert ant_sync.stamp_apply(s3, 'bucket', entry)['ok'] is True
    assert s3.copy_extra_args['MetadataDirective'] == 'REPLACE' and s3.copy_extra_args['CopySourceIfMatch']


def test_cmd_stamp_dry_run_changes_nothing_then_applies(ant_sync, tmp_path, monkeypatch, capsys):
    import argparse
    s3 = DepositFakeS3()
    store = tmp_path / 'store'
    good = dataset(store, 'good', b'good bytes')
    s3.put('G2024/good.h5ad', good.read_bytes(), metadata={})
    monkeypatch.setattr(ant_sync, 'require_primary', lambda **_: None)
    monkeypatch.setattr(ant_sync, '_s3_client', lambda cfg: s3)
    monkeypatch.setattr(ant_sync, '_transfer_config', lambda: None)
    cfg = {'bucket': 'bucket', 'prefix': ''}
    ant_sync.cmd_stamp(argparse.Namespace(filter=None, dry_run=True), cfg, store)
    out = capsys.readouterr().out
    assert '[STAMP] G2024/good.h5ad' in out and 'Dry run: nothing changed.' in out
    assert 'sha256' not in s3.objects['G2024/good.h5ad']['metadata']
    ant_sync.cmd_stamp(argparse.Namespace(filter=None, dry_run=False), cfg, store)
    assert '[DONE] G2024/good.h5ad' in capsys.readouterr().out
    assert s3.objects['G2024/good.h5ad']['metadata']['sha256'] == hashlib.sha256(b'good bytes').hexdigest()
    assert not [call for call in s3.calls if call[0] == 'delete']
