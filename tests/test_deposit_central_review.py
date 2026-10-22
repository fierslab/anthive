"""Security review round 1 of the central watcher (design section 24): regression tests of the fixed behaviour.

The review probes (an authentic site claiming another site's pending upload, a site admin acting as an admin of
the whole store, parse bombs in record.json and result.json, the owner check on plain artefacts, names and
e-mail addresses in records, a pulled file that is not the signed one, result versions after a lost state
file, the site keys folder, raw HTML in a remote collection) turned into tests of what must happen now.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

import dataset_deposit as dd
import deposit_records as dr
from tests.deposit_helpers import ALICE, upload
from tests.ingest_watch_helpers import PREFIX
from tests.test_dataset_deposit_watch import deposit_body
from tests.test_deposit_central_watcher import SECRET, _deposit, _split, central

ATTACKER_SECRET = 'attacker-secret-' + 'z' * 40
EVIL = 'dep_' + 'e' * 20


def _store(env):
    return dd.DepositStore(env.state / dd.DB_FILE)


def _attacker(env, site_id):
    env.watcher.remote.sites[site_id] = {'k1': ATTACKER_SECRET.encode()}


def _honest_record(env, deposit_id):
    return _split(env.s3.objects[f'__deposits/intern/{deposit_id}/record.json']['body'])[1]['record']


def _forge(env, deposit_id, site_id, mtime, files=True, **changes):
    """A copy of intern's pending record, signed by another (authentic) site for a deposit of its own."""
    honest = _honest_record(env, deposit_id)
    forged = dict(honest, id=EVIL, user='mallory', completed=2 ** 62, **changes)
    forged['files'] = [dict(entry, staging_key=None, staging_etag=None) for entry in honest['files']] if files else []
    raw = dr.sign_record(forged, site_id, EVIL, 1, 'k1', ATTACKER_SECRET)
    env.s3.put(f'__deposits/{site_id}/{EVIL}/record.json', raw, mtime)
    return honest


def _incoming_keys(record):
    return [entry['key'] for entry in record['files']]


def _result(env, site_id, deposit_id):
    raw = env.s3.objects[f'__deposits/{site_id}/{deposit_id}/result.json']['body']
    return dr.verify_result(raw, site_id, deposit_id, [dr.public_key_text(env.watcher.remote.result_key)])['result']


def _listed(env):
    contents = env.s3.get_paginator('list_objects_v2').paginate(Bucket='ingest', Prefix=PREFIX)[0]['Contents']
    return {obj['Key']: (obj['ETag'].strip('"'), obj['Size'], obj['LastModified']) for obj in contents}


# ── S1: another site's pending upload ───────────────────────────────────────

@pytest.mark.parametrize('attacker', ['prod', 'aaa'])          # its records sort after and before intern's
@pytest.mark.parametrize('files', [True, False])                # with the file entries, or only the linking keys
def test_an_authentic_sites_copy_of_a_pending_record_never_takes_the_upload(tmp_path, attacker, files):
    env, service, clock = central(tmp_path)
    _attacker(env, attacker)
    deposit_id = _deposit(env, service)
    _forge(env, deposit_id, attacker, env.clock_value + 1, files=files)
    assert env.poll_twice() == {'live': 1}
    store = _store(env)
    assert store.get(EVIL) is None
    assert store.get(deposit_id)['watch_state'] == 'live'
    assert store.owner_of_dataset('WT6/MIC') == 'alice@intern'
    answer = _result(env, attacker, EVIL)                            # an authentic site gets a signed reason
    assert answer['state'] == 'needs_decision'
    assert 'mark of' in answer['error'] or 'claimed by' in answer['error']


