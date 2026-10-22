"""Deployment configuration: lib/config path helpers and switches, the
coordinate mode in AnnotationWriter's ant-serve calls, ant-convert's staged
install, bin/ops/ant-stack rendering, and the image's uv shim.

Runs without docker: ant-stack is exercised through its render functions.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import config

REPO = Path(__file__).resolve().parent.parent


def _load_script(name: str, rel: str):
    loader = importlib.machinery.SourceFileLoader(name, str(REPO / rel))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith('ANTHIVE_'):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


# ── lib/config: ANTHIVE_CONFIG and path helpers ──────────────────────────────

def test_anthive_config_env_points_at_external_file(clean_env, tmp_path):
    cfg_file = tmp_path / 'site-anthive.yaml'
    cfg_file.write_text('rest_api_port: 9123\nstate_dir: /srv/x/state\n')
    clean_env.setenv('ANTHIVE_CONFIG', str(cfg_file))
    cfg = config.load_config()
    assert cfg['rest_api_port'] == 9123
    assert config.get_state_dir(cfg, create=False) == Path('/srv/x/state')


def test_unknown_key_in_external_config_still_rejected(clean_env, tmp_path):
    """Host tools stay strict; 2.46.1: a server (set_unknown_keys_lenient) only warns."""
    cfg_file = tmp_path / 'bad.yaml'
    cfg_file.write_text('not_a_real_key: 1\n')
    clean_env.setenv('ANTHIVE_CONFIG', str(cfg_file))
    clean_env.setitem(config._UNKNOWN_KEYS_LENIENT, 'on', False)
    with pytest.raises(config.ConfigError):
        config.load_config()
    clean_env.setitem(config._UNKNOWN_KEYS_LENIENT, 'on', True)
    assert 'not_a_real_key' in config.load_config()


def test_defaults_keep_the_historical_layout(clean_env):
    cfg = config.load_default_config()
    root = config.get_anthive_root()
    assert config.get_state_dir(cfg, create=False) == root / 'var'
    assert config.get_tmp_dir(cfg, create=False) == root / 'tmp'
    assert config.get_data_folder(cfg) == root / 'data' / 'h5adstore'
    assert config.get_webapp_dir(cfg) == root / 'webapp'
    assert config.get_duckdb_temp_dir(cfg) == root / 'tmp' / 'duckdb_spill'
    assert config.get_convert_staging_dir(cfg) is None
    assert config.get_api_url(cfg) == 'http://localhost:8080'
    assert config.get_coordinate_mode(cfg) == 'auto'
    assert config.get_catalog_scan_mode(cfg) == 'auto'
    assert config.get_auth_settings(cfg) == {'mode': None, 'admins': set()}


def test_env_beats_config_beats_default(clean_env):
    cfg = {'tmp_dir': '/from/config', 'api_url': 'http://cfg:1'}
    assert config.get_tmp_dir(cfg, create=False) == Path('/from/config')
    clean_env.setenv('ANTHIVE_TMP_DIR', '/from/env')
    clean_env.setenv('ANTHIVE_API_URL', 'http://env:2/')
    assert config.get_tmp_dir(cfg, create=False) == Path('/from/env')
    assert config.get_api_url(cfg) == 'http://env:2'


def test_relative_paths_anchor_at_repo_root(clean_env):
    assert config.get_state_dir({'state_dir': 'somewhere/state'}, create=False) == \
        config.get_anthive_root() / 'somewhere' / 'state'


def test_expand_value_defaulted_and_uid(clean_env):
    clean_env.delenv('TMPDIR', raising=False)
    assert config.expand_value('${TMPDIR:-/tmp}/x') == '/tmp/x'
    clean_env.setenv('TMPDIR', '/node/local')
    assert config.expand_value('${TMPDIR:-/tmp}/x') == '/node/local/x'
    clean_env.delenv('UID', raising=False)
    assert config.expand_value('/t/anthive-${UID}') == f'/t/anthive-{os.getuid()}'
    assert config.expand_value('$TMPDIR/y') == '/node/local/y'


def test_convert_staging_dir_follows_job_tmpdir(clean_env):
    clean_env.setenv('ANTHIVE_CONVERT_STAGING_DIR', '${TMPDIR:-/tmp}')
    clean_env.setenv('TMPDIR', '/job/123')
    assert config.get_convert_staging_dir({}) == Path('/job/123')


@pytest.mark.parametrize('key,getter,bad', [
    ('coordinate', config.get_coordinate_mode, 'sometimes'),
    ('catalog_scan', config.get_catalog_scan_mode, 'cloud'),
])
def test_invalid_switch_values_raise(clean_env, key, getter, bad):
    with pytest.raises(config.ConfigError):
        getter({key: bad})


def test_auth_settings(clean_env):
    settings = config.get_auth_settings({'auth': {'mode': 'basic', 'admins': ['a', ' b ']}})
    assert settings == {'mode': 'basic', 'admins': {'a', 'b'}}
    with pytest.raises(config.ConfigError):
        config.get_auth_settings({'auth': {'mode': 'ldap'}})
    clean_env.setenv('ANTHIVE_AUTH_MODE', 'none')
    assert config.get_auth_settings({'auth': {'mode': 'basic'}})['mode'] == 'none'


# ── AnnotationWriter's ant-serve calls honour `coordinate` ───────────────────

UNREACHABLE = 'http://127.0.0.1:9'   # discard port: connection refused


def test_coordinate_auto_tolerates_missing_server(clean_env, capsys):
    import anthive_db
    clean_env.setenv('ANTHIVE_COORDINATE', 'auto')
    assert anthive_db._post_ant_serve(UNREACHABLE, '/admin/release/x', timeout=2) is None
    assert 'continuing without coordination' in capsys.readouterr().out


def test_coordinate_always_raises_on_missing_server(clean_env):
    import anthive_db
    clean_env.setenv('ANTHIVE_COORDINATE', 'always')
    with pytest.raises(anthive_db.AnnotationWriterError):
        anthive_db._post_ant_serve(UNREACHABLE, '/admin/restore/x', timeout=2, on_failure='warn')


def test_coordinate_never_skips_the_call(clean_env):
    import anthive_db
    clean_env.setenv('ANTHIVE_COORDINATE', 'never')
    assert anthive_db._post_ant_serve(None, '/admin/release/x', on_failure='raise') is None


# ── ant-convert staged install ──────────────────────────────────────────────

@pytest.fixture(scope='module')
def ant_convert():
    pytest.importorskip('anndata')
    return _load_script('ant_convert_under_test', 'bin/ingest/ant-convert')


def test_staged_install_replaces_target(ant_convert, tmp_path):
    staged = tmp_path / 'stage' / 'x.duckdb.1.building'
    staged.parent.mkdir()
    staged.write_bytes(b'new')
    target = tmp_path / 'x.duckdb'
    target.write_bytes(b'old')
    ant_convert._install_staged_duckdb(staged, target)
    assert target.read_bytes() == b'new' and not staged.exists()


def test_staged_install_cross_device_copies_then_renames(ant_convert, tmp_path, monkeypatch):
    staged = tmp_path / 'stage.building'
    staged.write_bytes(b'payload')
    target = tmp_path / 'x.duckdb'
    real_replace = os.replace
    calls = []

    def fake_replace(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        if Path(src) == staged:
            raise OSError(18, 'Invalid cross-device link')
        return real_replace(src, dst)

    monkeypatch.setattr(ant_convert.os, 'replace', fake_replace)
    ant_convert._install_staged_duckdb(staged, target)
    assert target.read_bytes() == b'payload'
    assert not staged.exists()
    assert not list(tmp_path.glob('.x.duckdb.*.incoming'))
    assert calls[-1][1] == 'x.duckdb' and calls[-1][0].endswith('.incoming')


def test_staged_install_refuses_leftover_wal(ant_convert, tmp_path):
    staged = tmp_path / 's.building'
    staged.write_bytes(b'x')
    Path(str(staged) + '.wal').write_bytes(b'w')
    with pytest.raises(ant_convert.ConversionError):
        ant_convert._install_staged_duckdb(staged, tmp_path / 't.duckdb')


# ── bin/ops/ant-stack rendering ─────────────────────────────────────────────

@pytest.fixture(scope='module')
def ant_stack():
    pytest.importorskip('bcrypt')
    return _load_script('ant_stack_under_test', 'bin/ops/ant-stack')


def _site(tmp_path, ant_stack, **overrides):
    data = tmp_path / 'store'
    data.mkdir(exist_ok=True)
    doc = {
        'site': {'name': 'unit', 'public_url': 'https://anthive.example.org:8443', 'hostnames': ['alt.lan']},
        'data': {'h5adstore': str(data)},
        'state_dir': str(tmp_path / 'state'),
        'auth': {'mode': 'basic', 'admins': ['boss'],
                 'users': [{'name': 'boss', 'groups': ['admins']}, {'name': 'reader'}]},
    }
    for key, value in overrides.items():
        doc[key] = {**doc.get(key, {}), **value} if isinstance(value, dict) else value
    path = tmp_path / 'site.yaml'
    path.write_text(yaml.safe_dump(doc))
    return ant_stack.load_site(path)


def test_render_basic_mode(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack)
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    auth = (stack / 'caddy' / 'auth.caddy').read_text()
    assert 'basic_auth @anthive_protected' in auth
    assert 'boss $2' in auth and 'reader $2' in auth          # bcrypt hashes, no plaintext
    assert '\tboss admins' in auth and '\treader admins' not in auth
    assert 'request_header Remote-User {http.auth.user.id}' in auth
    rendered = yaml.safe_load((stack / 'anthive.yaml').read_text())
    assert rendered['auth'] == {'mode': 'basic', 'admins': ['boss']}
    assert rendered['state_dir'] == '/var/lib/anthive'
    assert rendered['h5adstore'] == str(tmp_path / 'store')
    assert oct((stack / 'anthive.yaml').stat().st_mode & 0o777) == '0o600'
    env = (stack / '.env').read_text()
    assert 'COMPOSE_PROFILES="mcp"' in env
    assert 'https://anthive.example.org:8443, https://alt.lan:8443' in env
    compose_site = yaml.safe_load((stack / 'compose.site.yaml').read_text())
    api_mounts = {m['target']: m for m in compose_site['services']['api']['volumes']}
    assert api_mounts[str(tmp_path / 'store')]['source'] == str(tmp_path / 'store')   # same-path
    web_mounts = {m['target']: m for m in compose_site['services']['web']['volumes']}
    assert web_mounts[str(tmp_path / 'store')].get('read_only') is True
    assert all(m['target'] != '/var/lib/anthive' for m in compose_site['services']['web']['volumes'])
    secrets_dir = tmp_path / 'state' / 'secrets'
    for name in ('internal_secret', 'mcp_token_secret', 'users.yaml'):
        assert oct((secrets_dir / name).stat().st_mode & 0o777) == '0o600'


def test_render_authelia_mode(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack, auth={'mode': 'authelia'})
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    assert 'caddy-auth.dev' in (stack / 'caddy' / 'auth.caddy').read_text()
    assert 'COMPOSE_PROFILES="mcp,authelia"' in (stack / '.env').read_text()
    authelia = yaml.safe_load((tmp_path / 'state' / 'authelia' / 'configuration.yml').read_text())
    cookie = authelia['session']['cookies'][0]
    assert cookie['domain'] == 'anthive.example.org'
    assert cookie['authelia_url'] == 'https://anthive.example.org:8443/authelia/'
    users = yaml.safe_load((tmp_path / 'state' / 'authelia' / 'users_database.yml').read_text())
    assert users['users']['boss']['groups'] == ['admins']
    assert users['users']['boss']['password'].startswith('$2')


def test_render_none_mode_strips_identity(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack, auth={'mode': 'none'})
    stack = ant_stack.render(site, quiet=True)
    assert 'caddy-auth.none' in (stack / 'caddy' / 'auth.caddy').read_text()
    none_snippet = (REPO / 'config' / 'caddy-auth.none').read_text()
    for header in ('Remote-User', 'Remote-Groups', 'Remote-Email', 'Remote-Name'):
        assert f'request_header -{header}' in none_snippet


def test_tls_off_written_bare_in_yaml(tmp_path, ant_stack):
    path = tmp_path / 'site.yaml'
    data = tmp_path / 'store'
    data.mkdir()
    path.write_text(f"""
