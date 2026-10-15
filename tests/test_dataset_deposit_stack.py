"""ant-stack and config for dataset deposits: rendering, the presigning key pair, `check`."""
from __future__ import annotations

import os

import pytest
import yaml

import config
from tests.test_deploy_config import _site


@pytest.fixture(scope='module')
def ant_stack():
    pytest.importorskip('bcrypt')
    from tests.test_deploy_config import _load_script
    return _load_script('ant_stack_deposit_test', 'bin/ops/ant-stack')


def _secret(site, name, value):
    path = site['_state'] / 'secrets' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    os.chmod(path, 0o600)


S3 = {'enabled': True, 'endpoint': 's3.example.org', 'region': 'eu', 'bucket': 'b'}


def test_render_off_by_default(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack)
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    api = yaml.safe_load((stack / 'anthive-api.yaml').read_text())
    assert api['datasets_deposit_enabled'] is False and 'datasets_deposit_s3' not in api
    assert api['datasets_upload_groups'] == ['admins', 'curators']


def test_render_on_with_the_incoming_pair_only_in_the_api(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3=S3,
                 datasets={'upload_groups': ['curators'], 'max_gb': 50, 'update_policy': 'any'})
    ant_stack.ensure_secrets(site)
    for name, value in (('s3_access_key_id', 'RW'), ('s3_secret_access_key', 'RWS'),
                        ('s3_incoming_access_key_id', 'IN'), ('s3_incoming_secret_access_key', 'INS')):
        _secret(site, name, value)
    stack = ant_stack.render(site, quiet=True)
    api = yaml.safe_load((stack / 'anthive-api.yaml').read_text())
    serve = yaml.safe_load((stack / 'anthive.yaml').read_text())
    assert api['datasets_deposit_enabled'] is True and api['datasets_deposit_max_gb'] == 50
    assert api['datasets_deposit_s3']['access_key_id'] == 'IN' and api['datasets_deposit_s3']['bucket'] == 'b'
    assert 'datasets_deposit_s3' not in serve and serve['datasets_upload_groups'] == ['curators']
    assert 'IN' not in (stack / 'anthive.yaml').read_text().split()
    settings = config.get_dataset_deposit_settings(api)
    assert settings['s3']['access_key_id'] == 'IN' and settings['credential_source'] == 'datasets_deposit_s3'
    assert settings['update_policy'] == 'any' and settings['upload_groups'] == ['curators']


def test_check_lines(tmp_path, ant_stack, capsys):
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3={'enabled': False})
    assert any('s3.enabled is false' in problem for problem in ant_stack._check_datasets(site))
    (tmp_path / 'b').mkdir()
    (tmp_path / 'c').mkdir()
    site = _site(tmp_path / 'b', ant_stack, services={'dataset_deposit': True}, s3=S3)
    assert any('neither' in problem for problem in ant_stack._check_datasets(site))
    for name, value in (('s3_access_key_id', 'RW'), ('s3_secret_access_key', 'RWS')):
        _secret(site, name, value)
    assert ant_stack._check_datasets(site) == []
    captured = capsys.readouterr()
    assert 'read-write S3 keys' in captured.err and 'ANTHIVE_INGEST_STATE_DIR=' in captured.out
    off = _site(tmp_path / 'c', ant_stack)
    assert ant_stack._check_datasets(off) == []
    assert 'dataset deposits: off' in capsys.readouterr().out


def test_bad_datasets_block_is_refused(tmp_path, ant_stack):
    with pytest.raises(ant_stack.SiteError, match='upload_groups'):
        _site(tmp_path, ant_stack, datasets={'upload_groups': 'curators'})


def test_config_defaults_and_validation():
    settings = config.get_dataset_deposit_settings({})
    assert settings['upload_groups'] == ['admins', 'curators'] and settings['enabled'] is False
    assert settings['max_gb'] == 200.0 and settings['url_ttl_hours'] == 24.0 and settings['max_open_per_user'] == 5
    assert settings['prefix'] == '__incoming/'
    with pytest.raises(config.ConfigError):
        config.get_dataset_deposit_settings({'datasets_deposit_update_policy': 'everyone'})
    with pytest.raises(config.ConfigError):
        config.get_dataset_deposit_settings({'datasets_deposit_max_gb': 'lots'})
    watch = config.get_dataset_deposit_settings({'ingest_watch': {'prefix': '__incoming_test'}})
    assert watch['prefix'] == '__incoming_test/'
