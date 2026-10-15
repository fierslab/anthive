"""Regression tests for the security review of the deposit API (2026-10-15): the reviewer's probes as tests.

C1 presigned URLs must never write a watched key (staging prefix, copy at complete, watcher defence);
S1 an open deposit holds back only its own items; S2 a deposit's static files are its own;
S3 the watcher fails closed; S4 no dataset named index; and the concerns of the same review.
"""
from __future__ import annotations

import ast
import json
import time

import pytest
import yaml

import dataset_deposit as dd
import ingest_watch
from ingest_pipeline import TransientError, plan_items
from tests.deposit_helpers import (ADMIN, ALICE, BOB, STORE_DB_ID, file_entry, make_service, sha256_bytes,
                                   sidecar_for, simple_deposit, upload)
from tests.ingest_watch_helpers import PREFIX
from tests.test_dataset_deposit_watch import GROUP, deposit, deposit_body, make_env

STAGING = '__deposits/'


def incoming(env):
    return sorted(key for key in env.s3.objects if key.startswith(f'{PREFIX}{GROUP}/'))


def created_upload(env, service, identity=ALICE, **kwargs):
    body, payloads = deposit_body(env.tmp, **kwargs)
    response = service.create(identity, body)
    upload(env.s3, response, payloads)
    return response, payloads


# ── C1: URLs write only the deposit's staging folder ───────────────────────

def test_urls_point_into_the_staging_folder_not_the_watched_prefix(tmp_path):
    env, service, _ = make_env(tmp_path)
    response, _ = created_upload(env, service)
    for entry in response['files']:
        assert entry['upload_key'].startswith(f'{STAGING}{response["deposit_id"]}/')
        assert entry['key'].startswith(PREFIX)
    assert incoming(env) == []                                   # nothing reaches the watch before complete
    assert env.poll_twice() == {}


def test_a_replayed_sidecar_url_after_complete_cannot_change_the_deposit(tmp_path):
    env, service, _ = make_env(tmp_path)
    response, payloads = created_upload(env, service)
    service.complete(ALICE, response['deposit_id'])
    before = {key: env.s3.objects[key]['etag'] for key in incoming(env)}
    mus = next(entry for entry in response['files'] if entry['kind'] == 'mus')
    original = payloads['micro.h5ad.mus']
    forged = (original.decode().replace('source=external', 'source=internal')).encode()
    assert len(forged) == len(original)
    env.s3.client_put(mus['url'], forged, mus['headers'])        # the old URL still works, but only in staging
    assert {key: env.s3.objects[key]['etag'] for key in incoming(env)} == before
    assert env.poll_twice() == {'live': 1}
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    assert md['ingest']['deposit']['id'] == response['deposit_id']
    assert 'source=internal' not in (env.store / GROUP / 'micro.h5ad.mus').read_text()
    assert not [key for key in env.s3.objects if key.startswith(STAGING)]   # the sweep removed the replay


def test_replaying_every_url_after_withdraw_reaches_nothing(tmp_path):
    env, service, _ = make_env(tmp_path)
    response, payloads = created_upload(env, service)
    service.complete(ALICE, response['deposit_id'])
    service.withdraw(ALICE, response['deposit_id'])
    assert incoming(env) == []
    upload(env.s3, response, payloads)
    assert incoming(env) == []
    assert env.poll_twice() == {}
    assert not [key for key in env.s3.objects if key.startswith(STAGING)]


def test_a_url_never_outlives_the_deposit_window(tmp_path):
    env, service, clock = make_env(tmp_path)
    body, payloads = deposit_body(env.tmp)
    response = service.create(ALICE, body)
    record = service.store.get(response['deposit_id'])
    assert all(entry['expires'] <= record['expires'] + 1 for entry in env.s3.presigned.values())
    for _ in range(13):                                          # refresh every 12 h, up to 6.5 days
        env.clock_value += 12 * 3600
        clock[0] = env.clock_value
        refreshed = service.refresh_urls(ALICE, response['deposit_id'], {'name': 'micro.h5ad.mus'})
    record = service.store.get(response['deposit_id'])
    assert record['expires'] <= record['created'] + 7 * 86400
    env.clock_value += 13 * 3600                                 # day 7 + 1 h: past the 7-day cap
    clock[0] = env.clock_value
    service.list(ALICE)
    assert service.store.get(response['deposit_id'])['state'] == 'expired'
    try:
        env.s3.client_put(refreshed['url'], payloads['micro.h5ad.mus'], refreshed['headers'])
    except Exception:  # noqa: BLE001 - expired: refused by S3
        pass
    assert incoming(env) == []
    env.poll_twice()
    assert not [key for key in env.s3.objects if key.startswith(STAGING)]


