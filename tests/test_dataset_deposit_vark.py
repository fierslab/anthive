"""Server changes asked for by the vark client (phase 2): facet registry and required specs in the vocab,
re-declaring a sidecar, DE-only deposits, the group lookup, corrections and orphaned h5ads, async complete."""
from __future__ import annotations

import json
import time

import pytest
import yaml

import dataset_deposit as dd
import obscol_tags
from tests.deposit_helpers import (ADMIN, ALICE, BOB, STORE_DB_ID, file_entry, make_service, sha256_bytes,
                                   sidecar_for, simple_deposit, upload)
from tests.ingest_watch_helpers import PREFIX
from tests.test_dataset_deposit_watch import GROUP, deposit, make_env


# ── 1, 2: vocab ────────────────────────────────────────────────────────────

def test_vocab_lists_every_registered_facet_and_machine_readable_requirements(tmp_path):
    vocab = make_service(tmp_path, [time.time()]).vocab()
    facets = {facet['column']: facet for facet in vocab['facets']}
    assert len(facets) == len(obscol_tags.facet_registry())
    assert facets['ant_sex']['controlled_values'] == ['m', 'f'] and 'sex' in facets['ant_sex']['hint_keywords']
    assert facets['ant_sex']['status'] == 'registered' and facets['ant_sex']['facet_scopes']
    assert any(facet['status'] == 'deprecated' for facet in facets.values())
    for key in ('kind', 'range', 'facet_scopes', 'hint_keywords', 'status'):
        assert key in facets['ant_braak']
    seed = vocab['seed_md']['required_spec']
    assert seed['keys'] == ['title', 'replicate_unit', 'api'] and seed['description']['min_chars'] == 200
    index = vocab['index_md']['required_spec']
    assert ['doi', 'paper_title'] in index['one_of'] and index['group_id']['length'] == 3
    assert vocab['limits']['async_complete_gb'] == 5.0


# ── 3: re-declare the sidecar ──────────────────────────────────────────────

def test_a_corrected_sidecar_can_be_declared_again_in_the_same_deposit(tmp_path):
    service = make_service(tmp_path, [time.time()])
    body, payloads = simple_deposit()
    wrong = sidecar_for('0' * 64)
    payloads['cells.h5ad.mus'] = wrong
    body['files'][1] = file_entry('cells.h5ad.mus', wrong, 'mus')
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert exc.value.body['status'] == 'sidecar_mismatch' and 'POST .../urls' in exc.value.body['message']
    right = sidecar_for(sha256_bytes(payloads['cells.h5ad']), license='CC0-1.0')
    fresh = service.refresh_urls(ALICE, response['deposit_id'], {'name': 'cells.h5ad.mus', 'size': len(right),
                                                                 'sha256': sha256_bytes(right)})
    assert fresh['size'] == len(right) and fresh['headers']['x-amz-meta-sha256'] == sha256_bytes(right)
    service.s3.client_put(fresh['url'], right, fresh['headers'])
    assert service.complete(ALICE, response['deposit_id'])['state'] == 'uploaded'
    assert service.s3.objects[f'{PREFIX}New2026/cells.h5ad.mus']['body'] == right
    record = service.store.get(response['deposit_id'])
    assert record['total_bytes'] == sum(len(body_) for name, body_ in payloads.items()) - len(wrong) + len(right)


def test_only_the_sidecar_can_be_declared_again(tmp_path):
    service = make_service(tmp_path, [time.time()])
    deposit_id = service.create(ALICE, simple_deposit()[0])['deposit_id']
    for name in ('cells.h5ad', 'cells.md'):
        with pytest.raises(dd.DepositError) as exc:
            service.refresh_urls(ALICE, deposit_id, {'name': name, 'size': 3, 'sha256': 'a' * 64})
        assert exc.value.status == 422
    for bad in ({'size': -1, 'sha256': 'a' * 64}, {'size': 3, 'sha256': 'x'}, {'size': dd.TEXT_MAX_BYTES + 1,
                                                                             'sha256': 'a' * 64}):
        with pytest.raises(dd.DepositError):
            service.refresh_urls(ALICE, deposit_id, {'name': 'cells.h5ad.mus', **bad})


