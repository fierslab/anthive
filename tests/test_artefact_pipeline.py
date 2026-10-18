"""The watcher side of artefact and collection deposits (lib/artefact_pipeline.py, design sections 5 and 6).

Every test drives the real deposit service (create, presigned upload, complete) and then the publisher on
a fake S3 that fails on calls the route must never make. Crash tests kill the publication between its steps
(an exception that no handler catches, as a SIGKILL would end the process) and check that the next poll
finishes the same version: no second version, no copy repeated, nothing overwritten.
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

import artefact_pipeline
import artefacts
import dataset_deposit as dd
import ingest_watch
from ingest_pipeline import plan_items
from tests.artefact_deposit_helpers import (ADMIN, ANALYST, CATALOG, PDF, PNG, PREFIX, artefact_body, context,
                                            deposit, listing, make_publisher, make_service, poll_until_final,
                                            watch_row)
from tests.artefact_helpers import write_artefact

INTERNAL = {'scope': 'int', 'made_from': [{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]}


class Killed(BaseException):
    """Stands in for SIGKILL: not an Exception, so nothing in the publisher handles it."""


@pytest.fixture
def clock():
    return [time.time()]


def _setup(tmp_path, clock, **settings):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, git, api = make_publisher(tmp_path, s3, clock, settings=settings or None)
    return service, s3, publisher, db, git, api


def _index(tmp_path):
    return context(tmp_path / 'store').index


def test_an_internal_artefact_goes_live(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG, 'panels/b.pdf': PDF}, **INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'live' and row['version'] == 'v1', row['error']
    base = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/'
    assert s3.keys(CATALOG) == sorted([base + 'artefact.md', base + 'v1/manifest.json', base + 'v1/files/volcano.png',
                                       base + 'v1/files/panels/b.pdf'])
    placed = s3.objects[base + 'v1/files/volcano.png']
    assert placed['metadata']['sha256'] == hashlib.sha256(PNG).hexdigest()
    assert placed['metadata']['artefact'] == 'PU1/int/volcano@v1'
    assert s3.content_types[base + 'v1/files/volcano.png'] == 'image/png'
    assert s3.keys(PREFIX) == []                                      # incoming deleted after placement
    entry = _index(tmp_path).get('PU1', 'int', 'volcano')
    assert entry.latest == 1 and entry.owner == 'ana'
    manifest = entry.versions[1].manifest
    assert manifest['recorded']['uploaded_by'] == 'ana' and manifest['recorded']['deposit_id'] == answer['deposit_id']
    assert manifest['made_from'] == [{'db_id': 'PU1/int', 'dataset_version': '1.0.0', 'h5ad_sha256': None}]
    assert [f['name'] for f in manifest['files']] == ['volcano.png', 'panels/b.pdf']
    assert manifest['files'][0]['role'] == 'main'
    # the figure is inline: kept locally too, byte for byte
    local = tmp_path / 'store' / str(entry.path.file_rel(1, 'volcano.png'))
    assert local.read_bytes() == PNG
    status = service.status(ANALYST, answer['deposit_id'])
    assert status['state'] == 'live' and status['artefact']['ref_version'] == 'PU1/int/volcano@v1'
    committed = [cmd for cmd in git.commands if 'commit' in cmd]
    assert len(committed) == 1 and committed[0][-2:] == ['Public2021/static/artefacts/int/figure/volcano/artefact.md',
                                                         'Public2021/static/artefacts/int/figure/volcano/v1/manifest.json']
    assert ('POST', '/admin/rescan?what=artefacts') in api.calls
    assert ('GET', '/artefacts/PU1/int/volcano/v1') in api.calls
    assert not any(call[0] == 'delete' and call[1].startswith(CATALOG) for call in s3.calls)


def test_the_first_listing_never_processes(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    publisher.poll(listing(s3))
    assert watch_row(db, answer['deposit_id'])['state'] == 'seen'
    assert s3.keys(CATALOG) == []


def test_a_public_artefact_is_held_then_approved(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG})
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'held' and json.loads(row['held_reasons']) == ['public_access']
    assert s3.keys(CATALOG) == []
    report = service.report(ANALYST, answer['deposit_id'])
    assert report['held_reasons'] == ['public_access'] and report['audience'] == 'curator'
    service.approve(ADMIN, answer['deposit_id'], 'fine')
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'
    manifest = _index(tmp_path).get('PU1', 'pub', 'volcano').versions[1].manifest
    assert manifest['recorded']['approval']['by'] == 'root'


def test_hold_public_can_be_switched_off(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock, artefacts_hold_public=False)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG})
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'


def test_rejected_deposits_are_cleaned_up(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG})
    poll_until_final(publisher, s3, clock)
    result = service.reject(ADMIN, answer['deposit_id'], 'not this')
    assert result['state'] == 'rejected' and s3.keys(PREFIX) == []
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'rejected' and s3.keys(CATALOG) == []


def test_a_new_version_by_the_owner(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    poll_until_final(publisher, s3, clock)
    second = deposit(service, s3, ANALYST, {'volcano.png': PNG + b'v2'}, new_version_of=1, change_note='relabelled',
                     **INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, second['deposit_id'])['version'] == 'v2'
    entry = _index(tmp_path).get('PU1', 'int', 'volcano')
    assert entry.live_versions == [1, 2] and entry.versions[2].manifest['supersedes'] == 1
    assert entry.versions[2].manifest['change_note'] == 'relabelled'


def test_version_conflict_at_publication(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    # meanwhile another route published the same artefact
    write_artefact(tmp_path / 'store', 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'x.png': PNG}}])
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'artefact_exists' in row['error']
    assert s3.keys(CATALOG) == [] and len(s3.keys(PREFIX)) == 1


def test_not_owner_at_publication(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    write_artefact(tmp_path / 'store', 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'x.png': PNG}}],
                   owner='ana')
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, new_version_of=1, **INTERNAL)
    head = tmp_path / 'store' / 'Public2021/static/artefacts/int/figure/volcano/artefact.md'
    head.write_text(head.read_text().replace('owner: ana', 'owner: otto'))      # ownership moved meanwhile
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'not_owner' in row['error']


@pytest.mark.parametrize('files, check', [
    ({'volcano.png': b'GIF89a not a png at all'}, 'files.type'),
    ({'volcano.pdf': b'<html>pretending</html>'}, 'files.type'),
])
def test_qc_failures(tmp_path, clock, files, check):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, files, **INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed'
    report = service.report(ANALYST, answer['deposit_id'])
    assert any(entry['id'] == check and entry['status'] == 'fail' for entry in report['checks'])
    assert s3.keys(CATALOG) == []


def test_a_declared_sha256_that_is_not_the_bytes_fails(tmp_path, clock):
    """The sha256 the uploader declared (and signed into the URL) is only an attestation: recomputed here."""
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    body = artefact_body({'volcano.png': PNG}, **INTERNAL)
    body['files'][0]['sha256'] = 'f' * 64
    answer = service.create(ANALYST, body, context(service.store_root))
    s3.client_put(answer['files'][0]['url'], PNG, answer['files'][0]['headers'])
    service.complete(ANALYST, answer['deposit_id'])
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'files.sha256' in json.dumps(service.report(ANALYST, answer['deposit_id']))


def test_secrets_and_donor_ids_are_held(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    (tmp_path / 'store' / 'donor_crosswalk.tsv').write_text(
        'cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets\nleuven\tdonor_id\tRM101\tleuven:RM101\tx\t1\n')
    secret = deposit(service, s3, ANALYST, {'notes.txt': b'aws_secret_access_key = abcdefghijklmnop\n'}, kind='other',
                     artefact_id='notes', **INTERNAL)
    donors = deposit(service, s3, ANALYST, {'table.tsv': b'donor\tvalue\nRM101\t1\n'}, kind='table',
                     artefact_id='donor-table', **INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert json.loads(watch_row(db, secret['deposit_id'])['held_reasons']) == ['possible_secret']
    assert json.loads(watch_row(db, donors['deposit_id'])['held_reasons']) == ['donor_identifiers']
    report = json.dumps(service.report(ANALYST, secret['deposit_id']))
    assert 'abcdefghijklmnop' not in report                         # the finding is named, never quoted
    assert s3.keys(CATALOG) == []


def test_zip_entries_that_escape_are_refused(tmp_path, clock):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('../../etc/evil', b'x')
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'model.zip': buffer.getvalue()}, kind='model', artefact_id='model',
                     **INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'failed'
    assert 'escapes the archive' in watch_row(db, answer['deposit_id'])['error']


def test_an_existing_key_with_other_bytes_is_never_overwritten(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    key = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/v1/files/volcano.png'
    s3.put(key, b'someone else', metadata={'sha256': 'e' * 64})
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'needs_decision' and 'never overwritten' in row['error']
    assert s3.objects[key]['body'] == b'someone else'


def test_missing_or_changed_incoming_objects_stop_the_item(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    key = s3.keys(PREFIX)[0]
    s3.put(key, PNG + b'tampered', metadata=s3.objects[key]['metadata'])        # same mark, other bytes
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'needs_decision' and 'missing or changed' in row['error']
    assert s3.keys(CATALOG) == []


def test_unlinked_objects_under_the_artefact_folder_are_refused(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    stray = f'{PREFIX}Public2021/artefacts/dep_{"0" * 20}/evil.png'
    s3.put(stray, PNG, metadata={'deposit': 'dep_' + '0' * 20})
    poll_until_final(publisher, s3, clock)
    row = db.get(f'artefact-unlinked:{stray}')
    assert row['state'] == 'needs_decision'
    assert s3.keys(CATALOG) == [] and stray in s3.keys(PREFIX)        # never processed, never deleted


def test_dataset_items_never_see_artefact_objects():
    objects = [{'Key': f'{PREFIX}Public2021/artefacts/dep_1/cells.h5ad', 'ETag': '"x"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}Public2021/artefacts/dep_1/cells.h5ad.mus', 'ETag': '"y"', 'Size': 1,
                'LastModified': 0}]
    items, malformed = plan_items(objects, PREFIX)
    assert items == [] and malformed == []


def test_a_withdrawn_deposit_is_never_published(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    publisher.poll(listing(s3))
    service.withdraw(ANALYST, answer['deposit_id'])
    poll_until_final(publisher, s3, clock)
    assert s3.keys(CATALOG) == []


def test_the_scope_must_still_be_served(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    (tmp_path / 'store' / 'Public2021' / 'int.inactive').write_text('retired')
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'not served' in row['error']


# ── crash and kill recovery ───────────────────────────────────────────────────

@pytest.mark.parametrize('step', ['after_marker', 'after_first_copy', 'after_manifest', 'after_head'])
def test_a_kill_between_steps_finishes_the_same_version(tmp_path, clock, monkeypatch, step):
    service, s3, publisher, db, git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG, 'panels/b.pdf': PDF}, **INTERNAL)
    real_place, real_put, real_head, real_save = (publisher._place, publisher._put_once, publisher._update_head,
                                                  publisher.save_marker)
    state = {'copies': 0}

    def save_marker(deposit_id, marker):
        real_save(deposit_id, marker)
        if step == 'after_marker' and not marker.get('steps') and not marker.get('manifest'):
            raise Killed()

    def place(*args, **kwargs):
        real_place(*args, **kwargs)
        state['copies'] += 1
        if step == 'after_first_copy' and state['copies'] == 1:
            raise Killed()

    def put_once(*args, **kwargs):
        real_put(*args, **kwargs)
        if step == 'after_manifest':
            raise Killed()

    def update_head(*args, **kwargs):
        real_head(*args, **kwargs)
        if step == 'after_head':
            raise Killed()
    monkeypatch.setattr(publisher, 'save_marker', save_marker)
    monkeypatch.setattr(publisher, '_place', place)
    monkeypatch.setattr(publisher, '_put_once', put_once)
    monkeypatch.setattr(publisher, '_update_head', update_head)
    with pytest.raises(Killed):
        poll_until_final(publisher, s3, clock)
    assert publisher.load_marker(answer['deposit_id'])['version'] == 1
    # the process restarts: a fresh publisher, the same state
    monkeypatch.undo()
    fresh, db2, git2, _api2 = make_publisher(tmp_path, s3, clock)
    poll_until_final(fresh, s3, clock)
    row = watch_row(db2, answer['deposit_id'])
    assert row['state'] == 'live' and row['version'] == 'v1', row['error']
    entry = _index(tmp_path).get('PU1', 'int', 'volcano')
    assert entry.live_versions == [1] and list(entry.versions) == [1]
    catalog_copies = [call for call in s3.calls if call[0] == 'copy_object' and call[2].startswith(CATALOG)]
    assert len(catalog_copies) == 2                                    # each file copied exactly once
    assert fresh.load_marker(answer['deposit_id']) is None
    assert not any(key.startswith(f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/v2') for key in s3.objects)


def test_a_transient_s3_error_is_retried_with_backoff(tmp_path, clock, monkeypatch):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    from tests.ingest_watch_helpers import S3Error
    real = s3.copy_object
    failures = {'left': 1}

    def flaky(**kwargs):
        if failures['left']:
            failures['left'] -= 1
            raise S3Error('SlowDown')
        return real(**kwargs)
    monkeypatch.setattr(s3, 'copy_object', flaky)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'stable' and row['attempts'] == 1 and float(row['next_attempt']) > clock[0]
    clock[0] += 4000
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'


# ── collections ───────────────────────────────────────────────────────────────

def _collection(service, access='internal'):
    return service.create(ANALYST, {'deposit_kind': 'collection', 'data_project': 'Fiers2026',
                                    'collection': {'title': 'Microglia atlas work', 'authors': ['Mark Fiers'],
                                                   'access': access}}, context(service.store_root))


def test_a_collection_is_placed_and_committed(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    answer = _collection(service)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'live', row['error']
    index = tmp_path / 'store' / 'Fiers2026' / 'index.md'
    assert artefacts.parse_front_matter(index.read_text())['group_id'] == 'F26'
    registry, _ = artefacts.scan_collections(tmp_path / 'store', {})
    assert registry['F26'].kind == 'analysis'
    assert any(cmd[-1] == 'Fiers2026/index.md' for cmd in git.commands if 'commit' in cmd)
    assert ('GET', '/groups/F26') in api.calls and s3.keys(PREFIX) == []
    # artefacts may now be deposited into it
    ctx = context(service.store_root)
    created = service.create(ANALYST, artefact_body({'umap.png': PNG}, data_project='Fiers2026', scope='_group',
                                                    artefact_id='umap'), ctx)
    assert created['artefact']['ref_without_version'] == 'F26/_group/umap'


def test_a_public_collection_is_held(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = _collection(service, access='public')
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'held'
    assert not (tmp_path / 'store' / 'Fiers2026').exists()
    service.approve(ADMIN, answer['deposit_id'])
    poll_until_final(publisher, s3, clock)
    assert (tmp_path / 'store' / 'Fiers2026' / 'index.md').is_file()


def test_a_collection_folder_that_appeared_meanwhile_is_needs_decision(tmp_path, clock):
    service, s3, publisher, db, _git, _api = _setup(tmp_path, clock)
    answer = _collection(service)
    (tmp_path / 'store' / 'fiers2026').mkdir()
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'needs_decision'
    assert not (tmp_path / 'store' / 'Fiers2026').exists()


# ── purge ─────────────────────────────────────────────────────────────────────

def _live_two_versions(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, {'volcano.png': PNG, 'thumb.png': PNG + b't'}, **INTERNAL)
    poll_until_final(publisher, s3, clock)
    deposit(service, s3, ANALYST, {'volcano.png': PNG + b'2'}, new_version_of=1, change_note='v2', **INTERNAL)
    poll_until_final(publisher, s3, clock)
    return service, s3, publisher, db, git


def _purge(service, identity, version=1):
    return service.request_purge(identity, context(service.store_root), 'PU1', 'int', 'volcano', f'v{version}',
                                 {'reason': 'wrong contrast', 'confirm': f'PU1/int/volcano@v{version}'})


def test_purge_deletes_exactly_the_listed_keys_and_leaves_a_tombstone(tmp_path, clock):
    service, s3, publisher, db, git = _live_two_versions(tmp_path, clock)
    base = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/'
    stray = base + 'v1/files/not-in-the-manifest.png'
    s3.put(stray, b'x', metadata={'sha256': 'd' * 64})
    _purge(service, ADMIN)
    publisher.poll(listing(s3))
    assert stray in s3.objects                                        # no listing of the prefix decides a delete
    assert not any(key.startswith(base + 'v1/') and key != stray for key in s3.objects)
    assert base + 'v2/manifest.json' in s3.objects and base + 'v2/files/volcano.png' in s3.objects
    assert not (tmp_path / 'store' / 'Public2021/static/artefacts/int/figure/volcano/v1').exists()
    entry = _index(tmp_path).get('PU1', 'int', 'volcano')
    assert entry.versions[1].state == 'purged' and entry.versions[1].purged_by == 'root'
    assert entry.versions[1].purged_by_admin and entry.latest == 2
    tombstone = artefacts.tombstone(entry.versions[1], artefacts.Viewer(sees_internal=True))
    assert tombstone['purged_by'] == 'an admin' and tombstone['purge_reason'] == 'wrong contrast'
    deleted = [call[1] for call in s3.calls if call[0] == 'delete' and call[1].startswith(CATALOG)]
    assert all(key.startswith(base + 'v1/') for key in deleted) and len(deleted) == 3   # 2 files + manifest
    assert all(call[0] != 'delete_objects' for call in s3.calls)
    assert service.purges(ADMIN)[0]['state'] == 'done'
    assert any(row['outcome'] == 'deleted' for row in db.deletions())
    assert any('purge PU1/int/volcano@v1' in ' '.join(cmd) for cmd in git.commands if 'commit' in cmd)


def test_purge_keeps_a_key_whose_sha256_differs(tmp_path, clock):
    service, s3, publisher, db, git = _live_two_versions(tmp_path, clock)
    key = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/v1/files/volcano.png'
    s3.objects[key]['metadata']['sha256'] = 'c' * 64
    _purge(service, ADMIN)
    publisher.poll(listing(s3))
    assert key in s3.objects
    assert service.purges(ADMIN)[0]['state'] == 'needs_decision'
    entry = _index(tmp_path).get('PU1', 'int', 'volcano')
    assert entry.versions[1].state == 'purging'                      # not purged; an admin looks


def test_the_uploader_purges_the_last_version(tmp_path, clock):
    service, s3, publisher, db, _git = _live_two_versions(tmp_path, clock)
    _purge(service, ANALYST, 2)
    publisher.poll(listing(s3))
    _purge(service, ANALYST, 1)
    publisher.poll(listing(s3))
    entry = _index(tmp_path).get('PU1', 'int', 'volcano')
    assert entry.latest is None and entry.versions[2].purged_by == 'ana' and not entry.versions[2].purged_by_admin
    assert not any(key.endswith('/files/volcano.png') for key in s3.objects)


def test_a_purge_after_a_kill_finishes(tmp_path, clock, monkeypatch):
    service, s3, publisher, db, _git = _live_two_versions(tmp_path, clock)
    _purge(service, ADMIN)
    real = publisher._guarded_delete
    calls = {'n': 0}

    def dies(*args, **kwargs):
        calls['n'] += 1
        result = real(*args, **kwargs)
        if calls['n'] == 1:
            raise Killed()
        return result
    monkeypatch.setattr(publisher, '_guarded_delete', dies)
    with pytest.raises(Killed):
        publisher.poll(listing(s3))
    monkeypatch.undo()
    fresh, _db, _git2, _api = make_publisher(tmp_path, s3, clock)
    fresh.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'done'
    assert _index(tmp_path).get('PU1', 'int', 'volcano').versions[1].state == 'purged'


# ── wiring into the watch route ───────────────────────────────────────────────

def test_the_watcher_calls_the_publisher_and_survives_its_failures(tmp_path):
    from tests.test_ingest_watch import Env

    env = Env(tmp_path)
    seen = []

    class Publisher:
        def poll(self, objects):
            seen.append([obj['Key'] for obj in objects])
            raise RuntimeError('boom')
    env.watcher.artefact_publisher = Publisher()
    env.s3.put(f'{env.settings["prefix"]}Fiers2026/artefacts/dep_{"1" * 20}/a.png', PNG)
    counts = env.watcher.poll()                      # the failure is logged, the poll ends normally
    assert seen and seen[0] == [f'{env.settings["prefix"]}Fiers2026/artefacts/dep_{"1" * 20}/a.png']
    assert isinstance(counts, dict)
    assert env.db.items() == []                      # no dataset item from an artefact key
