"""Regression tests for the second review of the deposit API (2026-10-15): N1 to N6."""
from __future__ import annotations

import json
import subprocess
import time

import pytest
import yaml

import config
import dataset_deposit as dd
from tests.deposit_helpers import (ADMIN, ALICE, DepositFakeS3, make_service, sidecar_for, simple_deposit, upload)
from tests.ingest_watch_helpers import PREFIX, S3Error
from tests.test_dataset_deposit_watch import GROUP, deposit_body, make_env


def staged(service, response, payloads):
    upload(service.s3, response, payloads)
    return next(entry for entry in response['files'] if entry['kind'] == 'mus')


def bad_sidecar(good: bytes) -> bytes:
    bad = sidecar_for('0' * 64, data_project='Other2020')
    return (bad + b'#' * len(good))[:len(good)]


# ── N1: a re-PUT during complete ───────────────────────────────────────────

@pytest.mark.parametrize('ignore_conditions', [False, True])
def test_a_reput_between_verify_and_copy_fails_the_complete(tmp_path, ignore_conditions):
    service = make_service(tmp_path, [1_000_000.0])
    service.s3.ignore_conditions = ignore_conditions          # a store that ignores CopySourceIfMatch
    body, payloads = simple_deposit()
    response = service.create(ALICE, body)
    mus = staged(service, response, payloads)
    bad = bad_sidecar(payloads[mus['name']])
    original = service._copy_in

    def racing(deposit_id, row):
        if row['kind'] == 'mus':
            service.s3.client_put(mus['url'], bad, mus['headers'])
        return original(deposit_id, row)
    service._copy_in = racing
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert (exc.value.status, exc.value.body['status']) == (409, 'staging_changed')
    assert not [key for key in service.s3.objects if key.startswith(PREFIX)]
    assert service.store.get(response['deposit_id'])['state'] == 'uploading'
    # the depositor uploads the right sidecar again and completes: now checked and accepted
    service._copy_in = original
    service.s3.client_put(mus['url'], payloads[mus['name']], mus['headers'])
    assert service.complete(ALICE, response['deposit_id'])['state'] == 'uploaded'
    assert service.s3.objects[mus['key']]['body'] == payloads[mus['name']]


@pytest.mark.parametrize('ignore_conditions', [False, True])
def test_a_reput_between_verify_and_the_sidecar_read_fails_the_complete(tmp_path, ignore_conditions):
    service = make_service(tmp_path, [1_000_000.0])
    service.s3.ignore_conditions = ignore_conditions
    body, payloads = simple_deposit()
    response = service.create(ALICE, body)
    mus = staged(service, response, payloads)
    bad = bad_sidecar(payloads[mus['name']])
    original = service._finish_upload

    def racing(deposit_id, row):
        result = original(deposit_id, row)
        if row['kind'] == 'mus':
            service.s3.client_put(mus['url'], bad, mus['headers'])
        return result
    service._finish_upload = racing
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert exc.value.body['status'] == 'staging_changed'
    assert not [key for key in service.s3.objects if key.startswith(PREFIX)]


def test_the_managed_copy_gets_the_condition_too(tmp_path):
    service = make_service(tmp_path, [1_000_000.0])
    row = {'name': 'big.h5ad', 'kind': 'h5ad', 'key': f'{PREFIX}New2026/big.h5ad', 'size': dd.SINGLE_PUT_HARD_MAX + 1,
           'sha256': 'a' * 64, 'staging_key': '__deposits/dep_00000000000000000000/New2026/big.h5ad',
           'staging_etag': 'abc', 'method': 'multipart'}
    service.s3.objects[row['staging_key']] = {'body': b'x', 'etag': 'abc', 'mtime': 0, 'metadata': {}}
    with pytest.raises(RuntimeError):                         # the fake copy is 1 byte: size check fails after
        service._copy_in('dep_00000000000000000000', row)
    assert service.s3.copy_extra_args['CopySourceIfMatch'] == '"abc"'


# ── N2: the staging sweep only touches what this install knows ─────────────

