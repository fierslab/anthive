"""Skill test 3 fixes: `caveats: []` seeds, retry of needs_decision, final rejects, kinds, designation, texts."""
from __future__ import annotations

import json

import pytest
import yaml

import dataset_deposit as dd
import ingest_watch
import mus
from tests.deposit_helpers import ADMIN, ALICE, file_entry, upload
from tests.ingest_watch_helpers import CATALOG, PREFIX
from tests.test_dataset_deposit_watch import GROUP, SEED_MD, deposit, make_env

KEY = f'{PREFIX}{GROUP}/micro.h5ad'
VARK_SEED = SEED_MD.replace('replicate_unit: donor\n', 'replicate_unit: donor\ncaveats: []\n')


def tick(env, clock, seconds=10):
    clock[0] = env.clock_value = env.clock_value + seconds


def published_with_flow_caveats(tmp_path, monkeypatch):
    import tests.test_dataset_deposit_watch as watch_tests
    monkeypatch.setattr(watch_tests, 'SEED_MD', VARK_SEED)
    env, service, clock = make_env(tmp_path)
    service.settings.catalog_prefix = CATALOG
    deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    assert 'caveats: []' in (env.store / GROUP / 'micro.md').read_text()
    return env, service, clock


def test_an_update_adding_a_caveat_to_a_vark_seeded_dataset_goes_live(tmp_path, monkeypatch):
    env, service, clock = published_with_flow_caveats(tmp_path, monkeypatch)
    seed = VARK_SEED.replace('caveats: []\n', 'caveats:\n- level: low\n  kind: note\n  message: A new caveat.\n')
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.md', seed.encode(), 'seed_md')]}
    response = service.create(ALICE, body)
    upload(env.s3, {'files': [entry for entry in response['files'] if entry['method'] != 'reuse']},
           {'micro.md': seed.encode()})
    service.complete(ALICE, response['deposit_id'])
    tick(env, clock)
    assert env.poll_twice() == {'live': 1}
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    assert md['version'] == '1.0.1' and any('new caveat' in caveat['message'] for caveat in md['caveats'])


def test_a_new_dataset_without_designation_is_served_as_primary(tmp_path):
    env, service, _ = make_env(tmp_path)
    deposit(env, service)
    env.poll_twice()
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    assert md['is_secondary'] is False
    assert any('Designation: primary dataset' in change for change in md['changelog'][-1]['changes'])


def test_needs_decision_is_retried_by_an_admin(tmp_path):
    report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['licence_unknown'], 'checks': []}
    env, service, _ = make_env(tmp_path, report=report)
    deposit_id = deposit(env, service)
    env.poll_twice()
    env.db.transition(KEY, 'needs_decision', 'the store .md cannot be edited safely (test)',
                      error='the store .md cannot be edited safely (test)')
    with pytest.raises(dd.DepositError) as exc:
        service.approve(ADMIN, deposit_id)
    assert exc.value.body['status'] == 'not_held' and '/retry' in exc.value.body['message']
    with pytest.raises(dd.DepositError) as exc:
        service.retry(ALICE, deposit_id)
    assert exc.value.status == 403
    result = service.retry(ADMIN, deposit_id, 'store .md fixed by hand')
    assert result['previous_state'] == 'needs_decision' and result['state'] == 'checking'
    row = env.db.get(KEY)
    assert row['state'] == 'stable' and 'retry requested by root (API login root)' in \
        env.db.events(KEY)[-1]['detail']
    assert env.watcher.poll() == {'held': 1}                     # ran again (and stopped where the report says)
    with pytest.raises(dd.DepositError) as exc:
        service.retry(ADMIN, deposit_id)
    assert exc.value.body['status'] == 'not_retryable' and 'approved or rejected' in exc.value.body['message']
    env.db.transition(KEY, 'needs_decision', 'again')
    with pytest.raises(ValueError, match='retry it'):
        ingest_watch.approve(env.db, KEY, 'root')


def test_reject_deletes_the_incoming_objects_and_is_never_continued(tmp_path):
    report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['licence_unknown'], 'checks': []}
    env, service, _ = make_env(tmp_path, report=report)
    deposit_id = deposit(env, service)
    env.poll_twice()
    assert [key for key in env.s3.objects if key.startswith(f'{PREFIX}{GROUP}/')]
    result = service.reject(ADMIN, deposit_id, 'wrong cohort')
    assert result['state'] == 'rejected' and len(result['deleted']) == 4 and not result['kept']
    assert not [key for key in env.s3.objects if key.startswith(f'{PREFIX}{GROUP}/')]
    assert not [call for call in env.s3.calls if call[0] == 'delete' and not call[1].startswith(PREFIX)
                and not call[1].startswith('__deposits')]
    status = service.status(ALICE, deposit_id)
    assert status['state'] == 'rejected'
    assert status['error'] == 'rejected by an admin: wrong cohort'                   # no "(API login an admin)"
    assert 'API login' not in json.dumps(status)
    assert 'API login root' in service.status(ADMIN, deposit_id)['error']             # admins see the login
    # a correction (sidecar only) has nothing to continue
    body = {'data_project': GROUP, 'basename': 'micro',
            'files': [file_entry('micro.h5ad.mus', b'sha256=x\n', 'mus')]}
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, body)
    assert exc.value.body['status'] == 'h5ad_required'
    assert service.withdraw(ALICE, deposit_id)['deleted'] == []


