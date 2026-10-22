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
from tests.test_deposit_central_watcher import SECRET, _deposit, _split, central, mailbox  # noqa: F401

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


# ── S1 (section 24.9): uploads stay in the site's staging, the watcher copies them in ─────────────────────

def _forge_staged(env, deposit_id, site_id, secret=ATTACKER_SECRET, deposit=EVIL, **changes):
    """intern's pending record, signed by an authentic site for a deposit of its own, naming intern's uploads."""
    honest = _honest_record(env, deposit_id)
    forged = dict(honest, id=deposit, user='mallory', **changes)
    env.s3.put(f'__deposits/{site_id}/{deposit}/record.json', dr.sign_record(forged, site_id, deposit, 1, 'k1', secret),
               env.clock_value + 1)
    return honest


def test_a_central_site_writes_nothing_into_incoming_and_keeps_its_staging(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    assert not [key for key in env.s3.objects if key.startswith(PREFIX)]
    record = _honest_record(env, deposit_id)
    assert all(entry['etag'] is None and entry['staging_etag'] for entry in record['files'])
    assert record['h5ad_etag'] is None and record['mus_etag'] is None
    service.sweep_staging()                                          # not final yet: the watcher copies from it
    assert all(entry['staging_key'] in env.s3.objects for entry in record['files'])
    assert env.poll_twice() == {'live': 1}
    copies = {entry['key']: env.s3.objects.get(entry['key']) for entry in record['files']}
    assert not [key for key, obj in copies.items() if obj is not None]  # consumed after publication
    service.status(ALICE, deposit_id)                                # the site reads the result: live
    service.sweep_staging()
    assert not [entry for entry in record['files'] if entry['staging_key'] in env.s3.objects]


@pytest.mark.parametrize('attacker', ['prod', 'aaa'])          # its records sort after and before intern's
def test_an_authentic_sites_copy_of_a_pending_record_never_takes_the_upload(tmp_path, attacker):
    env, service, clock = central(tmp_path)
    _attacker(env, attacker)
    deposit_id = _deposit(env, service)
    _forge_staged(env, deposit_id, attacker)
    assert env.poll_twice() == {'live': 1}
    store = _store(env)
    assert store.get(EVIL) is None and store.get(deposit_id)['watch_state'] == 'live'
    assert store.owner_of_dataset('WT6/MIC') == 'alice@intern'
    answer = _result(env, attacker, EVIL)                            # an authentic site gets a signed reason
    assert answer['state'] == 'needs_decision' and 'staging key is not under' in answer['error']


def test_a_site_cannot_name_the_staging_of_another_of_its_deposits(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    _forge_staged(env, deposit_id, 'intern', secret=SECRET)
    env.poll_twice()
    assert _store(env).get(EVIL) is None
    assert 'staging key is not under' in _result(env, 'intern', EVIL)['error']


def test_a_staged_upload_swapped_after_signing_is_refused(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    seed = next(row for row in service.store.files(deposit_id) if row['kind'] == 'seed_md')
    metadata = env.s3.objects[seed['staging_key']]['metadata']
    env.s3.put(seed['staging_key'], b'---\ntitle: swapped\n---\n', env.clock_value, metadata=metadata)
    env.poll_twice()
    assert _store(env).get(deposit_id) is None
    assert not [key for key in env.s3.objects if key.startswith(PREFIX)]   # nothing copied
    assert service.status(ALICE, deposit_id)['state'] == 'needs_decision'
    assert 'not the signed one' in service.store.get(deposit_id)['error']


def test_an_object_put_at_an_incoming_key_first_is_never_overwritten(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    h5ad_key = _honest_record(env, deposit_id)['watch_key']
    env.s3.put(h5ad_key, b'someone else', env.clock_value, metadata={'deposit': EVIL})
    env.watcher.remote.import_records(_store(env), {})
    assert _store(env).get(deposit_id) is None and env.s3.objects[h5ad_key]['body'] == b'someone else'
    assert 'another upload waits at this incoming key' in _result(env, 'intern', deposit_id)['error']


def test_an_incoming_key_claimed_by_a_waiting_deposit_is_refused(tmp_path):
    env, service, clock = central(tmp_path)
    first = _deposit(env, service)
    store = _store(env)
    assert env.watcher.remote.import_records(store, {})['imported'] == 1        # copied, waiting
    second = _deposit(env, service)                                  # the same dataset again at the same site
    env.watcher.remote.import_records(store, {})
    assert store.get(second) is None
    assert f'claimed by the deposit {first}' in _result(env, 'intern', second)['error']


def test_a_rewrite_in_incoming_after_the_copy_is_never_published(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    env.watcher.remote.import_records(_store(env), {})
    seed = next(row for row in _store(env).files(deposit_id) if row['kind'] == 'seed_md')
    env.s3.put(seed['key'], b'---\ntitle: rewritten\n---\n', env.clock_value, metadata={'deposit': deposit_id})
    counts = env.poll_twice()
    assert 'live' not in counts
    assert _store(env).get(deposit_id)['watch_state'] != 'live'


def test_a_withdrawn_record_removes_the_watchers_copies(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    env.watcher.remote.import_records(_store(env), {})
    keys = [entry['key'] for entry in _honest_record(env, deposit_id)['files']]
    assert all(key in env.s3.objects for key in keys)
    service.withdraw(ALICE, deposit_id)
    env.watcher.remote.import_records(_store(env), {})
    assert not [key for key in keys if key in env.s3.objects]
    assert _store(env).get(deposit_id)['state'] == 'withdrawn'


def test_continuing_a_deposit_is_refused(tmp_path):
    env, service, clock = central(tmp_path)
    _attacker(env, 'prod')
    deposit_id = _deposit(env, service)
    forged = dict(_honest_record(env, deposit_id), id=EVIL, user='mallory', continues=deposit_id, files=[])
    env.s3.put(f'__deposits/prod/{EVIL}/record.json', dr.sign_record(forged, 'prod', EVIL, 1, 'k1', ATTACKER_SECRET),
               env.clock_value + 1)
    env.watcher.remote.import_records(_store(env), {})
    assert 'continues: a site with a central watcher deposits the h5ad again' in _result(env, 'prod', EVIL)['error']


def test_a_record_of_a_site_that_copied_into_incoming_itself_is_refused():
    record = dr.staged_record_problems({'state': 'uploaded', 'h5ad_etag': 'e' * 32, 'files': [
        {'name': 'a.h5ad', 'state': 'complete', 'etag': 'e' * 32, 'staging_key': None, 'staging_etag': None,
         'sha256': 'a' * 64}]})
    assert any('copied into incoming itself' in problem for problem in record)
    assert any('no staged copy' in problem for problem in record)


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
    env.watcher.poll()                                               # imported, copied into incoming, seen
    seed_key = next(row['key'] for row in service.store.files(deposit_id) if row['kind'] == 'seed_md')
    original = env.s3.objects[seed_key]['body']
    env.s3.objects[seed_key]['body'] = original.replace(b'---', b'+++', 1)    # same size and listed ETag
    assert env.watcher.poll() == {'failed': 1}                       # the second identical listing processes
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


# ── security review round 3 (24.9): regression tests of its probes ───────────────────────────────────────

def test_a_swap_between_the_head_and_the_copy_is_refused_when_the_store_ignores_the_pin(tmp_path, monkeypatch):
    import remote_deposits
    env, service, clock = central(tmp_path)
    env.s3.ignore_conditions = True                    # a store that ignores CopySourceIfMatch
    deposit_id = _deposit(env, service)
    record = _honest_record(env, deposit_id)
    h5ad = next(entry for entry in record['files'] if entry['key'] == record['watch_key'])
    original = remote_deposits.RemoteSites._copy_one

    def swapping(self, entry, key, bucket, source_key, dep, copied):
        if source_key == h5ad['staging_key']:
            obj = env.s3.objects[source_key]
            env.s3.put(source_key, obj['body'][:-1] + bytes([obj['body'][-1] ^ 1]), env.clock_value,
                       metadata=obj['metadata'])          # other bytes, same size and marks
        return original(self, entry, key, bucket, source_key, dep, copied)
    monkeypatch.setattr(remote_deposits.RemoteSites, '_copy_one', swapping)
    env.watcher.poll()
    assert _store(env).get(deposit_id) is None                       # never imported
    assert not [key for key in env.s3.objects if key.startswith(PREFIX)]   # every copy removed
    answer = _result(env, 'intern', deposit_id)
    assert answer['state'] == 'needs_decision' and 'is not the signed upload (ETag' in answer['error']
    env.watcher.poll()                                               # later polls see the swapped staging
    assert _store(env).get(deposit_id) is None
    assert 'not the signed one' in _result(env, 'intern', deposit_id)['error']


def test_a_copy_of_a_multipart_upload_is_checked_by_its_sha256(tmp_path, monkeypatch):
    import remote_deposits
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    store = _store(env)
    record = _honest_record(env, deposit_id)
    h5ad = next(entry for entry in record['files'] if entry['key'] == record['watch_key'])
    copied = {}
    env.s3.ignore_conditions = True                    # the pin on a made-up multipart ETag is not the point here
    entry = dict(h5ad, staging_etag='0' * 32 + '-2', sha256='f' * 64)  # a multipart upload whose bytes differ
    env.s3.objects[h5ad['key']] = dict(env.s3.objects[h5ad['staging_key']])
    with pytest.raises(remote_deposits.CopyMismatch, match='sha256'):
        env.watcher.remote._copy_one(entry, h5ad['key'], 'ingest', h5ad['staging_key'], deposit_id, copied)
    assert h5ad['key'] in copied                                      # recorded, so the caller removes it
    del store


def test_re_marking_the_watchers_copies_never_takes_the_upload_and_answers_the_owner(tmp_path):
    env, service, clock = central(tmp_path)
    _attacker(env, 'prod')
    deposit_id = _deposit(env, service)
    store = _store(env)
    assert env.watcher.remote.import_records(store, {})['imported'] == 1      # copied into incoming, waiting
    honest = _honest_record(env, deposit_id)
    files = []
    env.clock_value += 5
    for entry in honest['files']:
        staged = f'__deposits/prod/{EVIL}/{entry["name"]}'
        body = env.s3.objects[entry['staging_key']]['body']
        etag = env.s3.put(staged, body, env.clock_value, metadata={'deposit': EVIL, 'sha256': entry['sha256']})
        env.s3.put(entry['key'], env.s3.objects[entry['key']]['body'], env.clock_value,
                   metadata={'deposit': EVIL, 'sha256': entry['sha256']})            # re-mark the watcher's copy
        env.s3.objects[entry['key']]['etag'] = hashlib.md5(entry['key'].encode()).hexdigest() + '-1'
        files.append(dict(entry, staging_key=staged, staging_etag=etag))
    forged = dict(honest, id=EVIL, user='mallory', files=files)
    env.s3.put(f'__deposits/prod/{EVIL}/record.json', dr.sign_record(forged, 'prod', EVIL, 1, 'k1', ATTACKER_SECRET),
               env.clock_value + 1)
    env.poll_twice()
    env.poll_twice()
    assert (store.get(EVIL) or {}).get('watch_state') != 'live'
    assert store.owner_of_dataset('WT6/MIC') != 'mallory@prod'
    assert store.get(deposit_id)['watch_state'] == 'needs_decision'          # not 'uploaded' forever
    answer = _result(env, 'intern', deposit_id)
    assert answer['state'] == 'needs_decision' and 're-marked in incoming' in answer['error']
    status = service.status(ALICE, deposit_id)
    assert status['state'] == 'needs_decision'


def test_record_mtime_is_gone():
    import inspect
    import remote_deposits
    assert 'record_mtime' not in inspect.getsource(remote_deposits)


def test_staged_uploads_of_a_needs_decision_deposit_expire_with_a_final_result(tmp_path, mailbox):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    seed = next(row for row in service.store.files(deposit_id) if row['kind'] == 'seed_md')
    env.s3.put(seed['staging_key'], b'---\ntitle: swapped\n---\n', env.clock_value,
               metadata=env.s3.objects[seed['staging_key']]['metadata'])
    env.poll_twice()
    assert service.status(ALICE, deposit_id)['state'] == 'needs_decision'
    staged = [row['staging_key'] for row in service.store.files(deposit_id)]
    clock[0] += 13 * 86400
    assert service.expire_decided() == []                              # 2 x max_open_days (7) not reached
    assert all(key in env.s3.objects for key in staged)
    clock[0] += 2 * 86400
    assert service.expire_decided() == [deposit_id]
    assert not [key for key in staged if key in env.s3.objects]       # the staged uploads are gone
    assert service.store.get(deposit_id)['state'] == 'expired'
    assert [message['To'] for message in mailbox.messages] == ['alice@example.org']
    assert 'expired' in mailbox.messages[0]['Subject']
    env.watcher.remote.import_records(_store(env), {})                # the expired record reaches the watcher
    answer = _result(env, 'intern', deposit_id)
    assert answer['state'] == 'expired' and 'removed its incoming copies' in answer['error']
