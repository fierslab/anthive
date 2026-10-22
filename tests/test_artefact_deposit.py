"""Artefact and collection deposits, server side (lib/artefact_deposit.py, doc/DESIGN_artefacts.md phase A2).

The deposit hardening of 2.46 is reused, and checked here for artefacts: uploads land only in the
deposit's staging folder, complete copies them with their deposit mark into
``<prefix><data_project>/artefacts/<deposit_id>/``, one open deposit per artefact, guarded withdraw.
The fake S3 fails on any call the route must never make (ACLs, policies, batch deletes).
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

import artefact_deposit as rules
import artefacts
import dataset_deposit as dd
from tests.artefact_deposit_helpers import (ADMIN, ANALYST, OTHER, PDF, PNG, PREFIX, STAGING_PREFIX, artefact_body,
                                            context, deposit, file_entry, make_service, upload)
from tests.ingest_watch_helpers import BUCKET


@pytest.fixture
def clock():
    return [time.time()]


def _create(service, body, identity=ANALYST, ctx=None):
    ctx = ctx or context(service.store_root, artefacts.Viewer(sees_internal=True, is_admin=identity.is_admin,
                                                               user=identity.user))
    return service.create(identity, body, ctx)


def _refused(service, body, status, word, identity=ANALYST, ctx=None):
    with pytest.raises(dd.DepositError) as caught:
        _create(service, body, identity, ctx)
    assert (caught.value.status, caught.value.body['status']) == (status, word), caught.value.body
    return caught.value.body


# ── the happy path and the deposit hardening ──────────────────────────────────

def test_create_hands_out_staging_urls_only(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    answer = _create(service, artefact_body({'volcano.png': PNG, 'panels/a.pdf': PDF}))
    assert answer['state'] == 'uploading' and answer['deposit_kind'] == 'artefact'
    assert answer['artefact'] == {'ref_without_version': 'PU1/pub/volcano', 'kind': 'figure', 'new_version_of': None}
    folder = f'{PREFIX}Public2021/artefacts/{answer["deposit_id"]}/'
    for entry in answer['files']:
        assert entry['key'].startswith(folder)
        assert entry['upload_key'].startswith(f'{STAGING_PREFIX}{answer["deposit_id"]}/')
        assert not entry['upload_key'].startswith(PREFIX)
    assert all(key.startswith(STAGING_PREFIX) for _operation, key in s3.presign_log)
    record = service.store.get(answer['deposit_id'])
    assert record['artefact_ref'] == 'PU1/pub/volcano' and record['watch_key'] == f'artefact:{answer["deposit_id"]}'
    block = json.loads(record['artefact'])
    assert block['files'] == [{'name': 'volcano.png', 'role': 'main'}, {'name': 'panels/a.pdf', 'role': 'supporting'}]


def test_complete_copies_with_the_deposit_mark_and_cleans_staging(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG})
    key = f'{PREFIX}Public2021/artefacts/{answer["deposit_id"]}/volcano.png'
    assert s3.objects[key]['metadata'] == {'sha256': hashlib.sha256(PNG).hexdigest(), 'deposit': answer['deposit_id']}
    assert s3.keys(STAGING_PREFIX) == []
    assert service.store.get(answer['deposit_id'])['state'] == 'uploaded'
    assert service.store._rows('SELECT * FROM open_keys') == []
    status = service.status(ANALYST, answer['deposit_id'])
    assert status['deposit_kind'] == 'artefact' and status['artefact']['ref'] == 'PU1/pub/volcano'


def test_a_wrong_upload_is_refused_at_complete(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    answer = _create(service, artefact_body({'volcano.png': PNG}))
    entry = answer['files'][0]
    with pytest.raises(Exception):                                   # the signed size does not match
        s3.client_put(entry['url'], PNG + b'x', entry['headers'])
    with pytest.raises(dd.DepositError) as caught:
        service.complete(ANALYST, answer['deposit_id'])
    assert caught.value.body['status'] == 'incomplete'
    assert s3.keys(PREFIX) == []


def test_multipart_for_large_files(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock, single_put_max_mb=1)
    big = b'%PDF-1.7\n' + b'y' * (65 * 1024 * 1024)
    answer = _create(service, artefact_body({'big.pdf': big}))
    assert answer['files'][0]['method'] == 'multipart' and answer['files'][0]['part_count'] == 2
    upload(s3, answer, {'big.pdf': big})
    assert service.complete(ANALYST, answer['deposit_id'])['state'] == 'uploaded'


def test_one_open_deposit_per_artefact(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    _create(service, artefact_body({'volcano.png': PNG}))
    body = _refused(service, artefact_body({'volcano.png': PNG}), 409, 'busy', identity=OTHER)
    assert 'another deposit' in body['message']
    _create(service, artefact_body({'volcano.png': PNG}, artefact_id='other-plot'))


def test_withdraw_deletes_only_its_own_incoming_copies(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG})
    foreign = f'{PREFIX}Public2021/artefacts/{answer["deposit_id"]}/stray.png'
    s3.put(foreign, PNG, metadata={'deposit': 'dep_' + '0' * 20})
    result = service.withdraw(ANALYST, answer['deposit_id'])
    assert result['state'] == 'withdrawn'
    assert s3.keys(PREFIX) == [foreign]
    assert all(call[0] != 'delete_objects' for call in s3.calls)


def test_another_users_deposit_is_a_404(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG})
    for action in (lambda: service.status(OTHER, answer['deposit_id']),
                   lambda: service.withdraw(OTHER, answer['deposit_id']),
                   lambda: service.complete(OTHER, answer['deposit_id'])):
        with pytest.raises(dd.DepositError) as caught:
            action()
        assert caught.value.status == 404


def test_artefact_files_cannot_be_declared_again(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    answer = _create(service, artefact_body({'volcano.png': PNG}))
    with pytest.raises(dd.DepositError) as caught:
        service.refresh_urls(ANALYST, answer['deposit_id'], {'name': 'volcano.png', 'size': 3, 'sha256': 'a' * 64})
    assert caught.value.status == 422
    fresh = service.refresh_urls(ANALYST, answer['deposit_id'], {'name': 'volcano.png'})
    assert fresh['upload_key'].startswith(STAGING_PREFIX)


def test_rate_limit_applies_before_the_object_store(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock, presign_per_minute=1)
    _create(service, artefact_body({'volcano.png': PNG}))
    calls = len(s3.calls)
    _refused(service, artefact_body({'volcano.png': PNG}, artefact_id='second'), 429, 'rate_limited')
    assert len(s3.calls) == calls


# ── switches ──────────────────────────────────────────────────────────────────

def test_artefacts_switched_off(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock, artefact_settings={'artefacts_enabled': False})
    _refused(service, artefact_body({'volcano.png': PNG}), 503, 'disabled')
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, {'deposit_kind': 'collection', 'data_project': 'Fiers2026',
                                 'collection': {'title': 'Atlas work', 'authors': ['M']}}, None)
    assert caught.value.status == 503


def test_dataset_deposits_still_need_their_own_switch(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)                 # artefacts on, dataset deposits off
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, {'data_project': 'New2026', 'basename': 'x', 'files': []})
    assert caught.value.status == 503
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, {'deposit_kind': 'other'})
    assert caught.value.status == 422


# ── refusals ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('changes, status, word', [
    ({'kind': 'video'}, 422, 'invalid_kind'),
    ({'kind': 'de_table'}, 422, 'invalid_name'),           # a de_table holds DE tables, not a png
    ({'artefact_id': 'Bad_Id'}, 422, 'invalid_artefact_id'),
    ({'artefact_id': 'a'}, 422, 'invalid_artefact_id'),
    ({'artefact_id': 'ok\n'}, 422, 'invalid_artefact_id'),
    ({'title': 'abc'}, 422, 'invalid'),
    ({'title': 'x' * 201}, 422, 'invalid'),
    ({'description': 'short'}, 422, 'invalid'),
    ({'access': 'public'}, 422, 'invalid'),
    ({'made_from': [{'db_id': 'PU1/nope', 'dataset_version': '1.0.0'}]}, 422, 'made_from_unknown'),
    ({'made_from': [{'db_id': 'PU1/pub', 'dataset_version': '9.9.9'}]}, 422, 'made_from_unknown'),
    ({'made_from': [{'db_id': 'PU1/pub', 'dataset_version': '1.0'}]}, 422, 'made_from_unknown'),
    ({'made_from': []}, 422, 'made_from_unknown'),
    ({'made_from': 'PU1/pub'}, 422, 'made_from_unknown'),
    ({'declared': {}}, 422, 'provenance_incomplete'),
    ({'declared': {'author': {'name': 'A'}, 'created': '2026-10-12', 'software': [{'name': 'python', 'version': '3'}],
                   'code': {'repository': 'x', 'commit': 'not-a-commit'}}}, 422, 'provenance_incomplete'),
    ({'declared': {'author': {'name': 'A'}, 'created': '12/10/2026', 'software': [{'name': 'p', 'version': '3'}],
                   'code': {'none': 'r'}}}, 422, 'provenance_incomplete'),
    ({'new_version_of': 1}, 409, 'version_conflict'),
])
def test_artefact_block_refusals(tmp_path, clock, changes, status, word):
    service, _s3 = make_service(tmp_path, clock)
    body = _refused(service, artefact_body({'volcano.png': PNG}, **changes), status, word)
    if word == 'provenance_incomplete':
        assert body['missing']


def test_other_kind_gets_a_provenance_warning_only(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    answer = _create(service, artefact_body({'notes.txt': b'plain notes'}, kind='other', declared={}))
    assert any('provenance' in warning for warning in answer['warnings'])


@pytest.mark.parametrize('name, word', [
    ('../x.png', 'invalid_name'), ('_x.png', 'invalid_name'), ('.x.png', 'invalid_name'), ('a/b/c.png', 'invalid_name'),
    ('café.png', 'invalid_name'), ('x.exe', 'invalid_name'), ('x.h5ad', 'invalid_name'), ('x.PNG.html', 'invalid_name'),
])
def test_file_name_refusals(tmp_path, clock, name, word):
    service, _s3 = make_service(tmp_path, clock)
    _refused(service, artefact_body({name: PNG}), 422, word)


def test_file_list_refusals(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock, artefact_settings={'artefacts_max_files': 2,
                                                                    'artefacts_max_file_gb': 0.000001,
                                                                    'artefacts_max_deposit_gb': 0.0000015})
    _refused(service, artefact_body({'a.png': PNG, 'b.png': PNG, 'c.png': PNG}), 422, 'invalid')
    _refused(service, artefact_body({'a.png': PNG * 20}), 413, 'too_large')
    _refused(service, artefact_body({'a.png': PNG * 4, 'b.png': PNG * 4}), 413, 'too_large')
    _refused(service, artefact_body({'a.png': PNG, 'A.png': PNG}), 422, 'invalid_name')
    body = artefact_body({'a.png': PNG, 'b.png': PNG})
    body['files'] = [dict(entry, role='main') for entry in body['files']]
    _refused(service, body, 422, 'invalid')
    body = artefact_body({'a.png': PNG})
    body['files'][0]['sha256'] = 'XYZ'
    _refused(service, body, 422, 'invalid')
    body['files'][0].update(sha256='a' * 64, size=0)
    _refused(service, body, 422, 'invalid')
    body['files'][0].update(size=True)
    _refused(service, body, 422, 'invalid')
    body = artefact_body({'a.png': PNG})
    body['files'] = []
    _refused(service, body, 422, 'invalid')


def test_scope_and_collection_refusals(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    _refused(service, artefact_body({'a.png': PNG}, data_project='Nope2026'), 422, 'invalid_data_project')
    _refused(service, artefact_body({'a.png': PNG}, data_project='../etc'), 422, 'invalid_data_project')
    _refused(service, artefact_body({'a.png': PNG}, scope='nope'), 422, 'invalid_scope')
    _refused(service, artefact_body({'a.png': PNG}, scope='_other'), 422, 'invalid_scope')
    anonymous_view = context(service.store_root, artefacts.Viewer(sees_internal=False))
    # an internal dataset the caller cannot see answers like one that does not exist
    _refused(service, artefact_body({'a.png': PNG}, scope='int'), 422, 'invalid_scope', ctx=anonymous_view)
    _refused(service, artefact_body({'a.png': PNG}, data_project='Atlas2026', scope='_group'), 422,
             'invalid_data_project', ctx=anonymous_view)
    answer = _create(service, artefact_body({'a.png': PNG}, data_project='Atlas2026', scope='_group'))
    assert answer['artefact']['ref_without_version'] == 'AT6/_group/volcano'


def test_made_from_an_invisible_dataset_answers_like_an_unknown_one(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    view = context(service.store_root, artefacts.Viewer(sees_internal=False))
    unknown = _refused(service, artefact_body({'a.png': PNG}, made_from=[
        {'db_id': 'PU1/pub', 'dataset_version': '1.1.0'}, {'db_id': 'PU1/zzz', 'dataset_version': '1.0.0'}]),
        422, 'made_from_unknown', ctx=view)
    hidden = _refused(service, artefact_body({'a.png': PNG}, made_from=[
        {'db_id': 'PU1/pub', 'dataset_version': '1.1.0'}, {'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]),
        422, 'made_from_unknown', ctx=view)
    assert unknown['message'].replace('zzz', 'X') == hidden['message'].replace('int', 'X')


def test_an_older_dataset_version_is_a_warning(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    answer = _create(service, artefact_body({'a.png': PNG}, made_from=[{'db_id': 'PU1/pub', 'dataset_version': '1.0.0'}]))
    assert any('now 1.1.0' in warning for warning in answer['warnings'])


def test_recorded_fields_of_the_request_are_dropped(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    answer = _create(service, artefact_body({'a.png': PNG}, recorded={'uploaded_by': 'mallory'}, owner='mallory'))
    block = json.loads(service.store.get(answer['deposit_id'])['artefact'])
    assert 'recorded' not in block and 'owner' not in block


def test_the_artefact_block_has_a_size_limit(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    _refused(service, artefact_body({'a.png': PNG}, declared={'parameters': {'x': 'y' * 300000}}), 413, 'too_large')


def test_collection_pending_blocks_artefacts(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    created = service.create(ANALYST, {'deposit_kind': 'collection', 'data_project': 'Fiers2026',
                                       'collection': {'title': 'Microglia atlas work', 'authors': ['Mark Fiers']}},
                             context(service.store_root))
    assert created['state'] == 'uploaded'
    _refused(service, artefact_body({'a.png': PNG}, data_project='Fiers2026', scope='_group'), 409,
             'collection_pending')


# ── versions and ownership against the index ──────────────────────────────────

def _publish_v1(tmp_path, owner='ana'):
    from tests.artefact_helpers import write_artefact
    return write_artefact(tmp_path / 'store', 'Public2021', 'PU1', 'pub', 'figure', 'volcano',
                          [{'files': {'volcano.png': PNG}}], owner=owner)


def test_new_versions_need_new_version_of_owner_and_latest(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    _publish_v1(tmp_path)
    _refused(service, artefact_body({'a.png': PNG}), 409, 'artefact_exists')
    body = _refused(service, artefact_body({'a.png': PNG}, new_version_of=2), 409, 'version_conflict')
    assert body['latest'] == 1
    _refused(service, artefact_body({'a.png': PNG}, new_version_of=1), 403, 'not_owner', identity=OTHER)
    _refused(service, artefact_body({'a.tsv': b'a\tb\n'}, kind='table', new_version_of=1), 422, 'invalid_kind')
    answer = _create(service, artefact_body({'a.png': PNG}, new_version_of=1))
    assert answer['artefact']['new_version_of'] == 1
    assert any('change_note' in warning for warning in answer['warnings'])


def test_admins_may_deposit_versions_of_anyones_artefact(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    _publish_v1(tmp_path)
    answer = _create(service, artefact_body({'a.png': PNG}, new_version_of=1, change_note='axes'), identity=ADMIN)
    assert answer['state'] == 'uploading'


def test_an_invisible_artefact_is_not_found_and_its_id_stays_taken(tmp_path, clock):
    from tests.artefact_helpers import write_artefact
    service, _s3 = make_service(tmp_path, clock)
    write_artefact(tmp_path / 'store', 'Public2021', 'PU1', 'int', 'figure', 'secret-plot',
                   [{'files': {'s.png': PNG}}], owner='otto')
    view = context(service.store_root, artefacts.Viewer(sees_internal=False))
    # the scope itself is invisible to this caller, so the scope check answers first
    _refused(service, artefact_body({'a.png': PNG}, artefact_id='secret-plot', scope='int'), 422, 'invalid_scope',
             ctx=view)


def test_version_and_storage_quotas(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock, artefact_settings={'artefacts_max_versions': 1})
    _publish_v1(tmp_path)
    _refused(service, artefact_body({'a.png': PNG}, new_version_of=1), 413, 'quota_exceeded')
    service, _s3 = make_service(tmp_path / 'b', clock,
                                artefact_settings={'artefacts_max_stored_gb_per_user': 300 / 1024 ** 3})
    from tests.artefact_helpers import write_artefact
    write_artefact(tmp_path / 'b' / 'store', 'Public2021', 'PU1', 'pub', 'figure', 'mine',
                   [{'files': {'m.png': PNG}}], owner='alice')                # 208 bytes uploaded by alice
    alice = dd.Identity(user='alice')
    _refused(service, artefact_body({'a.png': PNG}, artefact_id='second'), 413, 'quota_exceeded', identity=alice)
    _create(service, artefact_body({'a.png': PNG}, artefact_id='second'), identity=ANALYST)


# ── collections ───────────────────────────────────────────────────────────────

def _collection(service, data_project='Fiers2026', identity=ANALYST, **block):
    collection = {'title': 'Microglia atlas analyses', 'authors': ['Mark Fiers'], **block}
    return service.create(identity, {'deposit_kind': 'collection', 'data_project': data_project,
                                     'collection': collection}, context(service.store_root))


def test_create_collection_writes_index_md_with_the_mark(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    answer = _collection(service, description='What this collection holds.')
    assert answer['group_id'] == 'F26' and answer['state'] == 'uploaded'
    key = f'{PREFIX}Fiers2026/artefacts/{answer["deposit_id"]}/index.md'
    assert s3.keys(PREFIX) == [key]
    obj = s3.objects[key]
    assert obj['metadata']['deposit'] == answer['deposit_id']
    assert obj['metadata']['sha256'] == hashlib.sha256(obj['body']).hexdigest()
    meta = artefacts.parse_front_matter(obj['body'].decode())
    assert meta == {'group': 'Fiers2026', 'group_id': 'F26', 'collection_kind': 'analysis',
                    'title': 'Microglia atlas analyses', 'authors': ['Mark Fiers'], 'year': 2026, 'access': 'internal',
                    'created_by': 'ana', 'created': meta['created']}
    assert artefacts.analysis_collection_problems('Fiers2026', meta) == []
    assert answer['held'] == []
    assert s3.presign_log == []


def test_group_ids_are_unique(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    first = _collection(service, 'Fiers2026')
    second = _collection(service, 'Fox2026')
    assert first['group_id'] == 'F26' and second['group_id'] != 'F26' and len(second['group_id']) == 3
    assert rules.assign_group_id('Open2026', {'OP6', 'O26'}) not in ('OP6', 'O26')
    assert rules.assign_group_id('Atlas2026', set()) == 'A26'


@pytest.mark.parametrize('data_project, block, status, word', [
    ('fiers2026', {}, 422, 'invalid_data_project'),
    ('Fiers', {}, 422, 'invalid_data_project'),
    ('../Fiers2026', {}, 422, 'invalid_data_project'),
    ('Fiers2026\n', {}, 422, 'invalid_data_project'),
    ('Public2021', {}, 409, 'collection_exists'),
    ('PUBLIC2021', {}, 409, 'collection_exists'),
    ('Fiers2026', {'title': 'abc'}, 422, 'invalid_collection'),
    ('Fiers2026', {'authors': []}, 422, 'invalid_collection'),
    ('Fiers2026', {'authors': ['A\nB']}, 422, 'invalid_collection'),
    ('Fiers2026', {'access': 'open'}, 422, 'invalid_collection'),
    ('Fiers2026', {'doi': 'not a doi'}, 422, 'invalid_collection'),
    ('Fiers2026', {'papers': {'x y': 'main'}}, 422, 'invalid_collection'),
    ('Fiers2026', {'title': 'Line one\nline: two'}, 422, 'invalid_collection'),
])
def test_collection_refusals(tmp_path, clock, data_project, block, status, word):
    service, _s3 = make_service(tmp_path, clock)
    with pytest.raises(dd.DepositError) as caught:
        _collection(service, data_project, **block)
    assert (caught.value.status, caught.value.body['status']) == (status, word)


def test_collection_name_clash_is_shown_to_admins_only(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    with pytest.raises(dd.DepositError) as caught:
        _collection(service, 'PUBLIC2021')
    assert 'Public2021' not in caught.value.body['message']
    with pytest.raises(dd.DepositError) as caught:
        _collection(service, 'PUBLIC2021', identity=ADMIN)
    assert 'Public2021' in caught.value.body['message']


def test_a_pending_collection_cannot_be_created_twice(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    _collection(service, 'Fiers2026')
    with pytest.raises(dd.DepositError) as caught:
        _collection(service, 'FIERS2026', identity=OTHER)
    assert caught.value.body['status'] == 'collection_exists'


def test_public_collection_is_announced_as_held(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    assert _collection(service, access='public')['held'] == ['public_access']


def test_yaml_injection_in_collection_fields_stays_data(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    answer = _collection(service, description='---\ngroup_id: EVIL\n---\n', paper_title='x: [1, 2]')
    meta = artefacts.parse_front_matter(s3.objects[f'{PREFIX}Fiers2026/artefacts/{answer["deposit_id"]}/index.md']
                                        ['body'].decode())
    assert meta['group_id'] == 'F26' and meta['paper_title'] == 'x: [1, 2]'


# ── purge requests ────────────────────────────────────────────────────────────

def test_purge_request_rules(tmp_path, clock):
    """Two-stage purge (design 5.3): the latest version is refused, a soft purge needs a reason, the same request
    is idempotent, another waiting action is busy, unpurge and delete need the right state and delete needs an
    admin and the typed reference."""
    from tests.artefact_helpers import write_artefact
    service, _s3 = make_service(tmp_path, clock)
    write_artefact(tmp_path / 'store', 'Public2021', 'PU1', 'pub', 'figure', 'volcano',
                   [{'files': {'v.png': PNG}}, {'files': {'v.png': PNG + b'2'}}], owner='otto')
    ctx = context(service.store_root)

    def call(method, identity, body, version='v1'):
        return getattr(service, method)(identity, ctx, 'PU1', 'pub', 'volcano', version, body)
    good = {'reason': 'wrong contrast'}
    with pytest.raises(dd.DepositError) as caught:
        call('request_purge', dd.Identity(user='mallory'), good)
    assert caught.value.status == 403
    for body, word in (({'reason': 'x'}, 'invalid'), (None, 'invalid')):
        with pytest.raises(dd.DepositError) as caught:
            call('request_purge', OTHER, body)
        assert caught.value.body['status'] == word
    with pytest.raises(dd.DepositError) as caught:
        call('request_purge', OTHER, good, version='v9')
    assert caught.value.status == 404
    with pytest.raises(dd.DepositError) as caught:                 # the latest version: never, not even an admin
        call('request_purge', ADMIN, good, version='v2')
    assert caught.value.status == 409 and caught.value.body['status'] == 'latest_version'
    assert call('request_unpurge', ADMIN, {}, version='v2')['changed'] is False       # live already: idempotent
    with pytest.raises(dd.DepositError) as caught:                 # a live version is not deleted
        call('request_delete', ADMIN, {'reason': 'wrong contrast', 'confirm': 'PU1/pub/volcano@v1'})
    assert caught.value.status == 409 and caught.value.body['status'] == 'not_purged'
    answer = call('request_purge', dd.Identity(user='alice'), good)  # alice uploaded v1 (write_artefact records alice)
    assert answer['state'] == 'requested' and answer['action'] == 'soft' and answer['changed'] is True
    again = call('request_purge', ADMIN, good)                       # the same request, still waiting
    assert again['request_id'] == answer['request_id'] and again['state'] == 'requested'
    with pytest.raises(dd.DepositError) as caught:                   # another action while one waits
        call('request_unpurge', ADMIN, {})
    assert caught.value.body['status'] == 'busy'
    rows = service.purges(ADMIN)
    assert rows[0]['ref'] == 'PU1/pub/volcano' and rows[0]['version'] == 1 and rows[0]['state'] == 'requested'
    assert rows[0]['action'] == 'soft' and service.purges(ADMIN, action='hard') == []
    assert service.purges(dd.Identity(user='nobody')) == []


def test_unpurge_and_delete_rules_on_a_purged_version(tmp_path, clock):
    from tests.artefact_helpers import write_artefact
    service, _s3 = make_service(tmp_path, clock)
    write_artefact(tmp_path / 'store', 'Public2021', 'PU1', 'pub', 'figure', 'volcano',
                   [{'files': {'v.png': PNG}, 'state': 'purged', 'purged_by': 'alice'}, {'files': {'v.png': PNG + b'2'}}],
                   owner='otto')
    ctx = context(service.store_root)

    def call(method, identity, body):
        return getattr(service, method)(identity, ctx, 'PU1', 'pub', 'volcano', 'v1', body)
    done = call('request_purge', OTHER, {'reason': 'wrong contrast'})
    assert done == {'ref': 'PU1/pub/volcano', 'version': 1, 'action': 'soft', 'state': 'done', 'changed': False,
                    'version_state': 'purged', 'message': 'PU1/pub/volcano@v1 is already purged'}
    with pytest.raises(dd.DepositError) as caught:                   # only admins delete
        call('request_delete', OTHER, {'reason': 'wrong contrast', 'confirm': 'PU1/pub/volcano@v1'})
    assert caught.value.status == 403
    with pytest.raises(dd.DepositError) as caught:
        call('request_delete', ADMIN, {'reason': 'wrong contrast', 'confirm': 'PU1/pub/volcano@v2'})
    assert caught.value.body['status'] == 'confirm_mismatch'
    assert call('request_unpurge', OTHER, {})['action'] == 'unpurge'      # the owner reverses it
    with pytest.raises(dd.DepositError) as caught:
        call('request_delete', ADMIN, {'reason': 'wrong contrast', 'confirm': 'PU1/pub/volcano@v1'})
    assert caught.value.body['status'] == 'busy'


def test_old_purge_requests_become_hard_purges(tmp_path, clock):
    """The action column is added to a table of the one-stage purge: its rows were hard purges."""
    import sqlite3
    db = tmp_path / 'old.sqlite'
    conn = sqlite3.connect(db)
    conn.executescript(rules.PURGE_SCHEMA)
    conn.execute("INSERT INTO artefact_purges (ref, version, group_folder, scope, kind, artefact_id, requested_by, "
                 "state) VALUES ('PU1/pub/x', 1, 'Public2021', 'pub', 'figure', 'x', 'alice', 'done')")
    rules.ensure_purge_schema(conn)
    rules.ensure_purge_schema(conn)                       # idempotent
    assert conn.execute('SELECT action FROM artefact_purges').fetchall() == [('hard',)]


def test_purge_of_an_invisible_artefact_is_a_404(tmp_path, clock):
    from tests.artefact_helpers import write_artefact
    service, _s3 = make_service(tmp_path, clock)
    write_artefact(tmp_path / 'store', 'Public2021', 'PU1', 'int', 'figure', 'hidden', [{'files': {'v.png': PNG}}])
    ctx = context(service.store_root, artefacts.Viewer(sees_internal=False))
    with pytest.raises(dd.DepositError) as caught:
        service.request_purge(ADMIN, ctx, 'PU1', 'int', 'hidden', 'v1',
                              {'reason': 'wrong contrast', 'confirm': 'PU1/int/hidden@v1'})
    assert caught.value.status == 404


# ── vocabulary ────────────────────────────────────────────────────────────────

def test_vocab_describes_artefacts_and_analysis_collections(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    vocab = service.vocab(ANALYST)
    assert vocab['artefacts']['uploads_enabled'] is True
    assert 'de_table' in vocab['artefacts']['kinds'] and vocab['artefacts']['deferred_kinds'] == []
    spec = vocab['analysis_index_md']['required_spec']
    assert 'default_dataset' in spec['not_required'] and spec['equals']['collection_kind'] == 'analysis'
    assert vocab['index_md']['required_spec']['keys'] == ['group', 'group_id', 'year', 'default_dataset']


# ── shared rules ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name, head, problem', [
    ('a.png', PNG, False), ('a.png', b'GIF89a', True), ('a.pdf', PDF, False), ('a.pdf', b'<html>', True),
    ('a.jpg', b'\xff\xd8\xff\xe0', False), ('a.webp', b'RIFF\x00\x00\x00\x00WEBPVP8 ', False),
    ('a.tif', b'II*\x00', False), ('a.zip', b'PK\x03\x04', False), ('a.zip', b'Rar!', True),
    ('a.tsv.gz', b'\x1f\x8b\x08', False), ('a.tsv.gz', b'a\tb', True), ('a.tsv', b'a\tb\n', False),
    ('a.tsv', b'a\x00b', True), ('a.json', b'{"a": 1}', False), ('a.json', b'{nope', True),
    ('a.ipynb', b'{"cells": [], "nbformat": 4}', False), ('a.ipynb', b'{"a": 1}', True),
    ('a.h5', b'\x89HDF\r\n\x1a\n', False), ('a.txt', b'\xff\xfe', True),
])
def test_magic_bytes(name, head, problem):
    assert (rules.magic_problem(name, head, head[-8:], len(head)) is not None) is problem


def test_parquet_needs_both_ends():
    assert rules.magic_problem('a.parquet', b'PAR1xxxx', b'xxxxPAR1', 100) is None
    assert rules.magic_problem('a.parquet', b'PAR1xxxx', b'xxxxxxxx', 100) is not None


@pytest.mark.parametrize('text, found', [
    (b'-----BEGIN RSA PRIVATE KEY-----\nabc', True), (b'key AKIAABCDEFGHIJKLMNOP here', True),
    (b'glpat-abcdefghijklmnopqrstuvwx', True), (b'password = hunter2xyz', True),
    (b'  "token": "abcdefghij"', True), (b'the password field is empty', False), (b'gene\tlfc\nAPOE\t1.2', False),
])
def test_secret_scan(text, found):
    assert bool(rules.secret_findings(text)) is found


def test_h5ad_sha256_at_versions():
    meta = {'version': '1.2.0', 'sha256': 'c' * 64,
            'changelog': [{'version': '1.0.0'}, {'version': '1.1.0', 'previous_sha256': 'a' * 64},
                          {'version': '1.1.1'}, {'version': '1.2.0', 'previous_sha256': 'b' * 64}]}
    assert rules.h5ad_sha256_at(meta, '1.0.0') == 'a' * 64
    assert rules.h5ad_sha256_at(meta, '1.1.0') == 'b' * 64
    assert rules.h5ad_sha256_at(meta, '1.1.1') == 'b' * 64
    assert rules.h5ad_sha256_at(meta, '1.2.0') == 'c' * 64
    assert rules.h5ad_sha256_at(meta, '0.9.0') is None


def test_incoming_keys_are_built_from_validated_parts():
    assert rules.incoming_folder('__in/', 'Fiers2026', 'dep_' + 'a' * 20) == '__in/Fiers2026/artefacts/dep_' + 'a' * 20 + '/'
    for data_project, deposit_id in (('../x', 'dep_' + 'a' * 20), ('Fiers2026', 'dep_x'), ('_x', 'dep_' + 'a' * 20)):
        with pytest.raises(ValueError):
            rules.incoming_folder('__in/', data_project, deposit_id)
    assert rules.deposit_of_incoming_key('__in/', '__in/F/artefacts/dep_1/a/b.png') == ('F', 'dep_1', 'a/b.png')
    assert rules.deposit_of_incoming_key('__in/', '__in/F/static/a.png') is None


def test_every_validation_regex_is_used_with_fullmatch():
    import inspect
    import re
    for module in (rules, artefacts):
        source = inspect.getsource(module)
        assert not re.search(r'_RE\.match\(', source), module.__name__
        assert not re.search(r'_RE\.search\(', source), module.__name__


@pytest.mark.parametrize('name', ['manifest.json', 'Manifest.JSON'])
def test_manifest_json_is_a_reserved_top_level_name(tmp_path, clock, name):
    service, _s3 = make_service(tmp_path, clock)
    body = _refused(service, artefact_body({name: b'{}'}, kind='other', artefact_id='notes'), 422, 'invalid_name')
    assert 'reserved' in body['message']
    # pyvark's fallback name is refused by the name rule already (no leading underscore)
    _refused(service, artefact_body({'_manifest.json': b'{}'}, kind='other', artefact_id='notes'), 422,
             'invalid_name')
    # in a subfolder the name is fine (clients write the manifest at the top level only)
    _create(service, artefact_body({'meta/manifest.json': b'{}'}, kind='other', artefact_id='notes'), ANALYST, None)


# ── review round 4, nit 7: raw HTML in a collection description is refused, code renders as written ─────

def test_a_collection_description_with_raw_html_is_refused(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    for description in ('Atlas.\n\n<img src=x onerror=alert(1)>', 'a <b>bold</b> word', '<!-- hidden -->'):
        with pytest.raises(dd.DepositError) as caught:
            _collection(service, description=description)
        assert caught.value.status == 422 and 'raw HTML' in caught.value.body['message']


def test_code_and_comparisons_in_a_collection_description_are_kept_as_written(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    description = 'Use `<id>` and `a < b`; p < 0.05.\n\n```\n<script>x</script>\n```\n'
    answer = _collection(service, description=description)
    assert '`<id>`' in answer['index_md'] and '<script>x</script>' in answer['index_md']
    assert '&lt;' not in answer['index_md']