def test_a_site_that_re_marks_the_upload_before_its_owner_is_imported_gets_both_held(tmp_path):
    """The attacker's record is read first (site 'aaa'), after it re-marked intern's objects as its own: the
    owner's record then finds its objects rewritten after it signed them and disputes the attacker's deposit."""
    env, service, clock = central(tmp_path)
    _attacker(env, 'aaa')
    deposit_id = _deposit(env, service)
    honest = _honest_record(env, deposit_id)
    for key in _incoming_keys(honest):
        env.s3.objects[key]['metadata']['deposit'] = EVIL
        env.s3.objects[key]['mtime'] = env.clock_value + 1         # a copy onto itself: a new LastModified
    _forge(env, deposit_id, 'aaa', env.clock_value + 2)
    counts = env.poll_twice()
    assert 'live' not in counts
    store = _store(env)
    assert store.get(EVIL)['disputed'] and store.get(EVIL)['watch_state'] == 'needs_decision'
    assert store.get(deposit_id) is None
    assert store.owner_of_dataset('WT6/MIC') is None
    assert _result(env, 'aaa', EVIL)['state'] == 'needs_decision'
    assert 'rewritten after this record' in service.status(ALICE, deposit_id)['error']


def test_a_site_that_re_marks_the_upload_after_its_owner_was_read_gets_nothing(tmp_path):
    env, service, clock = central(tmp_path)
    _attacker(env, 'prod')
    deposit_id = _deposit(env, service)
    honest = _honest_record(env, deposit_id)
    for key in _incoming_keys(honest):
        env.s3.objects[key]['metadata']['deposit'] = EVIL
        env.s3.objects[key]['mtime'] = env.clock_value + 1
    _forge(env, deposit_id, 'prod', env.clock_value + 2)
    assert 'live' not in env.poll_twice()
    store = _store(env)
    assert store.get(EVIL) is None and store.get(deposit_id) is None
    assert 'claimed by the deposit' in _result(env, 'prod', EVIL)['error']


def test_a_key_claimed_by_a_waiting_deposit_cannot_be_taken_with_other_bytes(tmp_path):
    """Claims are keyed by the incoming key alone: replacing the object (new ETag, own mark) does not free it."""
    env, service, clock = central(tmp_path)
    _attacker(env, 'prod')
    deposit_id = _deposit(env, service)
    store = _store(env)
    assert env.watcher.remote.import_records(store, _listed(env))['imported'] == 1   # intern's, waiting
    honest = _honest_record(env, deposit_id)
    forged = dict(honest, id=EVIL, user='mallory', files=[])
    for entry in honest['files']:
        body = env.s3.objects[entry['key']]['body'] + b'\n'          # other bytes, the attacker's own mark
        etag = env.s3.put(entry['key'], body, env.clock_value + 1, metadata={'deposit': EVIL})
        forged['files'].append(dict(entry, etag=etag, size=len(body), staging_key=None, staging_etag=None))
        if entry['key'] == honest['watch_key']:
            forged['h5ad_etag'] = etag
        if entry['key'] == honest['mus_key']:
            forged['mus_etag'] = etag
    env.s3.put(f'__deposits/prod/{EVIL}/record.json', dr.sign_record(forged, 'prod', EVIL, 1, 'k1', ATTACKER_SECRET),
               env.clock_value + 2)
    env.watcher.remote.import_records(store, _listed(env))
    assert store.get(EVIL) is None
    assert f'claimed by the deposit {deposit_id} of intern' in _result(env, 'prod', EVIL)['error']


def test_continues_must_name_a_deposit_of_the_same_site(tmp_path):
    env, service, clock = central(tmp_path)
    _attacker(env, 'prod')
    deposit_id = _deposit(env, service)
    store = _store(env)
    assert env.watcher.remote.import_records(store, _listed(env))['imported'] == 1   # intern's, not linked yet
    honest = _honest_record(env, deposit_id)
    forged = dict(honest, id=EVIL, user='mallory', continues=deposit_id, files=[])
    env.s3.put(f'__deposits/prod/{EVIL}/record.json', dr.sign_record(forged, 'prod', EVIL, 1, 'k1', ATTACKER_SECRET),
               env.clock_value + 1)
    env.watcher.remote.import_records(store, _listed(env))
    assert store.get(EVIL) is None
    assert f'continues {deposit_id}, which is not a deposit of prod' in _result(env, 'prod', EVIL)['error']