def test_staging_prefix_must_be_outside_the_watched_prefix():
    for staging, prefix in (('__incoming/x/', '__incoming/'), ('__incoming/', '__incoming/'), ('a/', 'a/b/')):
        with pytest.raises(ValueError):
            dd.DepositSettings(prefix=prefix, staging_prefix=staging)


def test_unlinked_objects_with_a_deposit_mark_are_refused(tmp_path):
    env, service, _ = make_env(tmp_path)
    response, _ = created_upload(env, service)
    service.complete(ALICE, response['deposit_id'])
    mus_key = f'{PREFIX}{GROUP}/micro.h5ad.mus'
    body = env.s3.objects[mus_key]['body'] + b'# tampered\n'
    env.s3.put(mus_key, body, env.clock_value, metadata={'deposit': response['deposit_id'], 'sha256': '0' * 64})
    assert env.poll_twice() == {'needs_decision': 1}
    row = env.db.get(f'{PREFIX}{GROUP}/micro.h5ad')
    assert 'carries the mark of API deposit' in row['error']
    assert not (env.store / GROUP / 'micro.md').exists()


def test_accept_unlinked_false_refuses_the_mus_route(tmp_path):
    env, service, _ = make_env(tmp_path)
    env.settings['accept_unlinked'] = False
    env.deposit()
    assert env.poll_twice() == {'needs_decision': 1}
    assert 'accept_unlinked' in env.db.get(f'{PREFIX}{GROUP}/micro.h5ad')['error']
    deposit(env, service, basename='other', seed=2, index=False)
    env.clock_value += 10
    assert env.poll_twice().get('live') == 1                     # API deposits still go through


def test_accept_unlinked_defaults_to_true_and_is_documented():
    import config
    assert config.get_ingest_watch_settings({})['accept_unlinked'] is True
    assert 'accept_unlinked: true' in (config.get_anthive_root() / 'anthive.default.yaml').read_text()


# ── S1: only the deposit's own items wait ──────────────────────────────────

def test_a_foreign_open_deposit_does_not_freeze_the_project(tmp_path):
    env, service, _ = make_env(tmp_path)
    env.deposit(basename='adminset')                             # a lab member's upload, same group
    body, _ = deposit_body(env.tmp, basename='squat', seed=5, index=False)
    service.create(BOB, body)                                    # never completed
    assert env.poll_twice() == {'live': 1}
    assert env.db.get(f'{PREFIX}{GROUP}/adminset.h5ad')['state'] == 'live'


def test_items_of_a_completing_deposit_wait(tmp_path):
    env, service, _ = make_env(tmp_path)
    response, _ = created_upload(env, service)
    deposit_id = response['deposit_id']
    service.complete(ALICE, deposit_id)
    # simulate the moment between the copies and the end of complete: state completing, keys locked
    service.store.cas(deposit_id, ('uploaded',), 'completing')
    service.store.conn.execute('INSERT INTO open_keys (key, deposit_id) VALUES (?, ?)',
                               (f'{PREFIX}{GROUP}/micro.h5ad', deposit_id))
    assert env.poll_twice() == {}
    assert env.db.get(f'{PREFIX}{GROUP}/micro.h5ad') is None


def test_refreshes_are_capped_and_admins_can_end_a_deposit(tmp_path):
    env, service, _ = make_env(tmp_path)
    service.settings.max_refreshes = 2
    body, _ = deposit_body(env.tmp)
    deposit_id = service.create(ALICE, body)['deposit_id']
    service.refresh_urls(ALICE, deposit_id, {'name': 'micro.md'})
    service.refresh_urls(ALICE, deposit_id, {'name': 'micro.md'})
    with pytest.raises(dd.DepositError) as exc:
        service.refresh_urls(ALICE, deposit_id, {'name': 'micro.md'})
    assert (exc.value.status, exc.value.body['status']) == (429, 'too_many_refreshes')
    with pytest.raises(dd.DepositError) as exc:
        service.force_expire(ALICE, deposit_id)
    assert exc.value.status == 403
    assert service.force_expire(ADMIN, deposit_id)['state'] == 'expired'
    assert service.store.held_keys() == {}


# ── S2: static files belong to their deposit ───────────────────────────────