def test_a_second_state_dir_does_not_sweep_the_first_ones_staging(tmp_path):
    clock = [1_000_000.0]
    shared = DepositFakeS3(lambda: clock[0])
    prod = make_service(tmp_path / 'prod', clock, s3=shared)
    body, payloads = simple_deposit()
    response = prod.create(ALICE, body)
    upload(shared, response, payloads)
    other = make_service(tmp_path / 'test', clock, s3=shared)
    other.store.insert({'id': 'dep_00000000000000000000', 'user': 'x', 'data_project': 'X2020', 'basename': 'b',
                        'state': 'expired'}, [])
    other.expire_stale()
    assert len([key for key in shared.objects if key.startswith('__deposits/')]) == 4
    assert prod.complete(ALICE, response['deposit_id'])['state'] == 'uploaded'
    # unknown folders are swept only when every object is older than max_open_days
    shared.put('__deposits/dep_11111111111111111111/X/old.bin', b'old', clock[0] - 8 * 86400)
    shared.put('__deposits/dep_22222222222222222222/X/new.bin', b'new', clock[0] - 86400)
    other.expire_stale()
    assert '__deposits/dep_11111111111111111111/X/old.bin' not in shared.objects
    assert '__deposits/dep_22222222222222222222/X/new.bin' in shared.objects


def test_open_deposits_of_this_install_are_never_swept(tmp_path):
    clock = [1_000_000.0]
    service = make_service(tmp_path, clock)
    body, payloads = simple_deposit()
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    service.sweep_staging()
    assert len([key for key in service.s3.objects if key.startswith('__deposits/')]) == 4


def test_the_staging_prefix_carries_a_site_id(monkeypatch):
    monkeypatch.delenv('ANTHIVE_DATASETS_DEPOSIT_SITE_ID', raising=False)
    settings = config.get_dataset_deposit_settings({'datasets_deposit_site_id': 'Prod Site'})
    assert settings['staging_prefix'] == '__deposits/prod-site/' and settings['site_id'] == 'prod-site'
    default = config.get_dataset_deposit_settings({})
    assert default['staging_prefix'].startswith('__deposits/') and default['staging_prefix'].count('/') == 2
    monkeypatch.setenv('ANTHIVE_DATASETS_DEPOSIT_SITE_ID', 'test-watch')
    assert config.get_dataset_deposit_settings({})['staging_prefix'] == '__deposits/test-watch/'


def test_ant_stack_renders_the_site_name_as_site_id(tmp_path):
    from tests.test_dataset_deposit_stack import S3, _secret
    from tests.test_deploy_config import _load_script, _site
    ant_stack = _load_script('ant_stack_site_id_test', 'bin/ops/ant-stack')
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3=S3)
    ant_stack.ensure_secrets(site)
    for name in ('s3_access_key_id', 's3_secret_access_key'):
        _secret(site, name, 'x')
    stack = ant_stack.render(site, quiet=True)
    api = yaml.safe_load((stack / 'anthive-api.yaml').read_text())
    assert api['datasets_deposit_site_id'] == 'unit'
    assert config.get_dataset_deposit_settings(api)['staging_prefix'] == '__deposits/unit/'


def test_ant_stack_check_warns_about_shared_buckets(tmp_path, capsys):
    from tests.test_dataset_deposit_stack import S3, _secret
    from tests.test_deploy_config import _load_script, _site
    ant_stack = _load_script('ant_stack_share_test', 'bin/ops/ant-stack')
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3=S3)
    for name in ('s3_access_key_id', 's3_secret_access_key'):
        _secret(site, name, 'x')
    ant_stack._check_datasets(site)
    assert 'another site on the same bucket' in capsys.readouterr().err


# ── N3: the end of complete ────────────────────────────────────────────────

def test_a_complete_ended_by_recovery_answers_409_and_keeps_nothing(tmp_path):
    clock = [1_000_000.0]
    service = make_service(tmp_path, clock)
    body, payloads = simple_deposit()
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    original = service._copy_in
    copies = {'n': 0}

    def slow(deposit_id, row):
        etag = original(deposit_id, row)
        copies['n'] += 1
        if copies['n'] == len(response['files']):     # another request judges the complete stale
            service.store.conn.execute('UPDATE deposits SET updated = 0 WHERE id = ?', (deposit_id,))
            clock[0] += 13 * 3600
            assert service.expire_stale() == [deposit_id]
        return etag
    service._copy_in = slow
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert (exc.value.status, exc.value.body['status']) == (409, 'not_completed')
    assert not [key for key in service.s3.objects if key.startswith(PREFIX)]
    assert 'completed' not in [event['action'] for event in service.store.events(response['deposit_id'])]
    assert service.store.get(response['deposit_id'])['state'] == 'expired'


def test_a_long_running_complete_stays_alive(tmp_path):
    clock = [1_000_000.0]
    service = make_service(tmp_path, clock)
    body, payloads = simple_deposit()
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    original = service._copy_in

    def slow(deposit_id, row):
        clock[0] += 8 * 3600                          # each copy takes 8 hours
        service.expire_stale()
        return original(deposit_id, row)
    service._copy_in = slow
    assert service.complete(ALICE, response['deposit_id'])['state'] == 'uploaded'


