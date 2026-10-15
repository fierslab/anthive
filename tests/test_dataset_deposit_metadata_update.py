"""Metadata-only updates (the published h5ad is reused), DE presets in the vocab, mus' literal 'auto'."""
from __future__ import annotations

import time

import pytest
import yaml

import dataset_deposit as dd
from ingest_pipeline import de_ingest_command, de_sidecar_problems
from tests.deposit_helpers import ADMIN, ALICE, BOB, file_entry, make_service, upload
from tests.ingest_watch_helpers import CATALOG, PREFIX
from tests.test_dataset_deposit_watch import GROUP, SEED_MD, deposit, make_env


def published_by_alice(tmp_path):
    env, service, clock = make_env(tmp_path)
    service.settings.catalog_prefix = CATALOG
    deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    return env, service, clock


def sidecar_with(env, **extra):
    body = env.s3.objects[f'{CATALOG}{GROUP}/micro.h5ad.mus']['body'].decode()
    lines = [line for line in body.splitlines() if not line.startswith('s3_')]
    lines += [f'{key}={value}' for key, value in extra.items()]
    return ('\n'.join(lines) + '\n').encode()


def test_a_sidecar_only_update_reuses_the_published_h5ad(tmp_path):
    env, service, clock = published_by_alice(tmp_path)
    sidecar = sidecar_with(env, strain='C57BL/6')
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.h5ad.mus', sidecar, 'mus')]}
    response = service.create(ALICE, body)
    assert response['deposit_kind'] == 'metadata_update'
    reused = [entry for entry in response['files'] if entry['method'] == 'reuse']
    assert [entry['name'] for entry in reused] == ['micro.h5ad'] and reused[0]['upload_key'] is None
    upload(env.s3, {'files': [entry for entry in response['files'] if entry['method'] != 'reuse']},
           {'micro.h5ad.mus': sidecar})
    assert service.complete(ALICE, response['deposit_id'])['state'] == 'uploaded'
    assert f'{PREFIX}{GROUP}/micro.h5ad' in env.s3.objects                     # copied from the catalog
    assert f'{CATALOG}{GROUP}/micro.h5ad' in env.s3.objects                    # the published copy stays
    clock[0] = env.clock_value = env.clock_value + 10
    assert env.poll_twice() == {'live': 1}
    status = service.status(ALICE, response['deposit_id'])
    assert status['state'] == 'live' and status['version'] == '1.0.1' and status['kind'] == 'update_patch'
    assert 'strain=C57BL/6' in (env.store / GROUP / 'micro.h5ad.mus').read_text()
    assert f'{CATALOG}{GROUP}/micro.h5ad' in env.s3.objects
    assert f'{PREFIX}{GROUP}/micro.h5ad' not in env.s3.objects                 # consumed copy deleted


def test_a_seed_only_update_reuses_h5ad_and_sidecar(tmp_path):
    env, service, clock = published_by_alice(tmp_path)
    seed = SEED_MD.replace('replicate_unit: donor\n', 'replicate_unit: donor\ncaveats:\n- level: low\n  kind: note\n'
                                                        '  message: A new caveat from the depositor.\n').encode()
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.md', seed, 'seed_md')]}
    response = service.create(ALICE, body)
    assert sorted(entry['name'] for entry in response['files'] if entry['method'] == 'reuse') == \
        ['micro.h5ad', 'micro.h5ad.mus']
    upload(env.s3, {'files': [entry for entry in response['files'] if entry['method'] != 'reuse']}, {'micro.md': seed})
    service.complete(ALICE, response['deposit_id'])
    clock[0] = env.clock_value = env.clock_value + 10
    env.poll_twice()
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    assert md['version'] == '1.0.1' and any('new caveat' in caveat['message'] for caveat in md['caveats'])


def test_metadata_update_rules(tmp_path):
    env, service, _ = published_by_alice(tmp_path)
    sidecar = sidecar_with(env)
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.h5ad.mus', sidecar, 'mus')]}
    with pytest.raises(dd.DepositError) as exc:
        service.create(BOB, body)
    assert exc.value.body['status'] == 'name_taken'
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, {**body, 'files': [file_entry('paper.pdf', b'%PDF', 'static')]})
    assert exc.value.status == 422
    response = service.create(ALICE, body)
    with pytest.raises(dd.DepositError) as exc:
        service.refresh_urls(ALICE, response['deposit_id'], {'name': 'micro.h5ad'})
    assert exc.value.body['status'] == 'nothing_to_upload'
    service.withdraw(ALICE, response['deposit_id'])
    assert f'{CATALOG}{GROUP}/micro.h5ad' in env.s3.objects                    # never touched
    del env.s3.objects[f'{CATALOG}{GROUP}/micro.h5ad']
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, body)
    assert exc.value.body['status'] == 'h5ad_unavailable'


def test_a_changed_published_copy_fails_the_complete_and_withdraw_never_deletes_it(tmp_path):
    env, service, _ = published_by_alice(tmp_path)
    sidecar = sidecar_with(env, strain='x')
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.h5ad.mus', sidecar, 'mus')]}
    response = service.create(ALICE, body)
    upload(env.s3, {'files': [entry for entry in response['files'] if entry['method'] != 'reuse']},
           {'micro.h5ad.mus': sidecar})
    catalog = env.s3.objects[f'{CATALOG}{GROUP}/micro.h5ad']
    catalog['etag'] = 'changed'
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert exc.value.body['status'] == 'incomplete'
    service.withdraw(ALICE, response['deposit_id'])
    assert f'{CATALOG}{GROUP}/micro.h5ad' in env.s3.objects


def test_vocab_has_the_de_presets_with_their_required_keys(tmp_path):
    vocab = make_service(tmp_path, [time.time()]).vocab(ALICE)
    presets = vocab['de_presets']
    assert presets['deseq2']['required_keys'] == ['de_format', 'de_term', 'de_contrast']
    assert presets['long']['required_keys'] == ['de_format', 'de_method']
    assert 'gene' in presets['deseq2']['required_columns'] and vocab['metadata_update']


def test_auto_is_never_a_term_or_contrast():
    assert 'de_term= is missing or auto' in de_sidecar_problems({'de_format': 'deseq2', 'de_term': 'auto',
                                                                 'de_contrast': 'c'})[0]
    from pathlib import Path
    cmd = [str(part) for part in de_ingest_command(Path('x.duckdb'), Path('t.tsv'),
                                                    {'de_format': 'long', 'de_term': 'auto', 'de_contrast': 'AUTO',
                                                     'de_method': 'lmem'}, 't.tsv')]
    assert '--term' not in cmd and '--contrast' not in cmd and 'auto' not in [part.lower() for part in cmd]