def test_remote_deposits_are_ordered_by_the_watchers_import_time(tmp_path):
    store = dd.DepositStore(tmp_path / dd.DB_FILE)
    base = {'state': 'uploaded', 'watch_key': 'k', 'h5ad_etag': 'e', 'mus_key': 'm', 'mus_etag': 'f',
            'data_project': 'P', 'basename': 'b', 'user': 'u'}
    store.import_remote('dep_' + '1' * 20, {**base, 'id': 'dep_' + '1' * 20, 'completed': 2 ** 62, 'imported': 10.0,
                                            'site_id': 'aaa'}, [])
    store.import_remote('dep_' + '2' * 20, {**base, 'id': 'dep_' + '2' * 20, 'completed': 1, 'imported': 20.0,
                                            'site_id': 'intern'}, [])
    assert store.find_for_item('k', 'e', 'm', 'f')['id'] == 'dep_' + '2' * 20
    store.update('dep_' + '2' * 20, disputed='x')
    assert store.find_for_item('k', 'e', 'm', 'f')['id'] == 'dep_' + '1' * 20


# ── S2: a site's admin flag ─────────────────────────────────────────────────

@pytest.mark.parametrize('trusted', [False, True])
def test_a_site_admin_is_an_admin_of_the_central_store_only_when_trusted(tmp_path, trusted):
    env, service, clock = central(tmp_path)
    if trusted:
        env.watcher.remote.trusted_admin_sites = {'intern'}
    _deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    admin = dd.Identity(user='root', name='Root', email='root@example.org', is_admin=True)
    body, payloads = deposit_body(env.tmp, seed=3, update_of='WT6/MIC', index=False)
    response = service.create(admin, body)
    upload(env.s3, response, payloads)
    service.complete(admin, response['deposit_id'])
    env.clock_value += 10
    counts = env.poll_twice()
    row = _store(env).get(response['deposit_id'])
    if trusted:
        assert 'live' in counts and row['is_admin'] == 1
    else:
        assert row is None
        assert 'does not record root@intern as its owner' in service.status(admin, response['deposit_id'])['error']


# ── C1 / C2: parse bombs ────────────────────────────────────────────────────

def test_a_deeply_nested_record_never_blocks_the_other_imports(tmp_path):
    env, service, clock = central(tmp_path)
    env.s3.put('__deposits/intern/dep_' + '0' * 20 + '/record.json', b'[' * 200000 + b']' * 200000, env.clock_value)
    deposit_id = _deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    assert _store(env).get(deposit_id)['watch_state'] == 'live'


def test_signatures_are_checked_before_any_parsing_and_bodies_have_a_depth_limit():
    started = time.time()
    with pytest.raises(dr.RecordError, match='no signature line'):
        dr.verify_record(b'[' * 500000, 'intern', 'dep_' + 'a' * 20, {'intern': {'k1': SECRET.encode()}})
    nested = {'record': {}, 'deep': json.loads('[' * 40 + ']' * 40)}
    body = json.dumps({'schema': dr.RECORD_SCHEMA, 'site_id': 'intern', 'deposit_id': 'dep_' + 'a' * 20,
                       'version': 1, 'key_id': 'k1', **nested}, sort_keys=True).encode()
    signature = __import__('hmac').new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    with pytest.raises(dr.RecordError, match='nests deeper'):
        dr.verify_record(f'{dr.RECORD_SCHEMA} k1 {signature}\n'.encode() + body, 'intern', 'dep_' + 'a' * 20,
                         {'intern': {'k1': SECRET.encode()}})
    assert time.time() - started < 5