def test_another_users_static_file_is_not_consumed(tmp_path):
    env, service, _ = make_env(tmp_path)
    body, payloads = deposit_body(env.tmp, basename='zeta', seed=3)
    secret = b'donor,age,apoe\nD1,81,44\n'
    body['files'].append(file_entry('donors.csv', secret, 'static'))
    payloads['donors.csv'] = secret
    bob = service.create(BOB, body)
    upload(env.s3, bob, payloads)
    service.complete(BOB, bob['deposit_id'])
    service.store.cas(bob['deposit_id'], ('uploaded',), 'uploaded')
    body2, payloads2 = deposit_body(env.tmp, basename='alpha', seed=4, index=False)
    alice = service.create(ALICE, body2)
    upload(env.s3, alice, payloads2)
    service.complete(ALICE, alice['deposit_id'])
    env.poll_twice()
    alpha = env.db.get(f'{PREFIX}{GROUP}/alpha.h5ad')
    assert f'{PREFIX}{GROUP}/static/donors.csv' not in json.loads(alpha['consumed'] or '{}')
    assert f'{PREFIX}{GROUP}/static/index.md' not in json.loads(alpha['consumed'] or '{}')
    zeta = env.db.get(f'{PREFIX}{GROUP}/zeta.h5ad')
    if zeta['state'] == 'live':
        assert f'{PREFIX}{GROUP}/static/donors.csv' in json.loads(zeta['consumed'] or '{}')


def test_unlinked_items_do_not_consume_deposit_files(tmp_path):
    env, service, _ = make_env(tmp_path, report={'result': 'pass', 'publish': 'held', 'held_reasons': ['licence_unknown'],
                                                 'checks': []})
    body, payloads = deposit_body(env.tmp, basename='zeta', seed=3)
    body['files'].append(file_entry('paper.pdf', b'%PDF deposit', 'static'))
    payloads['paper.pdf'] = b'%PDF deposit'
    response = service.create(BOB, body)
    upload(env.s3, response, payloads)
    service.complete(BOB, response['deposit_id'])
    env.poll_twice()                                             # held: its files stay in incoming
    env.runner.report = {'result': 'pass', 'publish': 'auto', 'held_reasons': [], 'checks': []}
    env.deposit(basename='labset', seed=6, with_seeds=False)     # a lab member's upload in the same group
    env.clock_value += 10
    env.poll_twice()
    row = env.db.get(f'{PREFIX}{GROUP}/labset.h5ad')
    consumed = json.loads(row['consumed'] or '{}')
    assert not [key for key in consumed if '/static/' in key]


# ── S3: fail closed ────────────────────────────────────────────────────────

def test_an_unreadable_deposit_store_stops_the_poll(tmp_path):
    env, service, _ = make_env(tmp_path)
    env.deposit()
    service.store.close()
    (env.state / dd.DB_FILE).write_bytes(b'this is not a database' * 100)
    for name in ('deposits.sqlite-wal', 'deposits.sqlite-shm'):
        (env.state / name).unlink(missing_ok=True)
    with pytest.raises(TransientError):
        env.watcher.poll()
    assert env.db.get(f'{PREFIX}{GROUP}/micro.h5ad') is None


# ── S4: no dataset called index ────────────────────────────────────────────

@pytest.mark.parametrize('basename', ['index', 'INDEX', 'cells.index', 'x.Index'])
def test_index_basenames_are_refused(tmp_path, basename):
    service = make_service(tmp_path, [time.time()])
    body, _ = simple_deposit(basename=basename)
    for identity in (ALICE, ADMIN):
        with pytest.raises(dd.DepositError) as exc:
            service.create(identity, body)
        assert exc.value.body['status'] == 'invalid_name'
    objects = [{'Key': f'{PREFIX}G2026/{basename}.h5ad', 'ETag': '"1"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G2026/{basename}.h5ad.mus', 'ETag': '"2"', 'Size': 1, 'LastModified': 0}]
    items, malformed = plan_items(objects, PREFIX)
    assert items == [] and any('cannot be a dataset name' in message for message in malformed)


# ── concerns ───────────────────────────────────────────────────────────────

def test_one_open_deposit_per_key_is_enforced_by_the_database(tmp_path):
    service = make_service(tmp_path, [time.time()])
    service.create(ALICE, simple_deposit()[0])
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, simple_deposit(basename='more')[0])
    assert exc.value.body['status'] == 'key_in_open_deposit'
    assert 'more.index.md' in exc.value.body['message']         # the squatted index: what to use instead
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        service.store.conn.execute('INSERT INTO open_keys (key, deposit_id) VALUES (?, ?)',
                                   (f'{PREFIX}New2026/static/index.md', 'dep_' + '0' * 20))


