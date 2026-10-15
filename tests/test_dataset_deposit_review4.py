"""Fourth review of the deposit API: reused sidecar mismatch, published h5ad without sha256, shared prefixes."""
from __future__ import annotations

import pytest
import yaml

import config
import dataset_deposit as dd
from tests.deposit_helpers import ALICE, file_entry, upload
from tests.ingest_watch_helpers import CATALOG
from tests.test_dataset_deposit_metadata_update import published_by_alice
from tests.test_dataset_deposit_watch import GROUP, SEED_MD


def seed_only(env, service):
    seed = SEED_MD.replace('Microglia from a test.', 'Microglia, corrected.').encode()
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.md', seed, 'seed_md')]}
    response = service.create(ALICE, body)
    upload(env.s3, {'files': [entry for entry in response['files'] if entry['method'] != 'reuse']}, {'micro.md': seed})
    return response


def test_a_reused_sidecar_that_fails_the_check_is_a_clear_422_and_is_left_alone(tmp_path):
    env, service, _ = published_by_alice(tmp_path)
    key = f'{CATALOG}{GROUP}/micro.h5ad.mus'
    obj = env.s3.objects[key]
    text = [('sha256=' + '0' * 64) if line.startswith('sha256=') else line for line in obj['body'].decode().splitlines()]
    env.s3.put(key, ('\n'.join(text) + '\n').encode(), env.clock_value, metadata=obj.get('metadata'))
    response = seed_only(env, service)
    for _ in (1, 2):                                  # the same clear answer each time, nothing stuck halfway
        with pytest.raises(dd.DepositError) as exc:
            service.complete(ALICE, response['deposit_id'])
        assert exc.value.status == 422 and 'deposit a sidecar file' in exc.value.body['message']
    rows = {row['name']: row for row in service.store.files(response['deposit_id'])}
    assert rows['micro.h5ad.mus']['method'] == 'reuse' and rows['micro.h5ad.mus']['state'] == 'staged'
    assert key in env.s3.objects
    assert service.withdraw(ALICE, response['deposit_id'])['state'] == 'withdrawn'


def test_a_published_h5ad_without_sha_metadata_is_not_reused(tmp_path):
    env, service, _ = published_by_alice(tmp_path)
    key = f'{CATALOG}{GROUP}/micro.h5ad'
    obj = env.s3.objects[key]
    env.s3.put(key, obj['body'][:-1] + b'X', env.clock_value, metadata={})
    seed = SEED_MD.encode()
    calls_before = len(env.s3.calls)
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
                               'files': [file_entry('micro.md', seed, 'seed_md')]})
    assert exc.value.status == 409 and 'no recorded sha256' in exc.value.body['message']
    assert not [call for call in env.s3.calls[calls_before:] if call[0] in ('copy_object', 'copy')]


def test_the_api_and_the_watcher_share_the_prefixes(tmp_path, capsys):
    from tests.test_dataset_deposit_stack import S3, _secret
    from tests.test_deploy_config import _load_script, _site
    ant_stack = _load_script('ant_stack_prefix_test', 'bin/ops/ant-stack')
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3={**S3, 'prefix': 'h5adstore'})
    ant_stack.ensure_secrets(site)
    for name in ('s3_access_key_id', 's3_secret_access_key'):
        _secret(site, name, 'x')
    stack = ant_stack.render(site, quiet=True)
    for role_file in ('anthive-api.yaml', 'anthive-ingest.yaml'):
        rendered = yaml.safe_load((stack / role_file).read_text())
        assert rendered['ingest_watch'] == {'prefix': '__incoming/', 'catalog_prefix': 'h5adstore/'}
        watch = config.get_ingest_watch_settings(rendered)
        assert watch['prefix'] == '__incoming/' and watch['catalog_prefix'] == 'h5adstore/'
        assert config.get_dataset_deposit_settings(rendered)['catalog_prefix'] == 'h5adstore/'
    assert ant_stack._check_datasets(site) == []
    out = capsys.readouterr().out
    assert 'ANTHIVE_INGEST_WATCH_PREFIX=__incoming/' in out and 'ANTHIVE_INGEST_WATCH_CATALOG_PREFIX=h5adstore/' in out
    watcher = tmp_path / 'watcher.yaml'
    watcher.write_text('ingest_watch:\n  prefix: __incoming/\n  catalog_prefix: ""\n')
    other = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3={**S3, 'prefix': 'h5adstore'},
                  datasets={'watcher_config': str(watcher)})
    problems = ant_stack._check_datasets(other)
    assert any('watcher config' in problem for problem in problems)
    watcher.write_text('ingest_watch:\n  prefix: __incoming\n  catalog_prefix: h5adstore\n')
    assert ant_stack._check_datasets(other) == []