def test_a_deeply_nested_result_leaves_the_sites_routes_working(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    env.s3.put(f'__deposits/intern/{deposit_id}/result.json', b'{"a":' * 100000 + b'1' + b'}' * 100000,
               env.clock_value)
    assert service.status(ALICE, deposit_id)['state'] == 'uploaded'
    assert service.site_sync()['results'] == 0
    assert any(entry['action'] == 'result_refused' for entry in service.store.events(deposit_id))


# ── C3: owner check by kind ─────────────────────────────────────────────────

@pytest.mark.parametrize('kind, needs_owner', [('figure', False), ('de_table', True)])
def test_only_updates_and_de_tables_need_the_datasets_owner(tmp_path, kind, needs_owner):
    env, service, clock = central(tmp_path)
    record = {'deposit_kind': 'artefact', 'update_of': 'PU1/pub', 'user': 'ana', 'is_admin': 0,
              'artefact': json.dumps({'kind': kind, 'scope': 'pub'})}
    problems = env.watcher.remote._rights_problems(_store(env), record, 'intern')
    assert bool(problems) is needs_owner
    dataset = dict(record, deposit_kind='dataset', artefact=None)
    assert env.watcher.remote._rights_problems(_store(env), dataset, 'intern')


# ── C4: no names or e-mail addresses in records ─────────────────────────────

def test_records_carry_logins_but_no_names_or_email_addresses(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    record = _honest_record(env, deposit_id)
    assert record['user'] == 'alice' and 'name' not in record and 'email' not in record
    assert b'alice@example.org' not in env.s3.objects[f'__deposits/intern/{deposit_id}/record.json']['body']
    problems = dr.record_problems(dict(record, email='a@b.c'), 'intern', deposit_id, PREFIX, '__deposits/')
    assert any('unknown record fields' in problem for problem in problems)


# ── C5: the pulled file against the signed sha256 ───────────────────────────

def test_a_pulled_file_that_is_not_the_signed_one_fails(tmp_path):
    """A store that ignores IfMatch could serve other bytes under the listed ETag."""
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    seed_key = next(row['key'] for row in service.store.files(deposit_id) if row['kind'] == 'seed_md')
    original = env.s3.objects[seed_key]['body']
    env.s3.objects[seed_key]['body'] = original.replace(b'---', b'+++', 1)    # same size and listed ETag
    assert env.poll_twice() == {'failed': 1}
    status = service.status(ALICE, deposit_id)
    assert status['state'] == 'failed' and 'signed record' in status['error']


# ── N2: result versions survive a lost state file ───────────────────────────

def test_result_versions_grow_after_the_watchers_state_file_is_lost(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    first = _split(env.s3.objects[f'__deposits/intern/{deposit_id}/result.json']['body'])[1]['version']
    (env.state / 'remote_deposits.json').unlink()
    remote = env.watcher.remote
    remote.clock = lambda: 0.0                                       # even a clock that went back
    state = remote._load()
    assert remote._write_result(state, 'intern', deposit_id, {'state': 'held'}, floor=first)
    second = _split(env.s3.objects[f'__deposits/intern/{deposit_id}/result.json']['body'])[1]['version']
    assert second > first
    service.refresh_result(service.store.get(deposit_id))
    assert service.store.get(deposit_id)['result_version'] == second


# ── N4: the keys folder ─────────────────────────────────────────────────────

def test_the_site_keys_folder_must_be_private_and_hold_regular_files(tmp_path):
    folder = tmp_path / 'sites'
    folder.mkdir(mode=0o700)
    target = tmp_path / 'elsewhere.keys'
    target.write_text(f'k1 {SECRET}\n')
    os.chmod(target, 0o600)
    (folder / 'intern.keys').symlink_to(target)
    with pytest.raises(dr.RecordError, match='symbolic link'):
        dr.load_site_keys(folder)
    (folder / 'intern.keys').unlink()
    (folder / 'intern.keys').write_text(f'k1 {SECRET}\n')
    os.chmod(folder / 'intern.keys', 0o600)
    assert dr.load_site_keys(folder)['intern']['k1'] == SECRET.encode()
    os.chmod(folder, 0o770)
    with pytest.raises(dr.RecordError, match='not be writable by group or others'):
        dr.load_site_keys(folder)
