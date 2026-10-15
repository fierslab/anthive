"""Regression tests for the third review of the deposit API (2026-10-15): R1 to R5 and the nits."""
from __future__ import annotations

import time

import pytest
import yaml

import dataset_deposit as dd
from ingest_pipeline import de_sidecar_names
from tests.deposit_helpers import (ADMIN, ALICE, file_entry, make_service, sha256_bytes, sidecar_for, simple_deposit,
                                   upload)
from tests.ingest_watch_helpers import PREFIX
from tests.test_dataset_deposit_vark import de_body, de_files
from tests.test_dataset_deposit_watch import GROUP, make_env


def published(tmp_path, studies=()):
    env, service, clock = make_env(tmp_path)
    env.deposit()
    assert env.poll_twice() == {'live': 1}
    env.pipeline.list_de_studies = lambda path: set(studies)
    return env, service, clock


def ingest_runs(env):
    return [cmd for cmd in env.runner.commands if any(part.endswith('ant-de-ingest') for part in cmd)]


def version(env):
    return yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])['version']


# ── R1: DE-only imports ────────────────────────────────────────────────────

def test_an_existing_study_is_refused_for_a_depositor(tmp_path):
    env, service, _ = published(tmp_path, studies={'ad_vs_ctrl'})
    service.set_owner(ADMIN, 'WT6/MIC', 'alice')
    env.watcher.poll()                                  # the owner caveat: 1.0.1
    payloads = de_files()
    response = service.create(ALICE, de_body(payloads))
    upload(env.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    assert env.watcher.poll().get('failed') == 1
    status = service.status(ALICE, response['deposit_id'])
    assert status['state'] == 'failed' and 'already has the DE study ad_vs_ctrl' in status['error']
    assert ingest_runs(env) == [] and version(env) == '1.0.1'
    assert [key for key in env.s3.objects if '/de/' in key]           # nothing consumed


def test_an_admin_replaces_a_study_and_the_previous_one_is_archived(tmp_path):
    env, service, _ = published(tmp_path, studies={'ad_vs_ctrl'})
    (env.store / GROUP / 'micro.de.parquet').write_bytes(b'previous studies')
    payloads = de_files()
    response = service.create(ADMIN, de_body(payloads))
    upload(env.s3, response, payloads)
    service.complete(ADMIN, response['deposit_id'])
    assert env.watcher.poll().get('live') == 1
    assert '--replace' in ingest_runs(env)[0]
    archived = list((env.store / GROUP / '_versions' / 'micro' / 'de' / 'ad_vs_ctrl').glob('*.de.parquet'))
    assert len(archived) == 1 and archived[0].read_bytes() == b'previous studies'
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    assert 'replaced ad_vs_ctrl' in md['changelog'][-1]['changes'][0] and md['version'] == '1.0.1'


def test_a_failed_import_changes_nothing(tmp_path):
    env, service, _ = published(tmp_path)
    duckdb_before = (env.store / GROUP / 'micro.duckdb').read_bytes()
    md_before = (env.store / GROUP / 'micro.md').read_text()
    payloads = de_files()
    second = b'gene\tlogFC\tpvalue\nAPOE\t2\t0.01\n'
    payloads['deseq/zz_second.tsv'] = second
    payloads['deseq/zz_second.tsv.mus'] = (f'de_format=deseq2\nde_dataset_id=MIC\nsha256={sha256_bytes(second)}\n'
                                          ).encode()
    original = env.runner.run

    def run(label, cmd, cwd=None, timeout=None):
        if any(str(part).endswith('ant-de-ingest') for part in cmd):
            served = env.store / GROUP / 'micro.duckdb'
            assert str(served) not in [str(part) for part in cmd]     # never the served duckdb
            return (1, 'bad') if any('zz_second' in str(part) for part in cmd) else (0, 'ok')
        return original(label, cmd, cwd, timeout)
    env.runner.run = run
    response = service.create(ADMIN, de_body(payloads))
    upload(env.s3, response, payloads)
    service.complete(ADMIN, response['deposit_id'])
    assert env.watcher.poll().get('failed') == 1
    assert (env.store / GROUP / 'micro.duckdb').read_bytes() == duckdb_before
    assert (env.store / GROUP / 'micro.md').read_text() == md_before
    assert not (env.store / GROUP / 'de_sources').exists()
    assert 'nothing was changed' in service.status(ADMIN, response['deposit_id'])['error']


def test_de_sources_are_kept_per_dataset(tmp_path):
    env, service, _ = published(tmp_path)
    payloads = de_files()
    response = service.create(ADMIN, de_body(payloads))
    upload(env.s3, response, payloads)
    service.complete(ADMIN, response['deposit_id'])
    env.watcher.poll()
    assert (env.store / GROUP / 'de_sources' / 'micro' / 'deseq' / 'ad_vs_ctrl.tsv').exists()
    assert not (env.store / GROUP / 'de_sources' / 'deseq').exists()


def test_the_api_and_the_import_use_one_naming_rule():
    assert de_sidecar_names({'de_dataset_id': 'MIC'}, 'mic', 'micro')
    assert not de_sidecar_names({'de_dataset_id': 'OTHER', 'de_dataset_h5ad': 'micro.h5ad'}, 'MIC', 'micro')
    assert de_sidecar_names({'de_dataset_h5ad': 'micro.h5ad'}, 'MIC', 'micro')


def test_api_refuses_a_sidecar_whose_id_names_another_dataset(tmp_path):
    env, service, _ = published(tmp_path)
    payloads = de_files(dataset_id='OTHER', h5ad='micro.h5ad')            # the h5ad matches, the id does not
    response = service.create(ADMIN, de_body(payloads))
    upload(env.s3, response, payloads)
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ADMIN, response['deposit_id'])
    assert exc.value.body['status'] == 'de_mismatch'