# ── 4: DE tables for a published dataset ───────────────────────────────────

def de_files(dataset_id='MIC', h5ad='micro.h5ad'):
    table = b'gene\tlogFC\tpvalue\nTREM2\t1.2\t0.001\n'
    sidecar = (f'de_format=deseq2\nde_dataset_id={dataset_id}\nde_dataset_h5ad={h5ad}\n'
               f'sha256={sha256_bytes(table)}\n').encode()
    return {'deseq/ad_vs_ctrl.tsv': table, 'deseq/ad_vs_ctrl.tsv.mus': sidecar}


def de_body(payloads, update_of='WT6/MIC', basename='micro'):
    return {'data_project': GROUP, 'basename': basename, 'update_of': update_of,
            'files': [file_entry(name, body, 'de') for name, body in payloads.items()]}


def test_de_only_deposit_rules(tmp_path):
    env, service, _ = make_env(tmp_path)
    env.deposit()
    assert env.poll_twice() == {'live': 1}
    payloads = de_files()
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, {**de_body(payloads), 'update_of': None})
    assert exc.value.status == 422
    with pytest.raises(dd.DepositError) as exc:                       # not the owner of a mus-route dataset
        service.create(ALICE, de_body(payloads))
    assert exc.value.body['status'] == 'name_taken'
    lonely = {'deseq/ad_vs_ctrl.tsv': payloads['deseq/ad_vs_ctrl.tsv']}
    with pytest.raises(dd.DepositError) as exc:
        service.create(ADMIN, de_body(lonely))
    assert '.mus is missing' in exc.value.body['message']
    response = service.create(ADMIN, de_body(de_files(dataset_id='OTHER', h5ad='other.h5ad')))
    upload(env.s3, response, de_files(dataset_id='OTHER', h5ad='other.h5ad'))
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ADMIN, response['deposit_id'])
    assert exc.value.body['status'] == 'de_mismatch'


