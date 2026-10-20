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


# ── security review round 3: symbolic links in the checkout ──────────────────────────────────────────────

class _ListingS3:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, name):
        objects = self.objects

        class Pages:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                yield {'Contents': [{'Key': key, 'Size': len(body)} for key, body in objects.items()
                                    if key.startswith(Prefix)]}
        return Pages()

    def head_object(self, Bucket, Key):  # noqa: N803
        import hashlib
        return {'Metadata': {'sha256': hashlib.sha256(self.objects[Key]).hexdigest()}}

    def get_object(self, Bucket, Key):  # noqa: N803
        import io
        return {'Body': io.BytesIO(self.objects[Key])}


def test_a_symlinked_purged_version_is_never_followed_by_the_delete(tmp_path):
    import os
    from tests.artefact_helpers import write_artefact, write_index_md
    store = tmp_path / 'store'
    outside = tmp_path / 'precious'
    (outside / 'files').mkdir(parents=True)
    (outside / 'files' / 'thesis.txt').write_text('years of work')
    write_index_md(store, 'Public2021', group='Public2021', group_id='PU1')
    info = write_artefact(store, 'Public2021', 'PU1', 'int', 'figure', 'volcano',
                          [{'files': {'v.png': b'\x89PNG'}, 'state': 'purged'}, {'files': {'w.png': b'\x89PNG2'}}])
    v1 = info['root'] / 'v1'
    shutil.rmtree(v1, ignore_errors=True)
    os.symlink(outside, v1)
    actions = artefact_sync.plan(store, _ListingS3({}), 'b', '', 2 * 1024 * 1024)
    counts = artefact_sync.apply(actions, store, _ListingS3({}), 'b', log=lambda message: None)
    assert (outside / 'files' / 'thesis.txt').read_text() == 'years of work'
    assert counts['deleted'] == 0 and counts['failed'] >= 1


def test_a_symlinked_files_folder_never_receives_a_download(tmp_path):
    import os
    from tests.artefact_helpers import write_artefact, write_index_md
    store = tmp_path / 'store'
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    write_index_md(store, 'Public2021', group='Public2021', group_id='PU1')
    body = b'\x89PNG payload'
    info = write_artefact(store, 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'v.png': body}}],
                          local_files=False)
    files_dir = info['root'] / 'v1' / 'files'
    shutil.rmtree(files_dir, ignore_errors=True)
    os.symlink(outside, files_dir)
    key = 'Public2021/static/artefacts/int/figure/volcano/v1/files/v.png'
    s3 = _ListingS3({key: body})
    counts = artefact_sync.apply(artefact_sync.plan(store, s3, 'b', '', 2 * 1024 * 1024), store, s3, 'b',
                                 log=lambda message: None)
    assert list(outside.iterdir()) == [] and counts['downloaded'] == 0 and counts['failed'] == 1


def test_a_symlinked_artefact_folder_is_skipped_at_planning(tmp_path):
    import os
    from tests.artefact_helpers import write_artefact, write_index_md
    store = tmp_path / 'store'
    elsewhere = tmp_path / 'other_store'
    write_index_md(store, 'Public2021', group='Public2021', group_id='PU1')
    info = write_artefact(elsewhere, 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'v.png': b'x'}}])
    target = store / 'Public2021' / 'static' / 'artefacts' / 'int' / 'figure'
    target.mkdir(parents=True)
    os.symlink(info['root'], target / 'volcano')
    actions = artefact_sync.plan(store, _ListingS3({}), 'b', '', 2 * 1024 * 1024)
    assert [action.action for action in actions] == ['note'] and 'symbolic link' in actions[0].reason


