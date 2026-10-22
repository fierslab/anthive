"""The S3 watcher and API deposits: linking by key and ETag, identity from the login, held, approve, withdraw.

The deposit service and the watcher share one fake S3 and one state dir, as
ant-serve and ant-ingest-watch share ``ingest_state_dir`` on a site.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import dataset_deposit as dd
import ingest_watch
from tests.deposit_helpers import ADMIN, ALICE, BOB, DepositFakeS3, file_entry, make_service, upload
from tests.ingest_watch_helpers import INDEX_SEED, PREFIX, make_h5ad, sha_of, sidecar_text
from tests.test_ingest_watch import Env

GROUP = 'WatchTest2026'
SEED_MD = ('---\ntitle: Watch test microglia\nreplicate_unit: donor\n---\n\n' + 'Microglia from a test. ' * 20 + '\n')


class Mailbox:
    def __init__(self):
        self.messages = []

    def factory(self, host, port, timeout=None):
        box = self

        class Smtp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def send_message(self, message):
                box.messages.append(message)
        return Smtp()


@pytest.fixture()
def mailbox(monkeypatch):
    box = Mailbox()
    monkeypatch.setattr(ingest_watch.smtplib, 'SMTP', box.factory)
    return box


def make_env(tmp_path: Path, report=None):
    env = Env(tmp_path, report=report)
    env.s3 = DepositFakeS3(lambda: env.clock_value)
    env.pipeline.s3 = env.s3
    env.watcher.s3 = env.s3
    env.db.close()
    env.db = ingest_watch.StateDB(env.state / dd.WATCH_DB_FILE)        # where ant-serve looks for it
    env.watcher.db = env.db
    env.settings['notify'] = {'on': [], 'smtp_host': 'smtp.test', 'email_from': 'anthive@test'}
    clock = [env.clock_value]
    service = make_service(tmp_path, clock, s3=env.s3)
    env.watcher.deposit_settings = service.settings
    env.pipeline.list_de_studies = lambda path: set()      # the fake duckdb has no DE tables
    return env, service, clock


def deposit_body(tmp_path: Path, basename='micro', seed=0, sidecar_extra=None, update_of=None, index=True):
    local = make_h5ad(tmp_path / 'upload' / f'{basename}{seed}.h5ad', seed=seed)
    h5ad = local.read_bytes()
    sidecar = sidecar_text(sha_of(local), **(sidecar_extra or {})).encode()
    payloads = {f'{basename}.h5ad': h5ad, f'{basename}.h5ad.mus': sidecar, f'{basename}.md': SEED_MD.encode()}
    files = [file_entry(f'{basename}.h5ad', h5ad, 'h5ad'), file_entry(f'{basename}.h5ad.mus', sidecar, 'mus'),
             file_entry(f'{basename}.md', SEED_MD.encode(), 'seed_md')]
    if index:
        payloads['index.md'] = INDEX_SEED.encode()
        files.append(file_entry('index.md', INDEX_SEED.encode(), 'index_md'))
    body = {'data_project': GROUP, 'basename': basename, 'files': files}
    if update_of:
        body['update_of'] = update_of
    return body, payloads


def deposit(env, service, identity=ALICE, complete=True, **kwargs):
    body, payloads = deposit_body(env.tmp, **kwargs)
    response = service.create(identity, body)
    upload(env.s3, response, payloads)
    if complete:
        service.complete(identity, response['deposit_id'])
    return response['deposit_id']


def test_a_completed_deposit_is_published_with_the_login_as_depositor(tmp_path, mailbox):
    env, service, _ = make_env(tmp_path)
    (env.store / '.git').mkdir()
    deposit_id = deposit(env, service, sidecar_extra={'depositor': 'Mallory', 'depositor_email': 'm@evil.example'})
    assert env.poll_twice() == {'live': 1}
    status = service.status(ALICE, deposit_id)
    assert status['state'] == 'live' and status['version'] == '1.0.0' and status['db_id'] == 'WT6/MIC'
    assert status['watch']['incoming_deleted'] is True
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    assert md['ingest']['deposit'] == {'id': deposit_id, 'user': 'alice'}
    assert md['ingest']['depositor'] == 'Alice Smith <alice@example.org>'
    assert any('the API login was recorded instead' in change for change in md['changelog'][-1]['changes'])
    sidecar = (env.store / GROUP / 'micro.h5ad.mus').read_text()
    assert 'depositor=Alice Smith' in sidecar and 'depositor_email=alice@example.org' in sidecar
    assert 'Mallory' not in sidecar and 'evil' not in sidecar
    commit = next(cmd for cmd in env.runner.commands if 'commit' in cmd and '--author' in cmd)
    assert commit[commit.index('--author') + 1] == 'Alice Smith <alice@example.org>'
    record = service.store.get(deposit_id)
    assert record['watch_state'] == 'live' and record['db_id'] == 'WT6/MIC'
    assert dd.report_copy_path(env.state, deposit_id).is_file()
    assert [message['To'] for message in mailbox.messages] == ['alice@example.org']
    assert 'WT6/MIC' in mailbox.messages[0].get_content() and str(tmp_path) not in mailbox.messages[0].get_content()
    env.watcher.poll()
    assert len(mailbox.messages) == 1                                    # one message per outcome


def test_an_open_deposit_holds_back_its_project(tmp_path):
    env, service, _ = make_env(tmp_path)
    deposit_id = deposit(env, service, complete=False)
    assert env.poll_twice() == {}
    assert env.db.get(f'{PREFIX}{GROUP}/micro.h5ad') is None             # not even listed as seen
    service.complete(ALICE, deposit_id)
    assert env.poll_twice() == {'live': 1}


def test_an_expired_open_deposit_is_cleaned_up_by_the_watcher(tmp_path):
    env, service, clock = make_env(tmp_path)
    deposit_id = deposit(env, service, complete=False)
    assert [key for key in env.s3.objects if key.startswith(f'__deposits/{deposit_id}/')]
    assert not [key for key in env.s3.objects if key.startswith(f'{PREFIX}{GROUP}/')]   # uploads never land there
    env.clock_value += 25 * 3600
    clock[0] = env.clock_value
    assert env.poll_twice() == {}
    assert service.store.get(deposit_id)['state'] == 'expired'                         # the watcher expired it
    assert not [key for key in env.s3.objects if key.startswith('__deposits/')]


def test_held_deposit_is_approved_through_the_api(tmp_path, mailbox):
    report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['data_use_unknown'], 'checks': [
        {'id': 'access.controlled', 'status': 'warn', 'summary': 'no data_use', 'fix': 'set data_use='}],
        'admin_detail': {'x': 'secret cohort'}}
    env, service, _ = make_env(tmp_path, report=report)
    deposit_id = deposit(env, service)
    assert env.poll_twice() == {'held': 1}
    status = service.status(ALICE, deposit_id)
    assert status['state'] == 'held' and status['held_reasons'] == ['data_use_unknown']
    assert status['report']['problems'][0]['id'] == 'access.controlled'
    assert 'secret cohort' not in json.dumps(service.report(ALICE, deposit_id))
    assert 'held' in mailbox.messages[0]['Subject']
    with pytest.raises(dd.DepositError) as exc:
        service.approve(ALICE, deposit_id)
    assert exc.value.status == 403
    result = service.approve(ADMIN, deposit_id, 'licence checked')
    assert result['approval']['reasons'] == ['data_use_unknown']
    row = env.db.get(f'{PREFIX}{GROUP}/micro.h5ad')
    approval = json.loads(row['approval'])
    assert row['state'] == 'stable' and approval['channel'] == 'api' and approval['by'] == 'root'
    assert approval['os_user'] is None
    assert env.watcher.poll() == {'live': 1}
    md = (env.store / GROUP / 'micro.md').read_text()
    assert 'Published after review by root' in md
    assert service.status(ALICE, deposit_id)['state'] == 'live'


def test_reject_and_withdraw_of_a_held_deposit(tmp_path):
    report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['licence_unknown'], 'checks': []}
    env, service, _ = make_env(tmp_path, report=report)
    deposit_id = deposit(env, service)
    env.poll_twice()
    with pytest.raises(dd.DepositError) as exc:
        service.withdraw(BOB, deposit_id)
    assert exc.value.status == 404
    rejected = service.reject(ADMIN, deposit_id, 'wrong cohort')
    assert rejected['state'] == 'rejected' and len(rejected['deleted']) == 4      # a reject deletes the upload
    assert service.status(ALICE, deposit_id)['state'] == 'rejected'
    result = service.withdraw(ALICE, deposit_id)
    assert result['state'] == 'withdrawn' and result['deleted'] == []
    assert not [key for key in env.s3.objects if key.startswith(f'{PREFIX}{GROUP}/')]
    assert service.status(ALICE, deposit_id)['state'] == 'withdrawn'


def test_a_live_deposit_cannot_be_withdrawn(tmp_path):
    env, service, _ = make_env(tmp_path)
    deposit_id = deposit(env, service)
    env.poll_twice()
    with pytest.raises(dd.DepositError) as exc:
        service.withdraw(ALICE, deposit_id)
    assert exc.value.status == 409 and exc.value.body['status'] == 'live'


def test_updates_by_deposit_need_update_of_and_the_owner(tmp_path):
    env, service, clock = make_env(tmp_path)
    deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    with pytest.raises(dd.DepositError) as exc:
        deposit(env, service, BOB, seed=1, index=False, update_of='WT6/MIC')
    assert exc.value.body['status'] == 'name_taken'
    second = deposit(env, service, seed=1, index=False, update_of='WT6/MIC')
    env.clock_value += 10
    clock[0] = env.clock_value
    assert env.poll_twice() == {'live': 1}
    assert service.status(ALICE, second)['version'] == '2.0.0'
    # a deposit record without update_of for a dataset that exists by now: a person decides
    third = deposit(env, service, seed=2, index=False, update_of='WT6/MIC')
    service.store.update(third, update_of=None)
    env.clock_value += 10
    assert env.poll_twice() == {'needs_decision': 1}
    assert 'did not declare update_of' in service.status(ALICE, third)['error']


def test_mus_route_deposits_are_unchanged(tmp_path):
    env, service, _ = make_env(tmp_path)
    keys = env.deposit(sidecar_extra={'depositor': 'Ann Lab', 'depositor_email': 'ann@lab.example'})
    assert env.poll_twice() == {'live': 1}
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    assert md['ingest']['depositor'] == 'Ann Lab <ann@lab.example>' and 'deposit' not in md['ingest']
    assert not service.store.deposits()
    assert keys


def test_a_new_deposit_of_identical_files_is_processed_again(tmp_path):
    report = {'result': 'fail', 'publish': 'held', 'held_reasons': [], 'checks': [
        {'id': 'facets.unmapped_values', 'status': 'fail', 'summary': 'ant_sex: 2 values', 'fix': 'map them'}]}
    env, service, clock = make_env(tmp_path, report=report)
    first = deposit(env, service)
    assert env.poll_twice() == {'failed': 1}
    assert service.status(ALICE, first)['report']['problems'][0]['id'] == 'facets.unmapped_values'
    env.runner.report = {'result': 'pass', 'publish': 'auto', 'held_reasons': [], 'checks': []}
    # the same sidecar again (same ETag) with a corrected seed: a continuation deposit
    sidecar = env.s3.objects[f'{PREFIX}{GROUP}/micro.h5ad.mus']['body']
    seed = SEED_MD.replace('Microglia from a test.', 'Microglia from a corrected test.').encode()
    body = {'data_project': GROUP, 'basename': 'micro',
            'files': [file_entry('micro.h5ad.mus', sidecar, 'mus'), file_entry('micro.md', seed, 'seed_md')]}
    response = service.create(ALICE, body)
    assert response['continues'] == first
    upload(env.s3, response, {'micro.h5ad.mus': sidecar, 'micro.md': seed})
    service.complete(ALICE, response['deposit_id'])
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert service.status(ALICE, response['deposit_id'])['state'] == 'live'
    assert service.status(ALICE, first)['state'] == 'failed'            # the first deposit keeps its outcome
    # once live, a third deposit of the same bytes is processed too (as unchanged), not left in incoming
    third = deposit(env, service, update_of='WT6/MIC', index=False)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert service.status(ALICE, third)['kind'] == 'unchanged'


def test_an_update_by_deposit_does_not_adopt_sidecar_depositor_lines(tmp_path):
    env, service, clock = make_env(tmp_path)
    keys = env.deposit()                                                  # published through the mus route
    assert env.poll_twice() == {'live': 1}
    assert keys
    second = deposit(env, service, ADMIN, seed=1, index=False, update_of='WT6/MIC',
                     sidecar_extra={'depositor': 'Mallory', 'depositor_email': 'm@evil.example'})
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert service.status(ADMIN, second)['version'] == '2.0.0'
    sidecar = (env.store / GROUP / 'micro.h5ad.mus').read_text()
    assert 'Mallory' not in sidecar and 'evil' not in sidecar
