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


# ── central watcher (design section 24) ─────────────────────────────────────

CENTRAL = {'ingestion_bucket': 'anthive-ingest', 'watcher_public_keys': ['ed25519:' + 'A' * 43 + '='], 'key_id': 'k2'}


def test_render_central_watcher_site(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3=S3,
                 datasets={'site_id': 'intern', 'central_watcher': CENTRAL})
    ant_stack.ensure_secrets(site)
    for name, value in (('s3_access_key_id', 'RW'), ('s3_secret_access_key', 'RWS'),
                        ('s3_ingest_access_key_id', 'ING'), ('s3_ingest_secret_access_key', 'INGS')):
        _secret(site, name, value)
    stack = ant_stack.render(site, quiet=True)
    api = yaml.safe_load((stack / 'anthive-api.yaml').read_text())
    assert api['datasets_deposit_central_watcher'] is True and api['datasets_deposit_site_key_id'] == 'k2'
    assert api['datasets_deposit_s3']['bucket'] == 'anthive-ingest'
    assert api['datasets_deposit_s3']['access_key_id'] == 'ING'          # never the main pair
    assert api['datasets_deposit_catalog_bucket'] == 'b'
    assert api['datasets_deposit_watcher_public_keys'] == CENTRAL['watcher_public_keys']
    serve = yaml.safe_load((stack / 'anthive.yaml').read_text())
    assert 'datasets_deposit_s3' not in serve


def test_the_site_secret_is_made_on_the_host_and_bound_into_the_api_only(tmp_path, ant_stack):
    """Review S3 and N1: the secret is created by ensure_secrets on the host (not in the api's tmpfs, where it
    changed at every restart), bound read-only into the api, and no other role gets the central keys."""
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True, 'mcp': True}, s3=S3,
                 datasets={'site_id': 'intern', 'central_watcher': CENTRAL})
    created = ant_stack.ensure_secrets(site)
    secret_file = site['_state'] / 'secrets' / 'deposit_site_secret'
    assert 'secrets/deposit_site_secret' in created and len(secret_file.read_text().strip()) >= 32
    assert oct(secret_file.stat().st_mode & 0o777) == '0o600'
    first = secret_file.read_text()
    ant_stack.ensure_secrets(site)
    assert secret_file.read_text() == first                              # stable across runs
    for name, value in (('s3_access_key_id', 'RW'), ('s3_secret_access_key', 'RWS'),
                        ('s3_ingest_access_key_id', 'ING'), ('s3_ingest_secret_access_key', 'INGS')):
        _secret(site, name, value)
    stack = ant_stack.render(site, quiet=True)
    compose = yaml.safe_load(next(path for path in stack.iterdir()
                                  if path.name.startswith(('docker-compose', 'compose'))).read_text())
    api = compose['services']['api']
    binds = {volume['target']: volume for volume in api['volumes'] if isinstance(volume, dict)}
    bound = binds['/var/lib/anthive/secrets/deposit_site_secret']
    assert bound['source'] == str(secret_file) and bound.get('read_only') is True
    for service in ('web', 'mcp', 'ingest'):
        targets = [volume.get('target', '') for volume in compose['services'].get(service, {}).get('volumes', [])
                   if isinstance(volume, dict)]
        assert not any(target.endswith('deposit_site_secret') for target in targets), service
    for name in ('anthive.yaml', 'anthive-ingest.yaml'):
        cfg = yaml.safe_load((stack / name).read_text())
        assert not any(key.startswith(('datasets_deposit_central', 'datasets_deposit_site_key',
                                       'datasets_deposit_watcher', 'datasets_deposit_catalog')) for key in cfg), name
    assert yaml.safe_load((stack / 'anthive-api.yaml').read_text())['datasets_deposit_central_watcher'] is True


def test_check_central_watcher_site(tmp_path, ant_stack, capsys):
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3=S3,
                 datasets={'site_id': 'intern', 'central_watcher': {**CENTRAL, 'watcher_public_keys': []}})
    problems = ant_stack._check_datasets(site)
    assert any('s3_ingest_access_key_id' in problem for problem in problems)
    assert any('watcher_public_keys is empty' in problem for problem in problems)
    assert any('deposit_site_secret is missing' in problem and 'intern.keys' in problem for problem in problems)
    assert 'ant-stack watch' in capsys.readouterr().out
    ant_stack.ensure_secrets(site)
    problems = ant_stack._check_datasets(site)
    assert not any('deposit_site_secret' in problem for problem in problems)
    assert 'intern.keys' in capsys.readouterr().out


def test_decision_days_is_rendered_validated_and_checked(tmp_path, ant_stack, capsys):
    site = _site(tmp_path, ant_stack, services={'dataset_deposit': True}, s3=S3,
                 datasets={'site_id': 'intern', 'central_watcher': CENTRAL, 'decision_days': 10})
    ant_stack.ensure_secrets(site)
    for name, value in (('s3_access_key_id', 'RW'), ('s3_secret_access_key', 'RWS'),
                        ('s3_ingest_access_key_id', 'ING'), ('s3_ingest_secret_access_key', 'INGS')):
        _secret(site, name, value)
    stack = ant_stack.render(site, quiet=True)
    assert yaml.safe_load((stack / 'anthive-api.yaml').read_text())['datasets_deposit_decision_days'] == 10
    capsys.readouterr()
    ant_stack._check_datasets(site)
    assert 'keeps its staged uploads 10 days' in capsys.readouterr().out
    for bad in (-1, 'soon', True):
        with pytest.raises(ant_stack.SiteError, match='decision_days'):
            _site(tmp_path, ant_stack, datasets={'decision_days': bad})


def test_central_watcher_validation(tmp_path, ant_stack):
    with pytest.raises(ant_stack.SiteError, match='watcher_public_keys'):
        _site(tmp_path, ant_stack, datasets={'central_watcher': {'ingestion_bucket': 'x', 'watcher_public_keys': ['abc']}})