def test_checked_path_refuses_links_and_escapes(tmp_path):
    import os
    base = tmp_path / 'G' / 'static' / 'artefacts'
    base.mkdir(parents=True)
    assert artefact_sync.checked_path(tmp_path, 'G/static/artefacts/int/figure/x/v1/files/a.png') == \
        base / 'int' / 'figure' / 'x' / 'v1' / 'files' / 'a.png'
    os.symlink(tmp_path, base / 'int')
    with pytest.raises(artefact_sync.UnsafePath):
        artefact_sync.checked_path(tmp_path, 'G/static/artefacts/int/figure/x/v1/files/a.png')
    with pytest.raises(artefact_sync.UnsafePath):
        artefact_sync.checked_path(tmp_path, 'G/static/artefacts/../x')


# ── review round 3, nit 9: previews are checked against the manifest ──────────────────────────────────

def test_previews_are_recorded_in_the_manifest_and_checked_against_it(tmp_path, clock):
    import hashlib
    _service, s3, _publisher = _primary(tmp_path, clock)
    manifest_path = tmp_path / 'store' / 'Public2021/static/artefacts/int/table/big/v1/manifest.json'
    import json
    previews = json.loads(manifest_path.read_text())['previews']
    assert [entry['name'] for entry in previews] == ['big.tsv.txt']
    second = _checkout(tmp_path)
    key = f'{CATALOG}Public2021/static/artefacts/int/table/big/v1/preview/big.tsv.txt'
    forged = b'<script>forged preview</script>'
    s3.objects[key]['body'] = forged
    s3.objects[key]['metadata'] = {'sha256': hashlib.sha256(forged).hexdigest()}     # the object vouches for itself
    actions = [action for action in artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX)
               if action.reason == 'preview' and 'big.tsv.txt' in action.rel]
    assert len(actions) == 1 and actions[0].sha256 == previews[0]['sha256']
    counts = artefact_sync.apply(actions, second, s3, BUCKET, log=lambda message: None)
    assert counts['failed'] == 1 and not (second / actions[0].rel).exists()


def test_a_manifest_without_previews_pulls_none(tmp_path, clock):
    import json
    _service, s3, _publisher = _primary(tmp_path, clock)
    second = _checkout(tmp_path)
    for manifest_path in second.rglob('manifest.json'):
        manifest = json.loads(manifest_path.read_text())
        manifest.pop('previews', None)
        manifest_path.write_text(json.dumps(manifest))        # the head record's sha no longer matches: noted
    actions = artefact_sync.plan(second, s3, BUCKET, CATALOG, INLINE_MAX)
    assert not [action for action in actions if action.reason == 'preview']


def test_an_older_manifest_without_previews_is_noted(tmp_path):
    """Round-4 probe: an older manifest (no previews list) pulled no previews silently; now a note says so."""
    from tests.artefact_helpers import write_artefact, write_index_md
    store = tmp_path / 'store'
    write_index_md(store, 'Public2021', group='Public2021', group_id='PU1')
    write_artefact(store, 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'v.png': b'\x89PNG'}}])
    actions = artefact_sync.plan(store, _ListingS3({}), 'b', '', 2 * 1024 * 1024)
    notes = [action for action in actions if action.action == 'note']
    assert len(notes) == 1 and 'records no previews' in notes[0].reason
    assert not [action for action in actions if '/preview/' in action.rel]


def test_a_symlinked_static_folder_is_refused_and_a_symlinked_group_allowed(tmp_path):
    """Round-4 probe: <group>/static itself was not checked."""
    import os
    store = tmp_path / 'store'
    elsewhere = tmp_path / 'elsewhere'
    (elsewhere / 'artefacts').mkdir(parents=True)
    (store / 'Public2021').mkdir(parents=True)
    os.symlink(elsewhere, store / 'Public2021' / 'static')
    with pytest.raises(artefact_sync.UnsafePath):
        artefact_sync.checked_path(store, 'Public2021/static/artefacts/int/figure/x/v1/files/a.png')
    mirror = tmp_path / 'mirror_group'
    (mirror / 'static' / 'artefacts').mkdir(parents=True)
    os.symlink(mirror, store / 'Mirror2026')
    assert artefact_sync.checked_path(store, 'Mirror2026/static/artefacts/int/figure/x/v1/files/a.png')