def test_withdraw_and_expiry_wait_while_a_complete_runs(tmp_path):
    clock = [time.time()]
    service = make_service(tmp_path, clock, url_ttl_hours=1)
    response = service.create(ALICE, simple_deposit()[0])
    deposit_id = response['deposit_id']
    assert service.store.cas(deposit_id, ('uploading',), 'completing')
    with pytest.raises(dd.DepositError) as exc:
        service.withdraw(ALICE, deposit_id)
    assert exc.value.body['status'] == 'busy'
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, deposit_id)
    assert exc.value.status == 409
    clock[0] += 2 * 3600                                         # past the window, but completing: not expired
    service.expire_stale()
    assert service.store.get(deposit_id)['state'] == 'completing'
    clock[0] += dd.STALE_COMPLETING_SECONDS                      # an interrupted complete is cleaned up
    service.store.update(deposit_id, updated=clock[0] - dd.STALE_COMPLETING_SECONDS - 1)
    service.expire_stale()
    assert service.store.get(deposit_id)['state'] == 'expired'


def test_a_refused_complete_leaves_nothing_in_incoming(tmp_path):
    service = make_service(tmp_path, [time.time()])
    body, payloads = simple_deposit()
    wrong = sidecar_for('0' * 64)
    payloads['cells.h5ad.mus'] = wrong
    body['files'][1] = file_entry('cells.h5ad.mus', wrong, 'mus')
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    with pytest.raises(dd.DepositError):
        service.complete(ALICE, response['deposit_id'])
    assert not [key for key in service.s3.objects if key.startswith(PREFIX)]
    assert service.store.get(response['deposit_id'])['state'] == 'uploading'


def test_a_correction_is_withdrawn_with_its_original_not_alone(tmp_path):
    service = make_service(tmp_path, [time.time()])
    response, payloads = service.create(ALICE, simple_deposit()[0]), simple_deposit()[1]
    upload(service.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    sidecar = sidecar_for(sha256_bytes(payloads['cells.h5ad']), license='CC0-1.0')
    follow = service.create(ALICE, {'data_project': 'New2026', 'basename': 'cells',
                                    'files': [file_entry('cells.h5ad.mus', sidecar, 'mus')]})
    upload(service.s3, follow, {'cells.h5ad.mus': sidecar})
    service.complete(ALICE, follow['deposit_id'])
    # withdrawing the correction alone would leave the original h5ad without a sidecar
    with pytest.raises(dd.DepositError) as exc:
        service.withdraw(ALICE, follow['deposit_id'])
    assert exc.value.body['status'] == 'continuation' and response['deposit_id'] in exc.value.body['message']
    assert f'{PREFIX}New2026/cells.h5ad.mus' in service.s3.objects
    result = service.withdraw(ALICE, response['deposit_id'])
    assert result['withdrawn_corrections'] == [follow['deposit_id']]
    assert service.store.get(follow['deposit_id'])['state'] == 'withdrawn'
    assert not [key for key in service.s3.objects if key.startswith(f'{PREFIX}New2026/')]


def test_a_continuation_sidecar_must_name_the_waiting_h5ad(tmp_path):
    service = make_service(tmp_path, [time.time()])
    response, payloads = service.create(ALICE, simple_deposit()[0]), simple_deposit()[1]
    upload(service.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    wrong = sidecar_for('1' * 64)
    follow = service.create(ALICE, {'data_project': 'New2026', 'basename': 'cells',
                                    'files': [file_entry('cells.h5ad.mus', wrong, 'mus')]})
    upload(service.s3, follow, {'cells.h5ad.mus': wrong})
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, follow['deposit_id'])
    assert exc.value.body['status'] == 'sidecar_mismatch'


def test_daily_limit_and_rate_limit_before_any_s3_call(tmp_path):
    clock = [time.time()]
    service = make_service(tmp_path, clock, max_per_day=2, max_open_per_user=10)
    service.create(ALICE, simple_deposit(group='Aaa2026')[0])
    service.create(ALICE, simple_deposit(group='Bbb2026')[0])
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, simple_deposit(group='Ccc2026')[0])
    assert (exc.value.status, exc.value.body['status']) == (429, 'too_many_deposits_today')
    limited = make_service(tmp_path / 'r', clock, presign_per_minute=1)
    limited.create(ALICE, simple_deposit(group='Aaa2026')[0])
    calls = len(limited.s3.calls)
    with pytest.raises(dd.DepositError) as exc:
        limited.create(ALICE, simple_deposit(group='Bbb2026')[0])
    assert exc.value.body['status'] == 'rate_limited' and len(limited.s3.calls) == calls