# ── R4: importing locks withdraw ───────────────────────────────────────────

def test_withdraw_waits_while_the_watcher_imports(tmp_path):
    env, service, _ = published(tmp_path)
    payloads = de_files()
    response = service.create(ADMIN, de_body(payloads))
    upload(env.s3, response, payloads)
    service.complete(ADMIN, response['deposit_id'])
    assert service.store.claim_import(response['deposit_id'])
    assert not service.store.claim_import(response['deposit_id'])
    assert service.status(ADMIN, response['deposit_id'])['state'] == 'importing'
    with pytest.raises(dd.DepositError) as exc:
        service.withdraw(ADMIN, response['deposit_id'])
    assert exc.value.body['status'] == 'busy'
    # a claim a dead run left behind is retried by the next poll
    assert env.watcher.poll().get('live') == 1


# ── R2: lease and the end of complete ──────────────────────────────────────

def test_resume_does_not_steal_a_live_background_complete(tmp_path):
    clock = [time.time()]
    first = make_service(tmp_path, clock, async_complete_gb=1e-9)
    pending = []
    first.background = pending.append
    body, payloads = simple_deposit()
    response = first.create(ALICE, body)
    upload(first.s3, response, payloads)
    first.complete(ALICE, response['deposit_id'])
    second = dd.DepositService(first.settings, dd.DepositStore(first.store.path), first.s3, first.store_root,
                               first.state_dir, clock=lambda: clock[0], background=lambda work: work())
    assert second.resume_interrupted() == []              # the lease is fresh
    pending[0]()
    assert first.store.get(response['deposit_id'])['state'] == 'uploaded'


def test_files_are_marked_complete_only_with_the_final_state(tmp_path):
    clock = [time.time()]
    service = make_service(tmp_path, clock)
    body, payloads = simple_deposit()
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    original = service.store.finish_complete
    seen = {}

    def spy(deposit_id, file_etags, now, **fields):
        seen['states'] = {row['state'] for row in service.store.files(deposit_id)}
        seen['staging'] = [key for key in service.s3.objects if key.startswith('__deposits/')]
        return original(deposit_id, file_etags, now, **fields)
    service.store.finish_complete = spy
    service.complete(ALICE, response['deposit_id'])
    assert 'complete' not in seen['states'] and len(seen['staging']) == 4
    assert {row['state'] for row in service.store.files(response['deposit_id'])} == {'complete'}
    assert not [key for key in service.s3.objects if key.startswith('__deposits/')]