def test_de_only_deposit_is_imported_into_the_published_dataset(tmp_path):
    env, service, clock = make_env(tmp_path)
    (env.store / '.git').mkdir()
    env.deposit()
    assert env.poll_twice() == {'live': 1}
    service.set_owner(ADMIN, 'WT6/MIC', 'alice')
    payloads = de_files()
    response = service.create(ALICE, de_body(payloads))
    assert response['deposit_kind'] == 'de_only'
    upload(env.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    env.clock_value += 10
    counts = env.watcher.poll()
    assert counts.get('live') == 1
    status = service.status(ALICE, response['deposit_id'])
    assert status['state'] == 'live' and status['deposit_kind'] == 'de_only' and status['version'] == '1.0.2'   # 1.0.1 was the owner caveat
    commands = [cmd for cmd in env.runner.commands if cmd[0].endswith('ant-de-ingest')]
    assert commands and commands[0][1].endswith(f'{GROUP}/micro.duckdb')
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    assert md['version'] == '1.0.2' and 'ad_vs_ctrl.tsv' in ' '.join(md['changelog'][-1]['changes'])
    assert not [key for key in env.s3.objects if '/de/' in key]           # consumed and deleted
    assert env.watcher.poll().get('live') is None                        # processed once


def test_a_failing_de_import_keeps_the_files(tmp_path):
    env, service, _ = make_env(tmp_path)
    env.deposit()
    env.poll_twice()
    env.runner.failing_steps = set()
    original = env.runner.run

    def run(label, cmd, cwd=None, timeout=None):
        if str(cmd[0]).endswith('ant-de-ingest'):
            return 1, 'bad table'
        return original(label, cmd, cwd, timeout)
    env.runner.run = run
    payloads = de_files()
    response = service.create(ADMIN, de_body(payloads))
    upload(env.s3, response, payloads)
    service.complete(ADMIN, response['deposit_id'])
    assert env.watcher.poll().get('failed') == 1
    assert service.status(ADMIN, response['deposit_id'])['state'] == 'failed'
    assert [key for key in env.s3.objects if '/de/' in key]


# ── 5: group lookup ────────────────────────────────────────────────────────

def test_group_lookup(tmp_path):
    service = make_service(tmp_path, [time.time()])
    assert service.group_info(ALICE, 'Old2020') == {'data_project': 'Old2020', 'exists': True, 'group_id': 'OL2'}
    assert service.group_info(ALICE, 'New2026')['may_create'] is True
    bad = service.group_info(ALICE, 'old2020')
    assert bad['may_create'] is False and 'Old2020' not in bad['reason']
    mine = service.group_info(ALICE, 'Old2020', 'micro')
    assert mine['dataset_exists'] is True and mine['may_update'] is False and 'db_id' not in mine
    assert service.group_info(ADMIN, 'Old2020', 'micro')['db_id'] == STORE_DB_ID
    assert service.group_info(ALICE, 'Old2020', 'fresh')['dataset_exists'] is False
    with pytest.raises(dd.DepositError):
        service.group_info(ALICE, '../x')


# ── 6: an h5ad without its sidecar ─────────────────────────────────────────

def test_a_deposit_h5ad_without_sidecar_is_needs_decision(tmp_path):
    env, service, _ = make_env(tmp_path, report={'result': 'pass', 'publish': 'held',
                                                 'held_reasons': ['licence_unknown'], 'checks': []})
    deposit_id = deposit(env, service)
    env.poll_twice()
    del env.s3.objects[f'{PREFIX}{GROUP}/micro.h5ad.mus']           # e.g. a withdrawn correction took it away
    env.clock_value += 10
    env.watcher.poll()
    row = env.db.get(f'{PREFIX}{GROUP}/micro.h5ad')
    assert row['state'] == 'needs_decision' and deposit_id in row['error'] and 'no sidecar' in row['error']


# ── 7: asynchronous complete ───────────────────────────────────────────────

def test_large_deposits_complete_in_the_background(tmp_path):
    clock = [time.time()]
    service = make_service(tmp_path, clock, async_complete_gb=1e-9)
    queued = []
    service.background = queued.append
    body, payloads = simple_deposit()
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    answer = service.complete(ALICE, response['deposit_id'])
    assert answer['_http_status'] == 202 and answer['state'] == 'completing'
    assert answer['progress'] == {'files_total': 4, 'files_copied': 0, 'bytes_total': response['total_bytes'],
                                  'bytes_copied': 0}
    status = service.status(ALICE, response['deposit_id'])
    assert status['state'] == 'completing' and status['progress']['files_copied'] == 0
    assert not [key for key in service.s3.objects if key.startswith(PREFIX)]
    queued.pop()()
    status = service.status(ALICE, response['deposit_id'])
    assert status['state'] == 'uploaded' and 'progress' not in status


def test_a_failing_background_complete_reports_its_error(tmp_path):
    clock = [time.time()]
    service = make_service(tmp_path, clock, async_complete_gb=1e-9)
    queued = []
    service.background = queued.append
    body, payloads = simple_deposit()
    response = service.create(ALICE, body)
    mus = next(entry for entry in response['files'] if entry['kind'] == 'mus')
    upload(service.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    service.s3.client_put(mus['url'], payloads['cells.h5ad.mus'][:-1] + b'#', mus['headers'])   # re-PUT meanwhile
    queued.pop()()
    status = service.status(ALICE, response['deposit_id'])
    assert status['state'] == 'uploading' and status['complete_error']['status'] == 'staging_changed'
    assert not [key for key in service.s3.objects if key.startswith(PREFIX)]


def test_a_restart_resumes_an_interrupted_complete(tmp_path):
    clock = [time.time()]
    first = make_service(tmp_path, clock, async_complete_gb=1e-9)
    first.background = lambda work: None              # the process dies before the copies
    body, payloads = simple_deposit()
    response = first.create(ALICE, body)
    upload(first.s3, response, payloads)
    first.complete(ALICE, response['deposit_id'])
    assert first.store.get(response['deposit_id'])['state'] == 'completing'
    second = dd.DepositService(first.settings, dd.DepositStore(first.store.path), first.s3, first.store_root,
                               first.state_dir, clock=lambda: clock[0], background=lambda work: work())
    assert second.resume_interrupted() == [response['deposit_id']]
    assert second.store.get(response['deposit_id'])['state'] == 'uploaded'
    assert second.resume_interrupted() == []