# ── N4: owner caveats ──────────────────────────────────────────────────────

def test_owner_notes_cannot_inject_front_matter_and_are_versioned_and_committed(tmp_path):
    service = make_service(tmp_path, [1_000_000.0])
    store_root = service.store_root
    git = lambda *args: subprocess.run(['git', '-C', str(store_root), *args], capture_output=True, text=True, check=True)
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 't@example.org')
    git('config', 'user.name', 'Test')
    git('add', '-A')
    git('commit', '-q', '-m', 'init')
    note = "ok\n---\ninjected: true\ncaveats: []\n# '\"{}"
    service.set_owner(ADMIN, 'OL2/mic', 'alice', note)
    assert dd.apply_owner_caveats(service.store, store_root, lambda message: None) == 1
    from metadata import parse_md_file
    meta = parse_md_file(store_root / 'Old2020' / 'micro.md')[0]
    assert 'injected' not in meta and meta['id'] == 'mic'
    assert meta['version'] == '1.0.1' and meta['changelog'][-1]['version'] == '1.0.1'
    assert any('alice' in change for change in meta['changelog'][-1]['changes'])
    assert meta['caveats'][-1]['topic'] == 'dataset_owner' and '\n' not in meta['caveats'][-1]['message']
    assert '---' not in meta['caveats'][-1]['message']
    log = git('log', '--format=%s', '-1').stdout
    assert 'deposit owner of OL2/MIC: alice' in log
    assert [line for line in git('status', '--porcelain').stdout.splitlines() if not line.endswith('.lock')] == []
    assert not list((store_root / 'Old2020').glob('.*owner-partial'))


def test_owner_caveat_leaves_others_staged_changes_alone(tmp_path):
    service = make_service(tmp_path, [1_000_000.0])
    store_root = service.store_root
    git = lambda *args: subprocess.run(['git', '-C', str(store_root), *args], capture_output=True, text=True, check=True)
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 't@example.org')
    git('config', 'user.name', 'Test')
    git('add', '-A')
    git('commit', '-q', '-m', 'init')
    (store_root / 'Old2020' / 'micro.md').write_text((store_root / 'Old2020' / 'micro.md').read_text() + '\nhand edit\n')
    git('add', 'Old2020/micro.md')
    service.set_owner(ADMIN, 'OL2/mic', 'alice')
    messages = []
    dd.apply_owner_caveats(service.store, store_root, messages.append)
    assert git('log', '--format=%s', '-1').stdout.strip() == 'init'
    assert any('staged changes' in message for message in messages)


# ── N5, N6: unlinked items ─────────────────────────────────────────────────

def test_unlinked_items_skip_locked_and_marked_static_files(tmp_path):
    env, service, _ = make_env(tmp_path)
    marked = f'{PREFIX}{GROUP}/static/marked.pdf'
    env.s3.put(marked, b'%PDF marked', env.clock_value, metadata={'deposit': 'dep_99999999999999999999'})
    locked = f'{PREFIX}{GROUP}/static/locked.pdf'
    env.s3.put(locked, b'%PDF locked', env.clock_value)
    service.store.conn.execute("INSERT INTO deposits (id, user, data_project, basename, state, created, expires) "
                               "VALUES ('dep_88888888888888888888', 'bob', ?, 'x', 'completing', 0, 9e12)", (GROUP,))
    service.store.conn.execute("INSERT INTO open_keys (key, deposit_id) VALUES (?, 'dep_88888888888888888888')",
                               (locked,))
    env.deposit(with_seeds=True)
    assert env.poll_twice() == {'live': 1}
    consumed = json.loads(env.db.get(f'{PREFIX}{GROUP}/micro.h5ad')['consumed'])
    assert marked not in consumed and locked not in consumed
    assert not (env.store / GROUP / 'static' / 'marked.pdf').exists()


def test_a_head_error_on_an_unlinked_item_makes_it_wait(tmp_path):
    env, service, _ = make_env(tmp_path)
    keys = env.deposit()
    real_head = env.s3.head_object
    failing = {'on': True}

    def head(Bucket, Key):  # noqa: N803
        if failing['on'] and Key == keys['mus']:
            raise S3Error('InternalError')
        return real_head(Bucket=Bucket, Key=Key)
    env.s3.head_object = head
    assert env.poll_twice() == {}
    assert env.db.get(keys['h5ad']) is None
    failing['on'] = False
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