# ── R3: cascade withdraw never leaves a correction withdrawing ─────────────

def test_a_failing_correction_withdraw_is_rolled_back(tmp_path):
    service = make_service(tmp_path, [time.time()])
    response, payloads = service.create(ALICE, simple_deposit()[0]), simple_deposit()[1]
    upload(service.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    sidecar = sidecar_for(sha256_bytes(payloads['cells.h5ad']), license='CC0-1.0')
    follow = service.create(ALICE, {'data_project': 'New2026', 'basename': 'cells',
                                    'files': [file_entry('cells.h5ad.mus', sidecar, 'mus')]})
    original = service._abort_and_delete

    def failing(record, actor):
        if record['id'] == follow['deposit_id']:
            raise RuntimeError('s3 down')
        return original(record, actor)
    service._abort_and_delete = failing
    with pytest.raises(dd.DepositError) as exc:
        service.withdraw(ALICE, response['deposit_id'])
    assert exc.value.status == 502
    assert service.store.get(follow['deposit_id'])['state'] == 'uploading'
    assert service.store.get(response['deposit_id'])['state'] == 'uploaded'


# ── R5: re-declare only while uploading ────────────────────────────────────

def test_redeclare_is_refused_once_the_deposit_left_uploading(tmp_path):
    service = make_service(tmp_path, [time.time()])
    deposit_id = service.create(ALICE, simple_deposit()[0])['deposit_id']
    assert service.store.redeclare(deposit_id, 'cells.h5ad.mus', 10, 'a' * 64)
    service.store.cas(deposit_id, ('uploading',), 'completing')
    assert not service.store.redeclare(deposit_id, 'cells.h5ad.mus', 11, 'b' * 64)
    assert not service.store.redeclare(deposit_id, 'cells.h5ad', 11, 'b' * 64)        # not the sidecar


# ── nits ───────────────────────────────────────────────────────────────────

def test_group_lookup_hides_retired_datasets_and_is_rate_limited(tmp_path):
    clock = [time.time()]
    service = make_service(tmp_path, clock)
    (tmp_path / 'store' / 'Old2020' / 'micro.inactive').write_text('')
    service.set_owner(ADMIN, 'OL2/mic', 'alice')
    answer = service.group_info(ALICE, 'Old2020', 'micro')
    assert answer['may_update'] is False and 'db_id' not in answer
    assert service.group_info(ADMIN, 'Old2020', 'micro')['inactive'] is True
    service.lookup_limiter.per_minute = 2
    service.lookup_limiter.calls.clear()
    service.group_info(ALICE, 'Old2020')
    service.group_info(ALICE, 'Old2020')
    with pytest.raises(dd.DepositError) as exc:
        service.group_info(ALICE, 'Old2020')
    assert exc.value.status == 429


def test_deploy_guide_recommends_an_abort_incomplete_multipart_rule(tmp_path, capsys):
    from pathlib import Path
    guide = (Path(__file__).resolve().parent.parent / 'doc' / 'DEPLOY_CONTAINERS.md').read_text()
    assert 'AbortIncompleteMultipartUpload' in guide
    from tests.test_dataset_deposit_stack import S3, _secret
    from tests.test_deploy_config import _load_script, _site
    ant_stack = _load_script('ant_stack_lifecycle_test', 'bin/ops/ant-stack')
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3=S3)
    for name in ('s3_access_key_id', 's3_secret_access_key'):
        _secret(site, name, 'x')
    ant_stack._check_datasets(site)
    assert 'AbortIncompleteMultipartUpload' in capsys.readouterr().out