site: {{name: unit, public_url: 'https://x.example.org'}}
data: {{h5adstore: {data}}}
state_dir: {tmp_path / 'state'}
auth: {{mode: trusted-proxy}}
tls:
  mode: off
ports: {{bind: 127.0.0.1, http: 18500}}
""")
    site = ant_stack.load_site(path)
    assert site['tls']['mode'] == 'off'
    listen, ports, auto = ant_stack._listen_and_ports(site)
    assert listen == 'http://:8500' and ports == ['127.0.0.1:18500:8500'] and auto == 'auto_https off'


@pytest.mark.parametrize('overrides,message', [
    ({'auth': {'mode': 'ldap'}}, 'auth.mode'),
    ({'site': {'name': 'Bad_Name'}}, 'site.name'),
    ({'data': {'h5adstore': 'relative/path'}}, 'absolute'),
    ({'auth': {'mode': 'authelia'}, 'site': {'public_url': 'http://plain.example.org'}}, 'https'),
    ({'tls': {'mode': 'acme'}}, 'acme_email'),
])
def test_site_validation_errors(tmp_path, ant_stack, overrides, message):
    with pytest.raises(ant_stack.SiteError, match=message):
        _site(tmp_path, ant_stack, **overrides)


def test_trusted_proxy_must_bind_loopback(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack, auth={'mode': 'trusted-proxy'}, ports={'bind': '0.0.0.0'})
    problems = ant_stack.check(site, running=True)
    assert any('trusted-proxy' in problem for problem in problems)


def test_rendered_config_is_validated(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack, anthive={'no_such_key': 1})
    ant_stack.ensure_secrets(site)
    with pytest.raises(ant_stack.SiteError, match='invalid'):
        ant_stack.render(site, quiet=True)


def test_site_example_is_valid(tmp_path, ant_stack):
    """The shipped template must load (with paths pointed somewhere real)."""
    doc = yaml.safe_load((REPO / 'site.example.yaml').read_text())
    doc['data']['h5adstore'] = str(tmp_path)
    doc['state_dir'] = str(tmp_path / 'state')
    path = tmp_path / 'site.yaml'
    path.write_text(yaml.safe_dump(doc))
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    ant_stack.render(site, quiet=True)


# ── docker/uv-shim ──────────────────────────────────────────────────────────

def _shim(*args, python=sys.executable):
    return subprocess.run(['sh', str(REPO / 'docker' / 'uv-shim'), *args],
                          env={**os.environ, 'ANTHIVE_VENV_PYTHON': python},
                          capture_output=True, text=True)


def test_uv_shim_runs_scripts_with_the_fixed_python(tmp_path):
    script = tmp_path / 'hello'
    script.write_text('import sys; print("ran", sys.argv[1:])\n')
    result = _shim('run', '--quiet', '--script', str(script), 'a', 'b')
    assert result.returncode == 0 and "ran ['a', 'b']" in result.stdout


@pytest.mark.parametrize('args', [('pip', 'install', 'x'), ('run', '--with', 'x', 'y'), ('run',)])
def test_uv_shim_refuses_everything_else(args):
    assert _shim(*args).returncode == 2


def test_docs_base_url_default_env_and_config(clean_env):
    default = 'https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/blob/main/doc'
    assert config.get_docs_base_url(config.load_default_config()) == default
    assert config.get_docs_base_url({'docs_base_url': ''}) == default      # empty = default
    assert config.get_docs_base_url({'docs_base_url': 'https://x.org/doc/'}) == 'https://x.org/doc'
    clean_env.setenv('ANTHIVE_DOCS_BASE_URL', 'https://env.org/doc')
    assert config.get_docs_base_url({'docs_base_url': 'https://x.org/doc'}) == 'https://env.org/doc'


def test_render_docs_base_url_only_when_set(tmp_path, ant_stack):
    site = _site(tmp_path, ant_stack)
    ant_stack.ensure_secrets(site)
    rendered = yaml.safe_load((ant_stack.render(site, quiet=True) / 'anthive.yaml').read_text())
    assert 'docs_base_url' not in rendered
    site = _site(tmp_path, ant_stack, site={'docs_base_url': 'https://fork.example/doc'})
    rendered = yaml.safe_load((ant_stack.render(site, quiet=True) / 'anthive.yaml').read_text())
    assert rendered['docs_base_url'] == 'https://fork.example/doc'


def test_caddy_and_authelia_images_are_pinned_by_digest_everywhere():
    """2.46.5 scratch-site check: a moving tag made `ant-stack pull` report "updated" without a release."""
    import re
    images = yaml.safe_load((REPO / 'site.example.yaml').read_text())['images']
    compose = (REPO / 'docker' / 'compose.yaml').read_text()
    for which in ('caddy', 'authelia'):
        ref = images[which]
        assert re.fullmatch(r'[a-z0-9./-]+:[0-9][0-9A-Za-z.-]*@sha256:[0-9a-f]{64}', ref), ref
        assert ref in compose
    assert f"ARG CADDY_IMAGE={images['caddy']}" in (REPO / 'docker' / 'Dockerfile').read_text()