def test_a_leftover_object_of_a_rejected_deposit_is_not_continued(tmp_path):
    report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['licence_unknown'], 'checks': []}
    env, service, _ = make_env(tmp_path, report=report)
    deposit_id = deposit(env, service)
    env.poll_twice()
    kept = dict(env.s3.objects[KEY])
    service.reject(ADMIN, deposit_id)
    env.s3.objects[KEY] = kept                                  # e.g. a delete that did not happen
    body = {'data_project': GROUP, 'basename': 'micro',
            'files': [file_entry('micro.h5ad.mus', b'sha256=x\n', 'mus')]}
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, body)
    assert exc.value.status == 409 and exc.value.body['status'] == 'rejected'
    assert deposit_id in exc.value.body['message'] and 'not continued' in exc.value.body['message']


def test_a_reject_on_the_host_is_cleaned_up_by_the_next_poll(tmp_path):
    report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['licence_unknown'], 'checks': []}
    env, service, _ = make_env(tmp_path, report=report)
    deposit_id = deposit(env, service)
    env.poll_twice()
    row = env.db.get(KEY)
    ingest_watch.reject(env.db, KEY, 'admin-on-host')
    assert ingest_watch.deposit_of_row(row) == deposit_id
    service.store.update(deposit_id, watch_state='rejected', error='rejected by an admin')   # what the CLI does
    assert service.pending_rejected() == [deposit_id]
    env.watcher.poll()
    assert not [key for key in env.s3.objects if key.startswith(f'{PREFIX}{GROUP}/')]
    assert service.pending_rejected() == []


def test_a_correction_of_a_metadata_update_keeps_its_kind(tmp_path):
    env, service, clock = make_env(tmp_path)
    service.settings.catalog_prefix = CATALOG
    deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    sidecar = env.s3.objects[f'{CATALOG}{GROUP}/micro.h5ad.mus']['body']
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.h5ad.mus', sidecar, 'mus')]}
    first = service.create(ALICE, body)
    upload(env.s3, {'files': [entry for entry in first['files'] if entry['method'] != 'reuse']},
           {'micro.h5ad.mus': sidecar})
    service.complete(ALICE, first['deposit_id'])
    corrected = sidecar + b'age=33-62 years\n'
    second = service.create(ALICE, {**body, 'files': [file_entry('micro.h5ad.mus', corrected, 'mus')]})
    assert second['continues'] == first['deposit_id']
    assert second['deposit_kind'] == 'metadata_update'
    assert service.store.get(second['deposit_id'])['deposit_kind'] == 'metadata_update'


def test_vocab_field_info_and_organism_specific_soft_fields(tmp_path):
    env, service, _ = make_env(tmp_path)
    info = service.vocab(ALICE)['mus']['field_info']
    assert set(info) == set(mus.soft_fields())
    assert info['strain']['organisms'] and 'human' not in info['strain']['organisms']
    assert 'range' in info['age']['form'] and info['age']['organisms'] is None
    assert 'mixed' in info['sex']['values']
    verdict = mus.validate_annotation({'organism': 'human'})
    assert 'strain' not in verdict['missing_soft'] and 'age' in verdict['missing_soft']
    assert 'strain' in mus.validate_annotation({'organism': 'mouse'})['missing_soft']


def test_report_of_a_deposit_stopped_before_the_checks_gives_the_reason(tmp_path):
    report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['licence_unknown'], 'checks': []}
    env, service, _ = make_env(tmp_path, report=report)
    deposit_id = deposit(env, service)
    env.poll_twice()
    dd.report_copy_path(env.state, deposit_id).unlink()
    env.db.transition(KEY, 'needs_decision', 'the store .md cannot be edited safely', report_path=None,
                      error='the store .md cannot be edited safely')
    with pytest.raises(dd.DepositError) as exc:
        service.report(ALICE, deposit_id)
    body = exc.value.body
    assert exc.value.status == 404 and body['state'] == 'needs_decision'
    assert body['reason'] == 'the store .md cannot be edited safely' and 'Reason:' in body['message']
