"""ant-sync's artefact pull rules (lib/artefact_sync.py, design section 11, phase A4).

A primary publishes through the real watcher on a fake S3; a second site is simulated by a store that holds
only what the h5adstore checkout brings (index.md, head records and manifests). The pull then fetches inline
files, the files of selected artefacts on request, verifies every sha256 and deletes purged copies.
"""
from __future__ import annotations

import shutil
import time

import pytest

import artefact_sync
from tests.artefact_deposit_helpers import (ANALYST, CATALOG, PNG, BUCKET, context, deposit, listing,
                                            make_publisher, make_service, poll_until_final)

INTERNAL = {'scope': 'int', 'made_from': [{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]}
BIG = b'gene\tvalue\n' + b'Apoe\t1\n' * 400                      # 3 kB: "large" with a 1 kB inline limit
INLINE_MAX = 1024


@pytest.fixture
def clock():
    return [time.time()]


def _primary(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, _db, _commits, _api = make_publisher(tmp_path, s3, clock,
                                                    settings={'artefacts_inline_max_mb': INLINE_MAX / 1024 ** 2})
    deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    deposit(service, s3, ANALYST, {'big.tsv': BIG}, kind='table', artefact_id='big', **INTERNAL)
    poll_until_final(publisher, s3, clock)
    return service, s3, publisher


def _checkout(tmp_path):
    """What a git clone of the primary's h5adstore holds: no artefact files, no previews."""
    primary, second = tmp_path / 'store', tmp_path / 'second'
    for path in primary.rglob('*'):
        if path.is_file() and (path.name in ('index.md', 'artefact.md', 'manifest.json') or path.suffix == '.md'):
            target = second / path.relative_to(primary)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return second


def _files(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob('*') if path.is_file() and '/files/' in str(path))


def test_inline_files_are_pulled_and_large_ones_only_on_request(tmp_path, clock):
    _service, s3, _publisher = _primary(tmp_path, clock)
    second = _checkout(tmp_path)
    actions = artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX)
    downloads = [action for action in actions if action.action == 'download']
    assert [action.rel for action in downloads if action.reason != 'preview'] == \
        ['Public2021/static/artefacts/int/figure/volcano/v1/files/volcano.png']
    assert all('/preview/' in action.rel for action in downloads if action.reason == 'preview')
    counts = artefact_sync.apply(actions, second, s3, BUCKET, log=lambda message: None)
    assert counts['downloaded'] == len(downloads) and counts['failed'] == 0
    assert (second / downloads[0].rel).read_bytes() == PNG
    assert artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX) == []        # up to date
    selected = artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX, pattern='PU1/int/big')
    assert [action.reason for action in selected] == ['selected']
    artefact_sync.apply(selected, second, s3, BUCKET, log=lambda message: None)
    assert (second / selected[0].rel).read_bytes() == BIG
    assert artefact_sync.selected('PU1/*/big@v1', 'PU1/int/big', 1)
    assert not artefact_sync.selected('PU1/int/big@v2', 'PU1/int/big', 1)


def test_a_dry_run_changes_nothing(tmp_path, clock):
    _service, s3, _publisher = _primary(tmp_path, clock)
    second = _checkout(tmp_path)
    before = sorted(str(path) for path in second.rglob('*'))
    actions = artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX, pattern='*')
    counts = artefact_sync.apply(actions, second, s3, BUCKET, dry_run=True, log=lambda message: None)
    assert counts['downloaded'] == len(actions) >= 2
    assert sorted(str(path) for path in second.rglob('*')) == before


def test_a_file_whose_bytes_differ_from_the_manifest_is_not_kept(tmp_path, clock):
    _service, s3, _publisher = _primary(tmp_path, clock)
    second = _checkout(tmp_path)
    key = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/v1/files/volcano.png'
    s3.objects[key]['body'] = PNG + b'tampered'
    actions = artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX)
    counts = artefact_sync.apply(actions, second, s3, BUCKET, log=lambda message: None)
    assert counts['failed'] == 1 and _files(second) == []
    assert not list(second.rglob('*.partial'))


def test_purged_versions_are_deleted_locally(tmp_path, clock):
    service, s3, publisher = _primary(tmp_path, clock)
    second = _checkout(tmp_path)
    artefact_sync.apply(artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX), second, s3, BUCKET,
                        log=lambda message: None)
    assert _files(second)
    deposit(service, s3, ANALYST, {'volcano.png': PNG + b'2'}, new_version_of=1, change_note='v2', **INTERNAL)
    poll_until_final(publisher, s3, clock)
    service.request_purge(ANALYST, context(service.store_root), 'PU1', 'int', 'volcano', 'v1',
                          {'reason': 'an old version', 'confirm': 'PU1/int/volcano@v1'})
    publisher.poll(listing(s3))
    for name in ('artefact.md',):                       # the checkout catches up (git pull)
        rel = 'Public2021/static/artefacts/int/figure/volcano/' + name
        shutil.copy2(tmp_path / 'store' / rel, second / rel)
    v2 = 'Public2021/static/artefacts/int/figure/volcano/v2/manifest.json'
    (second / v2).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tmp_path / 'store' / v2, second / v2)
    actions = artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX)
    assert {action.action for action in actions} == {'delete', 'download'}
    artefact_sync.apply(actions, second, s3, BUCKET, log=lambda message: None)
    assert _files(second) == ['Public2021/static/artefacts/int/figure/volcano/v2/files/volcano.png']


def test_a_manifest_missing_from_the_checkout_is_only_noted(tmp_path, clock):
    _service, s3, _publisher = _primary(tmp_path, clock)
    second = _checkout(tmp_path)
    (second / 'Public2021/static/artefacts/int/figure/volcano/v1/manifest.json').unlink()
    actions = [action for action in artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX)
               if '/figure/volcano/' in action.rel]
    assert [action.action for action in actions] == ['note'] and 'pull the h5adstore' in actions[0].reason


def test_actions_outside_the_artefact_store_are_refused(tmp_path):
    with pytest.raises(ValueError):
        artefact_sync.apply([artefact_sync.Action('delete', 'Public2021/static/papers')], tmp_path, None, BUCKET,
                            log=lambda message: None)
