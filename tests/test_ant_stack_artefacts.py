"""ant-stack and the artefact store (doc/DESIGN_artefacts.md section 14, phase A4): the site.yaml
``artefacts:`` block, the watcher and presigning key for artefact-only sites, the read-only key pair, and
``check``.
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
S3_ON = {'enabled': True, 'endpoint': 's3.example.org', 'bucket': 'b', 'region': 'eu'}


@pytest.fixture(scope='module')
def ant_stack():
    pytest.importorskip('bcrypt')
    loader = importlib.machinery.SourceFileLoader('ant_stack_artefacts', str(REPO / 'bin/ops/ant-stack'))
    spec = importlib.util.spec_from_loader('ant_stack_artefacts', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write_site(tmp_path: Path, **overrides) -> Path:
    data = tmp_path / 'store'
    data.mkdir(exist_ok=True)
    doc = {
        'site': {'name': 'unit', 'public_url': 'https://anthive.example.org'},
        'data': {'h5adstore': str(data)},
        'state_dir': str(tmp_path / 'state'),
        'auth': {'mode': 'authelia', 'admins': ['boss'], 'users': [{'name': 'boss', 'groups': ['admins']}]},
    }
    for key, value in overrides.items():
        doc[key] = {**doc.get(key, {}), **value} if isinstance(value, dict) else value
    path = tmp_path / 'site.yaml'
    path.write_text(yaml.safe_dump(doc))
    return path


def _keys(state: Path, **names: str) -> None:
    folder = state / 'secrets'
    folder.mkdir(parents=True, exist_ok=True)
    for name, value in names.items():
        (folder / name).write_text(value + '\n')
        os.chmod(folder / name, 0o600)


def _site(ant_stack, tmp_path, read_pair=False, **overrides):
    site = ant_stack.load_site(_write_site(tmp_path, **overrides))
    ant_stack.ensure_secrets(site)
    _keys(site['_state'], s3_access_key_id='RW-KEY-ID', s3_secret_access_key='RW-SECRET')
    if read_pair:
        _keys(site['_state'], s3_artefacts_read_access_key_id='AR-KEY-ID',
              s3_artefacts_read_secret_access_key='AR-SECRET')
    return site


def _configs(ant_stack, site):
    stack = ant_stack.render(site, quiet=True)
    return stack, {role: yaml.safe_load((stack / name).read_text())
                   for role, name in ant_stack._config_roles(site).items()}


def test_an_artefact_only_site_gets_the_watcher_and_the_presigning_key(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, s3=S3_ON,
                 artefacts={'enabled': True, 'upload_groups': ['admins', 'analysts'], 'max_file_gb': 20,
                            'hold_public': True})
    stack, configs = _configs(ant_stack, site)
    assert set(configs) >= {'api', 'serve', 'ingest', 'watcher'}
    api = configs['api']
    assert api['artefacts_enabled'] is True and api['datasets_deposit_enabled'] is False
    assert api['artefacts_upload_groups'] == ['admins', 'analysts'] and api['artefacts_max_file_gb'] == 20
    assert api['datasets_deposit_s3']['access_key_id'] == 'RW-KEY-ID'
    assert configs['serve']['artefacts_enabled'] is True and 'datasets_deposit_s3' not in configs['serve']
    assert configs['watcher']['artefacts_enabled'] is True
    compose = yaml.safe_load((stack / 'compose.site.yaml').read_text())
    assert '127.0.0.1:8079:8079' in compose['services']['caddy']['ports']
    assert 'reverse_proxy' in (stack / 'caddy' / 'tools.caddy').read_text()
    assert 'bin/ingest/ant-ingest-watch' in ant_stack.watcher_command(site)
    assert ant_stack.cmd_config_path(argparse.Namespace(site=str(site['_path']), role='watcher')) == 0


def test_the_artefact_read_pair_reaches_only_the_api(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, read_pair=True, s3=S3_ON, artefacts={'enabled': True})
    stack, configs = _configs(ant_stack, site)
    assert configs['api']['artefacts_s3']['access_key_id'] == 'AR-KEY-ID'
    for role in ('serve', 'ingest', 'watcher'):
        assert 'AR-SECRET' not in (stack / ant_stack.CONFIG_ROLES[role]).read_text(), role


def test_without_artefacts_nothing_changes(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, s3=S3_ON)
    _stack, configs = _configs(ant_stack, site)
    assert 'watcher' not in configs
    assert configs['api']['artefacts_enabled'] is False and 'artefacts_s3' not in configs['api']
    assert 'datasets_deposit_s3' not in configs['api']


def test_artefacts_need_s3(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, artefacts={'enabled': True})
    _stack, configs = _configs(ant_stack, site)
    assert configs['api']['artefacts_enabled'] is False and 'watcher' not in configs
    problems = ant_stack._check_artefacts(site)
    assert any('s3.enabled is false' in problem for problem in problems)


@pytest.mark.parametrize('block,word', [
    ({'enabled': 'yes'}, 'enabled'),
    ({'enabled': True, 'upload_groups': 'analysts'}, 'upload_groups'),
    ({'max_file_gb': -1}, 'max_file_gb'),
    ({'max_versions': 2.5}, 'max_versions'),
    ({'serve_local': 'always'}, 'serve_local'),
    ({'max_size': 3}, 'unknown'),
])
def test_the_artefacts_block_is_validated(tmp_path, ant_stack, block, word):
    with pytest.raises(ant_stack.SiteError, match=word):
        ant_stack.load_site(_write_site(tmp_path, artefacts=block))


def test_check_names_a_half_read_pair_and_a_missing_gitignore(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path, s3=S3_ON, artefacts={'enabled': True})
    _keys(site['_state'], s3_artefacts_read_access_key_id='AR-KEY-ID')
    (site['_h5adstore'] / '.git').mkdir()
    problems = ant_stack._check_artefacts(site)
    assert any('only one of' in problem for problem in problems)
    output = capsys.readouterr()
    text = output.out + output.err
    assert 'static/artefacts/**/files/' in text
    (site['_h5adstore'] / '.gitignore').write_text('static/artefacts/**/files/\nstatic/artefacts/**/preview/\n')
    _keys(site['_state'], s3_artefacts_read_secret_access_key='AR-SECRET')
    assert ant_stack._check_artefacts(site) == []
    output = capsys.readouterr()
    assert '.gitignore' not in output.out + output.err
    assert 'read-only pair' in output.out + output.err


def test_check_warns_when_the_api_cannot_sign_artefact_links(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path, s3=S3_ON, artefacts={'enabled': True})
    assert ant_stack._check_artefacts(site) == []
    output = capsys.readouterr()
    assert 'only files with a local copy' in output.out + output.err


def test_the_caddy_log_filter_drops_signed_query_strings():
    caddyfile = (REPO / 'config' / 'Caddyfile').read_text()
    assert 'resp_headers>Location regexp \\?.*$ ?REDACTED' in caddyfile