def test_non_group_folders_and_case_clashes_reveal_nothing(tmp_path):
    service = make_service(tmp_path, [time.time()])
    (tmp_path / 'store' / 'Scripts2024').mkdir()                 # a folder without index.md is no group
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, simple_deposit(group='Scripts2024')[0])
    assert exc.value.body['status'] == 'invalid_data_project'
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, simple_deposit(group='OLD2020')[0])
    assert 'Old2020' not in exc.value.body['message']
    with pytest.raises(dd.DepositError) as exc:
        service.create(ADMIN, simple_deposit(group='OLD2020')[0])
    assert 'Old2020' in exc.value.body['message']


def test_a_broken_md_is_not_a_500(tmp_path):
    service = make_service(tmp_path, [time.time()])
    (tmp_path / 'store' / 'Old2020' / 'micro.md').write_text('---\nid: [unclosed\n---\n')
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, simple_deposit(group='Old2020', basename='micro')[0])
    assert exc.value.body['status'] == 'name_taken'


def test_refresh_body_is_validated(tmp_path):
    service = make_service(tmp_path, [time.time()])
    deposit_id = service.create(ALICE, simple_deposit()[0])['deposit_id']
    for body in (None, [], {'name': 3}, {'name': 'cells.h5ad', 'first_part': 'x'}):
        with pytest.raises(dd.DepositError) as exc:
            service.refresh_urls(ALICE, deposit_id, body)
        assert exc.value.status == 422


def test_sanitize_removes_s3_addresses_and_the_bucket():
    roots = {'bucket': 'anthive4a', 'store': '/srv/store'}
    text = dd.sanitize_text('copy of s3://anthive4a/G/x.h5ad failed in bucket anthive4a (anthive4a-old ok)', roots)
    assert 's3://' not in text and ' anthive4a ' not in text and 'anthive4a-old' in text


def test_depositors_see_an_admin_not_the_admins_name(tmp_path):
    report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['licence_unknown'], 'checks': []}
    env, service, _ = make_env(tmp_path, report=report)
    deposit_id = deposit(env, service)
    env.poll_twice()
    service.reject(ADMIN, deposit_id, 'not our data')
    view = json.dumps(service.status(ALICE, deposit_id))
    assert 'root' not in view and 'an admin' in view
    assert 'root' in json.dumps(service.status(ADMIN, deposit_id))


def test_owners_for_datasets_published_before_deposits(tmp_path):
    service = make_service(tmp_path, [time.time()])
    body = simple_deposit(group='Old2020', basename='micro', update_of=STORE_DB_ID)[0]
    with pytest.raises(dd.DepositError):
        service.create(ALICE, body)
    with pytest.raises(dd.DepositError) as exc:
        service.set_owner(ALICE, STORE_DB_ID, 'alice')
    assert exc.value.status == 403
    with pytest.raises(dd.DepositError) as exc:
        service.set_owner(ADMIN, 'NO1/thing', 'alice')
    assert exc.value.status == 404
    service.set_owner(ADMIN, STORE_DB_ID.lower(), 'alice', 'she generated it')
    assert service.create(ALICE, body)['update_of'] == STORE_DB_ID
    with pytest.raises(dd.DepositError) as exc:
        service.create(BOB, simple_deposit(group='Old2020', basename='micro', update_of=STORE_DB_ID)[0])
    assert exc.value.body['status'] == 'name_taken'
    assert dd.apply_owner_caveats(service.store, tmp_path / 'store', lambda message: None) == 1
    md = (tmp_path / 'store' / 'Old2020' / 'micro.md').read_text()
    assert 'accepted from alice' in md and 'topic: dataset_owner' in md
    assert dd.apply_owner_caveats(service.store, tmp_path / 'store', lambda message: None) == 0


def test_ant_ingest_watch_has_the_owner_command():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / 'bin' / 'ingest' / 'ant-ingest-watch').read_text()
    tree = ast.parse(source)
    assert any(getattr(node, 'name', None) == 'cmd_owner' for node in tree.body)
    assert "'owner'" in source and 'apply_owner_caveats' in source


def test_the_sha256_metadata_is_documented_as_an_attestation():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / 'lib' / 'dataset_deposit.py').read_text()
    assert 'attestation only' in source
