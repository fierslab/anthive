"""One central watcher for several sites (design section 24): signed records out, signed results back.

Threat model tested here (everything in the ingestion bucket is attacker-reachable by anyone holding its keys):
forged records (unknown site, wrong secret, tampered content, a record of site A under site B's prefix, a
mismatching deposit id), replay of an older version and rewrites of an imported version, files swapped after
signing, path traversal in names and keys, oversized records, an update of another site's dataset, forged or
foreign results, and secrets that must never appear in errors. End to end: a deposit accepted by site "intern"
is imported, published and answered by the watcher; the site shows the result and sends the e-mail; the keys
go to the right bucket (ingestion bucket for staging, records and incoming; data bucket for the catalog).
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

import dataset_deposit as dd
import deposit_records as dr
import ingest_watch
import remote_deposits
from tests.deposit_helpers import ALICE, DepositFakeS3, upload
from tests.ingest_watch_helpers import PREFIX
from tests.test_dataset_deposit_watch import GROUP, Mailbox, deposit_body
from tests.test_ingest_watch import Env

SECRET = 'intern-secret-' + 'x' * 40
OTHER_SECRET = 'other-secret-' + 'y' * 40
SITES = {'intern': {'k1': SECRET.encode()}, 'prod': {'k1': OTHER_SECRET.encode()}}
DEP = 'dep_' + 'a' * 20


def _record(**changes):
    record = {field: None for field in dr.RECORD_FIELDS}
    record.update(id=DEP, user='alice', name='Alice', email='alice@example.org', is_admin=0, data_project=GROUP,
                  basename='micro', state='uploaded', created=1, updated=2, total_bytes=10,
                  watch_key=f'{PREFIX}{GROUP}/micro.h5ad', h5ad_etag='0' * 32, mus_key=f'{PREFIX}{GROUP}/micro.h5ad.mus',
                  mus_etag='1' * 32, deposit_kind='dataset',
                  files=[{'name': 'micro.h5ad', 'kind': 'h5ad', 'key': f'{PREFIX}{GROUP}/micro.h5ad', 'size': 10,
                          'sha256': 'a' * 64, 'method': 'put', 'state': 'complete', 'etag': '0' * 32,
                          'staging_key': f'__deposits/intern/{DEP}/{GROUP}/micro.h5ad', 'staging_etag': '0' * 32,
                          'completed': 2}])
    record.update(changes)
    return record


# ── signatures ───────────────────────────────────────────────────────────────

def test_a_signed_record_verifies_and_the_canonical_form_has_no_floats():
    raw = dr.sign_record(_record(), 'intern', DEP, 1, 'k1', SECRET)
    envelope = dr.verify_record(raw, 'intern', DEP, SITES)
    assert envelope['record']['user'] == 'alice' and envelope['version'] == 1
    with pytest.raises(dr.RecordError):
        dr.canonical({'size': 1.5})


@pytest.mark.parametrize('case', ['wrong_secret', 'unknown_site', 'other_prefix', 'deposit_id', 'tampered',
                                  'unknown_key', 'not_json', 'oversized', 'float'])
def test_forged_records_are_refused(case):
    raw = dr.sign_record(_record(), 'intern', DEP, 1, 'k1', SECRET)
    site, deposit_id, sites = 'intern', DEP, SITES
    if case == 'wrong_secret':
        raw = dr.sign_record(_record(), 'intern', DEP, 1, 'k1', OTHER_SECRET)
    elif case == 'unknown_site':
        site, sites = 'evil', SITES
        raw = dr.sign_record(_record(), 'evil', DEP, 1, 'k1', 'e' * 40)
    elif case == 'other_prefix':                # intern's record dropped under prod's folder
        site = 'prod'
    elif case == 'deposit_id':
        deposit_id = 'dep_' + 'b' * 20
    elif case == 'tampered':
        envelope = json.loads(raw)
        envelope['record']['is_admin'] = 1
        raw = json.dumps(envelope).encode()
    elif case == 'unknown_key':
        raw = dr.sign_record(_record(), 'intern', DEP, 1, 'k9', SECRET)
    elif case == 'not_json':
        raw = b'\xff not json'
    elif case == 'oversized':
        raw = b'{' + b' ' * (dr.MAX_RECORD_BYTES + 1) + b'}'
    elif case == 'float':
        envelope = json.loads(raw)
        envelope['version'] = 1.0
        raw = json.dumps(envelope).encode()
    with pytest.raises(dr.RecordError) as exc:
        dr.verify_record(raw, site, deposit_id, sites)
    assert SECRET not in str(exc.value) and OTHER_SECRET not in str(exc.value)


def test_a_rotated_secret_accepts_both_key_ids():
    sites = {'intern': {'k1': SECRET.encode(), 'k2': ('n' * 40).encode()}}
    assert dr.verify_record(dr.sign_record(_record(), 'intern', DEP, 1, 'k2', 'n' * 40), 'intern', DEP, sites)
    assert dr.verify_record(dr.sign_record(_record(), 'intern', DEP, 2, 'k1', SECRET), 'intern', DEP, sites)


def test_site_secret_files_must_be_private_and_long(tmp_path):
    folder = tmp_path / 'sites'
    folder.mkdir()
    path = folder / 'intern.keys'
    path.write_text(f'k1 {SECRET}\nk2 {"n" * 40}\n')
    os.chmod(path, 0o600)
    assert set(dr.load_site_keys(folder)['intern']) == {'k1', 'k2'}
    os.chmod(path, 0o644)
    with pytest.raises(dr.RecordError) as exc:
        dr.load_site_keys(folder)
    assert 'chmod 600' in str(exc.value) and SECRET not in str(exc.value)
    os.chmod(path, 0o600)
    path.write_text('k1 short\n')
    with pytest.raises(dr.RecordError) as exc:
        dr.load_site_keys(folder)
    assert 'short' not in str(exc.value).split(':', 1)[1] or 'at least 32' in str(exc.value)
    (folder / 'Bad Site.keys').write_text('x')
    os.chmod(folder / 'Bad Site.keys', 0o600)
    path.unlink()
    with pytest.raises(dr.RecordError):
        dr.load_site_keys(folder)


# ── content rules: traversal, prefixes, identity ─────────────────────────────

@pytest.mark.parametrize('change, expected', [
    ({'user': 'alice@prod'}, 'plain login'),
    ({'data_project': '../Other'}, 'data_project'),
    ({'watch_key': f'{PREFIX}Other2026/micro.h5ad'}, 'watch_key'),
    ({'update_of': '../x'}, 'update_of'),
    ({'deposit_kind': 'root'}, 'deposit_kind'),
    ({'id': 'dep_' + 'c' * 20}, 'record id'),
    ({'extra': 1}, 'unknown record fields'),
])
def test_record_content_rules(change, expected):
    problems = dr.record_problems(_record(**change), 'intern', DEP, PREFIX, '__deposits/')
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize('name, key', [
    ('../../etc/passwd', f'{PREFIX}{GROUP}/x'),
    ('static/../x', f'{PREFIX}{GROUP}/x'),
    ('ok.md', f'{PREFIX}{GROUP}/../Other2026/x'),
    ('ok.md', f'{PREFIX}Other2026/ok.md'),
    ('ok.md', '__catalog/x'),
])
def test_file_names_and_keys_cannot_escape(name, key):
    record = _record(files=[{'name': name, 'kind': 'static', 'key': key, 'size': 1, 'state': 'complete'}])
    assert dr.record_problems(record, 'intern', DEP, PREFIX, '__deposits/')


def test_staging_keys_stay_in_the_sites_own_deposit_folder():
    entry = dict(_record()['files'][0], staging_key=f'__deposits/prod/{DEP}/{GROUP}/micro.h5ad')
    problems = dr.record_problems(_record(files=[entry]), 'intern', DEP, PREFIX, '__deposits/')
    assert any('staging key' in problem for problem in problems)


def test_files_swapped_after_signing_are_seen():
    record = _record()
    listed = {f'{PREFIX}{GROUP}/micro.h5ad': ('f' * 32, 10)}
    assert dr.listing_mismatches(record, listed)
    assert dr.listing_mismatches(record, {f'{PREFIX}{GROUP}/micro.h5ad': ('0' * 32, 11)})
    assert not dr.listing_mismatches(record, {f'{PREFIX}{GROUP}/micro.h5ad': ('0' * 32, 10)})


def test_import_refuses_replays_rewrites_and_foreign_deposit_ids(tmp_path):
    store = dd.DepositStore(tmp_path / 'deposits.sqlite')
    v2 = dr.verify_record(dr.sign_record(_record(), 'intern', DEP, 2, 'k1', SECRET), 'intern', DEP, SITES)
    assert dr.import_record(store, v2, 'intern') == 'imported'
    assert store.get(DEP)['user'] == 'alice@intern' and store.get(DEP)['site_id'] == 'intern'
    assert dr.import_record(store, v2, 'intern') == 'unchanged'
    v1 = dr.verify_record(dr.sign_record(_record(), 'intern', DEP, 1, 'k1', SECRET), 'intern', DEP, SITES)
    with pytest.raises(dr.RecordError, match='replay'):
        dr.import_record(store, v1, 'intern')
    other = dr.verify_record(dr.sign_record(_record(name='Changed'), 'intern', DEP, 2, 'k1', SECRET), 'intern', DEP,
                             SITES)
    with pytest.raises(dr.RecordError, match='other content'):
        dr.import_record(store, other, 'intern')
    stolen = dr.verify_record(dr.sign_record(_record(), 'prod', DEP, 9, 'k1', OTHER_SECRET), 'prod', DEP, SITES)
    with pytest.raises(dr.RecordError, match='another site'):
        dr.import_record(store, stolen, 'prod')


# ── results ──────────────────────────────────────────────────────────────────

def test_results_are_signed_by_the_watcher_and_checked_by_the_site(tmp_path):
    key = dr.load_or_create_result_key(tmp_path / 'secrets' / 'result.pem')
    assert oct((tmp_path / 'secrets' / 'result.pem').stat().st_mode & 0o777) == '0o600'
    public = dr.public_key_text(key)
    raw = dr.sign_result({'state': 'live'}, 'intern', DEP, 1, key)
    assert dr.verify_result(raw, 'intern', DEP, [public])['result'] == {'state': 'live'}
    other = dr.load_or_create_result_key(tmp_path / 'other.pem')
    with pytest.raises(dr.RecordError, match='does not trust'):
        dr.verify_result(dr.sign_result({'state': 'live'}, 'intern', DEP, 1, other), 'intern', DEP, [public])
    tampered = json.loads(raw)
    tampered['result']['state'] = 'held'
    with pytest.raises(dr.RecordError, match='bad signature'):
        dr.verify_result(json.dumps(tampered).encode(), 'intern', DEP, [public])
    with pytest.raises(dr.RecordError, match='another site'):
        dr.verify_result(raw, 'prod', DEP, [public])


# ── end to end ───────────────────────────────────────────────────────────────

class BucketCheckS3(DepositFakeS3):
    """Asserts the bucket of every call: staging, records and incoming in "ingest", everything else in "data"."""

    def __init__(self, clock):
        super().__init__(clock)
        self.wrong = []

    def _check(self, bucket, key):
        expected = 'ingest' if str(key).startswith((PREFIX, '__deposits/')) else 'data'
        if bucket != expected:
            self.wrong.append((bucket, key))

    def head_object(self, Bucket, Key):  # noqa: N803
        self._check(Bucket, Key)
        return super().head_object(Bucket=Bucket, Key=Key)

    def get_object(self, Bucket, Key, IfMatch=None):  # noqa: N803
        self._check(Bucket, Key)
        return super().get_object(Bucket=Bucket, Key=Key, IfMatch=IfMatch)

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        self._check(Bucket, Key)
        return super().put_object(Bucket=Bucket, Key=Key, Body=Body, **kwargs)

    def delete_object(self, Bucket, Key, **kwargs):  # noqa: N803
        self._check(Bucket, Key)
        return super().delete_object(Bucket=Bucket, Key=Key, **kwargs)

    def download_file(self, bucket, key, dest, Config=None):  # noqa: N803
        self._check(bucket, key)
        return super().download_file(bucket, key, dest, Config=Config)

    def upload_file(self, src, bucket, key, Config=None, ExtraArgs=None):  # noqa: N803
        self._check(bucket, key)
        return super().upload_file(src, bucket, key, Config=Config, ExtraArgs=ExtraArgs)

    def copy_object(self, Bucket, Key, CopySource, **kwargs):  # noqa: N803
        self._check(Bucket, Key)
        self._check(CopySource['Bucket'], CopySource['Key'])
        return super().copy_object(Bucket=Bucket, Key=Key, CopySource=CopySource, **kwargs)


@pytest.fixture()
def mailbox(monkeypatch):
    box = Mailbox()
    monkeypatch.setattr(ingest_watch.smtplib, 'SMTP', box.factory)
    return box


def central(tmp_path):
    """A watcher (its own state dir) and the deposit service of site "intern" (another state dir), one fake S3."""
    env = Env(tmp_path)
    env.s3 = BucketCheckS3(lambda: env.clock_value)
    env.pipeline.s3 = env.s3
    env.pipeline.config.bucket = 'data'
    env.pipeline.config.incoming_bucket = 'ingest'
    env.watcher.s3 = env.s3
    env.watcher.bucket = 'ingest'
    env.pipeline.list_de_studies = lambda path: set()
    env.settings['notify'] = {'on': [], 'smtp_host': 'smtp.test', 'email_from': 'anthive@test'}
    key = dr.load_or_create_result_key(env.state / 'secrets' / 'deposit_result_ed25519.pem')
    env.watcher.remote = remote_deposits.RemoteSites(
        s3=env.s3, bucket='ingest', state_dir=env.state, sites={'intern': {'k1': SECRET.encode()}}, result_key=key,
        staging_root='__deposits/', incoming_prefix=PREFIX, local_site_id='dev', log=lambda message: None)
    clock = [env.clock_value]
    import artefact_deposit
    settings = dd.DepositSettings(
        enabled=True, bucket='ingest', catalog_bucket='data', prefix=PREFIX, site_id='intern',
        staging_prefix='__deposits/intern/', s3={'access_key_id': 'k', 'secret_access_key': 's'},
        central_watcher=True, site_key_id='k1', site_secret=SECRET, watcher_public_keys=[dr.public_key_text(key)],
        notify={'on': [], 'smtp_host': 'smtp.test', 'email_from': 'anthive@test'})
    site_state = tmp_path / 'site_state'
    service = artefact_deposit.ArtefactDepositService(settings, dd.DepositStore(site_state / dd.DB_FILE), env.s3,
                                                      env.store, site_state, clock=lambda: clock[0])
    return env, service, clock


def _deposit(env, service):
    body, payloads = deposit_body(env.tmp)
    response = service.create(ALICE, body)
    upload(env.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    return response['deposit_id']


def test_a_site_deposit_is_published_by_the_central_watcher_and_answered(tmp_path, mailbox):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    record_key = f'__deposits/intern/{deposit_id}/record.json'
    assert record_key in env.s3.objects
    assert env.poll_twice() == {'live': 1}
    watcher_store = dd.DepositStore(env.state / dd.DB_FILE)
    assert watcher_store.get(deposit_id)['user'] == 'alice@intern'
    assert f'__deposits/intern/{deposit_id}/result.json' in env.s3.objects
    assert mailbox.messages == []                                   # the watcher never mails another site's user
    status = service.status(ALICE, deposit_id)
    assert status['state'] == 'live' and status['db_id'] == 'WT6/MIC' and status['version'] == '1.0.0'
    assert service.report(ALICE, deposit_id)['result'] in ('pass', 'fail')
    assert [message['To'] for message in mailbox.messages] == ['alice@example.org']   # the site sends it
    service.status(ALICE, deposit_id)
    assert len(mailbox.messages) == 1
    assert env.s3.wrong == []                                       # ingestion vs data bucket, every call
    assert env.store.joinpath(GROUP, 'micro.md').exists()


def test_a_record_changed_after_signing_is_never_imported(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    key = f'__deposits/intern/{deposit_id}/record.json'
    envelope = json.loads(env.s3.objects[key]['body'])
    envelope['record']['is_admin'] = 1
    env.s3.put(key, json.dumps(envelope).encode())
    counts = env.poll_twice()
    assert counts == {'needs_decision': 1}                          # deposit-marked objects nobody accounts for
    store = dd.DepositStore(env.state / dd.DB_FILE)
    assert store.get(deposit_id) is None
    assert f'__deposits/intern/{deposit_id}/result.json' not in env.s3.objects   # unauthenticated: no answer


def test_a_swapped_file_is_refused_with_a_signed_reason(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    seed_key = next(row['key'] for row in service.store.files(deposit_id) if row['kind'] == 'seed_md')
    env.s3.put(seed_key, b'---\ntitle: swapped\n---\n', env.clock_value)
    env.poll_twice()
    assert dd.DepositStore(env.state / dd.DB_FILE).get(deposit_id) is None
    status = service.status(ALICE, deposit_id)
    assert status['state'] == 'needs_decision'
    assert 'another ETag than the signed record' in (service.store.get(deposit_id)['error'] or '')


def test_an_update_of_a_dataset_the_central_store_does_not_record_for_the_user_is_refused(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    # bob of site intern claims an update; the site's own rules let him (policy any), the watcher does not
    service.settings.update_policy = 'any'
    body, payloads = deposit_body(env.tmp, seed=1, update_of='WT6/MIC', index=False)
    bob = dd.Identity(user='bob', name='Bob', email='bob@example.org')
    response = service.create(bob, body)
    upload(env.s3, response, payloads)
    service.complete(bob, response['deposit_id'])
    env.clock_value += 10
    env.poll_twice()
    assert dd.DepositStore(env.state / dd.DB_FILE).get(response['deposit_id']) is None
    status = service.status(bob, response['deposit_id'])
    assert status['state'] == 'needs_decision'
    assert 'does not record bob@intern as its owner' in service.store.get(response['deposit_id'])['error']
    service.withdraw(bob, response['deposit_id'])                  # the site removes its incoming copies
    # alice, the owner, may
    body, payloads = deposit_body(env.tmp, seed=2, update_of='WT6/MIC', index=False)
    response = service.create(ALICE, body)
    upload(env.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    env.clock_value += 10
    assert 'live' in env.poll_twice()


def test_an_unknown_site_is_ignored_and_a_local_site_is_left_to_the_shared_store(tmp_path):
    env, service, clock = central(tmp_path)
    env.watcher.remote.sites = {}
    deposit_id = _deposit(env, service)
    env.poll_twice()
    assert dd.DepositStore(env.state / dd.DB_FILE).get(deposit_id) is None


def test_the_site_refuses_approve_and_purge_and_keeps_records_in_the_sweep(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    admin = dd.Identity(user='root', is_admin=True)
    with pytest.raises(dd.DepositError) as exc:
        service.approve(admin, deposit_id)
    assert exc.value.body['status'] == 'central_watcher'
    service.sweep_staging()
    assert f'__deposits/intern/{deposit_id}/record.json' in env.s3.objects


def test_a_single_host_without_site_secrets_works_as_before(tmp_path):
    from tests.test_dataset_deposit_watch import deposit, make_env
    env, service, _ = make_env(tmp_path)
    assert env.watcher.remote is None and not service.settings.central_watcher
    deposit_id = deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    assert not [key for key in env.s3.objects if key.endswith('/record.json')]
    assert service.status(ALICE, deposit_id)['state'] == 'live'


def test_the_watcher_ignores_oversized_records_and_refuses_replays(tmp_path):
    env, service, clock = central(tmp_path)
    deposit_id = _deposit(env, service)
    key = f'__deposits/intern/{deposit_id}/record.json'
    first = env.s3.objects[key]['body']
    assert env.poll_twice() == {'live': 1}
    service.publish_record(deposit_id)                              # version 2
    env.watcher.poll()
    env.s3.put(key, first)                                          # an attacker puts version 1 back
    env.watcher.poll()
    state = json.loads((env.state / remote_deposits.STATE_FILE).read_text())
    assert state['seen'][f'intern/{deposit_id}'] == 2
    big = 'dep_' + 'd' * 20
    env.s3.put(f'__deposits/intern/{big}/record.json', b'{' + b' ' * (dr.MAX_RECORD_BYTES + 10) + b'}')
    counts = env.watcher.remote.import_records(dd.DepositStore(env.state / dd.DB_FILE), {})
    assert counts['ignored'] >= 1 and dd.DepositStore(env.state / dd.DB_FILE).get(big) is None


def test_record_paths_outside_the_layout_are_ignored(tmp_path):
    env, service, clock = central(tmp_path)
    env.s3.put('__deposits/intern/../prod/record.json', b'{}')
    env.s3.put('__deposits/intern/dep_x/record.json', b'{}')
    counts = env.watcher.remote.import_records(dd.DepositStore(env.state / dd.DB_FILE), {})
    assert counts['imported'] == 0 and counts['ignored'] == 2
