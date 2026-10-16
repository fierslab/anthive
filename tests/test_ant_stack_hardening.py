"""Tests for the 2.41.0 ant-stack hardening (bin/ops/ant-stack).

Covers: S3 keys and other secrets reaching only the containers that use
them, the secret-permission warnings of `check`, Caddy's trusted proxies
and the forwarded scheme towards Authelia (tls off behind a proxy), `pull`
using the refs site.yaml names now, and the host-side `publish-library`.
2.45.1: ANTHIVE_CONFIG_SHA recreates api/web/mcp on a config change,
`restart` with several services, `passwd` without a terminal, the
internal-dataset line and listener labels of `check`, log_stderr_level.

Runs without docker. The Caddy tests need a `caddy` binary on PATH (or
envs/caddy/caddy) and are skipped without one.
"""
from __future__ import annotations

import argparse
import fcntl
import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def ant_stack():
    pytest.importorskip('bcrypt')
    loader = importlib.machinery.SourceFileLoader('ant_stack_hardening', str(REPO / 'bin/ops/ant-stack'))
    spec = importlib.util.spec_from_loader('ant_stack_hardening', loader)
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
        'auth': {'mode': 'authelia', 'admins': ['boss'],
                 'users': [{'name': 'boss', 'groups': ['admins']}]},
    }
    for key, value in overrides.items():
        doc[key] = {**doc.get(key, {}), **value} if isinstance(value, dict) else value
    path = tmp_path / 'site.yaml'
    path.write_text(yaml.safe_dump(doc))
    return path


def _site(ant_stack, tmp_path, **overrides):
    return ant_stack.load_site(_write_site(tmp_path, **overrides))


def _put_s3_keys(state: Path, *, read: bool = False) -> None:
    secrets_dir = state / 'secrets'
    secrets_dir.mkdir(parents=True, exist_ok=True)
    keys = {'s3_access_key_id': 'RW-KEY-ID', 's3_secret_access_key': 'RW-SECRET'}
    if read:
        keys.update({'s3_read_access_key_id': 'RO-KEY-ID', 's3_read_secret_access_key': 'RO-SECRET'})
    for name, value in keys.items():
        (secrets_dir / name).write_text(value + '\n')
        os.chmod(secrets_dir / name, 0o600)


S3_ON = {'enabled': True, 'endpoint': 's3.example.org', 'bucket': 'b', 'region': 'eu'}


def _rendered(ant_stack, site) -> dict[str, str]:
    stack = ant_stack.render(site, quiet=True)
    return {role: (stack / name).read_text() for role, name in ant_stack._config_roles(site).items()}


# ── 1. secrets only where they are used ─────────────────────────────────────

def test_s3_keys_only_in_ingest_config_by_default(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, s3=S3_ON)
    ant_stack.ensure_secrets(site)
    _put_s3_keys(site['_state'])
    texts = _rendered(ant_stack, site)
    for role in ('serve', 'api'):
        assert 'RW-SECRET' not in texts[role] and 'RW-KEY-ID' not in texts[role]
        assert 's3' not in yaml.safe_load(texts[role])
    ingest = yaml.safe_load(texts['ingest'])
    assert ingest['s3']['access_key_id'] == 'RW-KEY-ID'
    assert ingest['s3']['secret_access_key'] == 'RW-SECRET'
    for name in ant_stack._config_roles(site).values():
        assert oct((site['_state'] / 'stack' / name).stat().st_mode & 0o777) == '0o600'
    assert oct((site['_state'] / 'stack').stat().st_mode & 0o777) == '0o700'
    assert not (site['_state'] / 'stack' / 'anthive-watcher.yaml').exists()     # only with deposits on


def test_api_read_access_uses_the_read_only_keys(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, s3={**S3_ON, 'api_access': 'read'})
    ant_stack.ensure_secrets(site)
    _put_s3_keys(site['_state'], read=True)
    texts = _rendered(ant_stack, site)
    api = yaml.safe_load(texts['api'])
    assert api['s3']['access_key_id'] == 'RO-KEY-ID' and api['s3']['bucket'] == 'b'
    assert 'RW-SECRET' not in texts['api']
    assert 'RO-SECRET' not in texts['serve'] and 'RW-SECRET' not in texts['serve']
    assert yaml.safe_load(texts['ingest'])['s3']['secret_access_key'] == 'RW-SECRET'


def test_api_write_access_gets_the_read_write_keys(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, s3={**S3_ON, 'api_access': 'write'})
    ant_stack.ensure_secrets(site)
    _put_s3_keys(site['_state'])
    texts = _rendered(ant_stack, site)
    assert yaml.safe_load(texts['api'])['s3']['secret_access_key'] == 'RW-SECRET'
    assert 'RW-SECRET' not in texts['serve']


def test_chat_key_only_in_api_config(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, services={'chat': True})
    ant_stack.ensure_secrets(site)
    (site['_state'] / 'secrets' / 'chat_api_key').write_text('sk-CHAT\n')
    texts = _rendered(ant_stack, site)
    assert 'sk-CHAT' in texts['api']
    assert 'sk-CHAT' not in texts['serve'] and 'sk-CHAT' not in texts['ingest']
    assert yaml.safe_load(texts['serve'])['chat']['enabled'] is True   # UI still knows it is on


def test_invalid_api_access_rejected(tmp_path, ant_stack):
    with pytest.raises(ant_stack.SiteError, match='api_access'):
        _site(ant_stack, tmp_path, s3={'api_access': 'admin'})


def test_containers_mount_only_the_secrets_they_need(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, s3=S3_ON)
    ant_stack.ensure_secrets(site)
    _put_s3_keys(site['_state'])
    stack = ant_stack.render(site, quiet=True)
    services = yaml.safe_load((stack / 'compose.site.yaml').read_text())['services']
    state = site['_state']
    c_state = ant_stack.C_STATE

    def sources(service):
        return {m['source']: m for m in services[service]['volumes'] if m['type'] == 'bind'}

    def config_source(service):
        return next(m['source'] for m in services[service]['volumes'] if m['target'] == ant_stack.C_CONFIG)

    # api: state_dir for caches and MCP links, with secrets/, authelia/ and
    # stack/ hidden; only its two secrets come back, read-only
    api = sources('api')
    assert str(state) in api
    secret_binds = {s for s in api if s.startswith(str(state / 'secrets'))}
    assert secret_binds == {str(state / 'secrets' / 'internal_secret'),
                            str(state / 'secrets' / 'mcp_token_secret')}
    assert all(api[s]['read_only'] for s in secret_binds)
    hidden = {entry.split(':', 1)[0] for entry in services['api']['tmpfs']}
    assert hidden == {f"{c_state}/secrets", f"{c_state}/authelia", f"{c_state}/stack"}
    assert all(f"uid={site['_uid']}" in entry for entry in services['api']['tmpfs'])
    assert config_source('api') == str(stack / 'anthive-api.yaml')

    # mcp: no state_dir at all, only the internal secret
    mcp = sources('mcp')
    assert str(state) not in mcp
    assert {s for s in mcp if s.startswith(str(state))} == {str(state / 'secrets' / 'internal_secret'),
                                                       str(stack / 'anthive.yaml')}
    assert config_source('mcp') == str(stack / 'anthive.yaml')

    # web: nothing from state_dir
    assert [s for s in sources('web') if s.startswith(str(state))] == [str(stack / 'anthive.yaml')]
    assert config_source('web') == str(stack / 'anthive.yaml')

    # ingest: the operator's tool, full keys
    assert config_source('ingest') == str(stack / 'anthive-ingest.yaml')
    assert str(state) in sources('ingest')


# ── check: permissions and S3 key files ─────────────────────────────────────

def test_check_warns_on_readable_secrets(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path, s3=S3_ON)
    ant_stack.ensure_secrets(site)
    _put_s3_keys(site['_state'])
    ant_stack.render(site, quiet=True)
    assert ant_stack._check_secret_permissions(site) == []
    leaky = site['_state'] / 'secrets' / 's3_secret_access_key'
    os.chmod(leaky, 0o644)
    config = site['_state'] / 'stack' / 'anthive-ingest.yaml'
    os.chmod(config, 0o640)
    loose = ant_stack._check_secret_permissions(site)
    assert str(leaky) in loose and str(config) in loose
    assert 'chmod 600' in capsys.readouterr().err


def test_check_warns_on_readable_deploy_key(tmp_path, ant_stack):
    key = tmp_path / 'deploy_key'
    key.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\n')
    os.chmod(key, 0o644)
    site = _site(ant_stack, tmp_path, library={'deploy_key': str(key)})
    ant_stack.ensure_secrets(site)
    assert str(key) in ant_stack._check_secret_permissions(site)


def test_check_s3_requires_read_keys_for_read_access(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, s3={**S3_ON, 'api_access': 'read'})
    ant_stack.ensure_secrets(site)
    _put_s3_keys(site['_state'])              # no read pair
    problems = ant_stack._check_s3(site)
    assert any('s3_read_access_key_id' in p for p in problems)
    _put_s3_keys(site['_state'], read=True)
    assert ant_stack._check_s3(site) == []


def test_autopush_defaults_off(tmp_path, ant_stack):
    import config  # noqa: PLC0415
    assert config.get_library_upload_settings({})['s3_autopush'] is False
    site = _site(ant_stack, tmp_path)
    assert ant_stack.render_anthive_yaml(site, 'api')['library_s3_autopush'] is False
    example = yaml.safe_load((REPO / 'site.example.yaml').read_text())
    assert example['library']['s3_autopush'] is False
    assert example['s3']['api_access'] == 'none'


# ── 3. trusted proxies and the forwarded scheme ─────────────────────────────

def test_trusted_proxies_default_follows_tls_mode(tmp_path, ant_stack):
    off = _site(ant_stack, tmp_path, tls={'mode': 'off'}, ports={'bind': '127.0.0.1'})
    assert off['_trusted_proxies'] == ['private_ranges']
    snippet = ant_stack.render_caddy_servers(off)
    assert 'trusted_proxies static private_ranges' in snippet
    internal = _site(ant_stack, tmp_path)
    assert internal['_trusted_proxies'] == []
    assert 'trusted_proxies' not in ant_stack.render_caddy_servers(internal).replace('# Rendered by bin/ops/ant-stack from tls.trusted_proxies', '')
    custom = _site(ant_stack, tmp_path, tls={'mode': 'off', 'trusted_proxies': ['172.17.0.1/32']})
    assert 'trusted_proxies static 172.17.0.1/32' in ant_stack.render_caddy_servers(custom)
    none = _site(ant_stack, tmp_path, tls={'mode': 'off', 'trusted_proxies': []})
    assert none['_trusted_proxies'] == []


def test_trusted_proxies_rejects_garbage(tmp_path, ant_stack):
    with pytest.raises(ant_stack.SiteError, match='trusted_proxies'):
        _site(ant_stack, tmp_path, tls={'mode': 'off', 'trusted_proxies': ['the-proxy']})


def test_servers_snippet_rendered_and_wired(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, tls={'mode': 'off'})
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    assert (stack / 'caddy' / 'servers.caddy').is_file()
    compose = yaml.safe_load((REPO / 'docker' / 'compose.yaml').read_text())
    assert compose['services']['caddy']['environment']['ANTHIVE_SERVERS_SNIPPET'] == \
        f"{ant_stack.C_CADDY_SNIPPETS}/servers.caddy"
    assert 'import {$ANTHIVE_SERVERS_SNIPPET:caddy-servers.dev}' in (REPO / 'config' / 'Caddyfile').read_text()
    assert (REPO / 'config' / 'caddy-servers.dev').is_file()
    assert '{http.request.scheme}://' not in (REPO / 'config' / 'caddy-auth.dev').read_text()


def _caddy() -> str | None:
    local = REPO / 'envs' / 'caddy' / 'caddy'
    return str(local) if local.is_file() else shutil.which('caddy')


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _caddy_env(ant_stack, site, stack: Path, port: int, authelia_port: int, upstream_port: int) -> dict:
    """The env compose gives the caddy container, pointed at host paths."""
    env = dict(os.environ)
    listen, _ports, auto_https = ant_stack._listen_and_ports(site)
    env.update({
        'ANTHIVE_LISTEN': listen.replace(str(ant_stack.PORT_PLAIN), str(port)),
        'ANTHIVE_CADDY_BIND': '127.0.0.1',
        'ANTHIVE_TLS_SNIPPET': str(stack / 'caddy' / 'tls.caddy'),
        'ANTHIVE_AUTH_SNIPPET': str(stack / 'caddy' / 'auth.caddy'),
        'ANTHIVE_SERVERS_SNIPPET': str(stack / 'caddy' / 'servers.caddy'),
        'ANTHIVE_HTTP_REDIRECT_SNIPPET': str(REPO / 'config' / 'caddy-http-redirect.prod'),
        'ANTHIVE_AUTO_HTTPS': auto_https,
        'ANTHIVE_DEFAULT_SNI': '',
        'ANTHIVE_WEBAPP_ROOT': str(stack),
        'ANTHIVE_CADDY_LOG_OUTPUT': 'discard',
        'ANTHIVE_AUTHELIA_HOST': '127.0.0.1', 'ANTHIVE_AUTHELIA_PORT': str(authelia_port),
        'ANTHIVE_API_HOST': '127.0.0.1', 'ANTHIVE_API_PORT': str(upstream_port),
        'ANTHIVE_BROWSE_HOST': '127.0.0.1', 'ANTHIVE_BROWSE_PORT': str(upstream_port),
        'ANTHIVE_MCP_HOST': '127.0.0.1', 'ANTHIVE_MCP_PORT': str(upstream_port),
    })
    return env


def _host_snippets(ant_stack, stack: Path) -> None:
    """auth.caddy imports from the container path; point it at the repo."""
    auth = stack / 'caddy' / 'auth.caddy'
    auth.write_text(auth.read_text().replace(ant_stack.C_CADDY_CONFIG, str(REPO / 'config')))


@pytest.mark.parametrize('tls_mode', ['off', 'internal'])
def test_rendered_caddyfile_validates(tmp_path, ant_stack, tls_mode):
    caddy = _caddy()
    if caddy is None:
        pytest.skip('caddy not installed')
    site = _site(ant_stack, tmp_path, tls={'mode': tls_mode}, ports={'bind': '127.0.0.1'})
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    _host_snippets(ant_stack, stack)
    env = _caddy_env(ant_stack, site, stack, _free_port(), 9091, 8080)
    result = subprocess.run([caddy, 'adapt', '--config', str(REPO / 'config' / 'Caddyfile'),
                             '--adapter', 'caddyfile'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    servers = json.loads(result.stdout)['apps']['http']['servers']
    trusted = [srv.get('trusted_proxies') for srv in servers.values()]
    if tls_mode == 'off':
        assert all(t and '10.0.0.0/8' in t['ranges'] for t in trusted)
    else:
        assert not any(trusted)
    validate = subprocess.run([caddy, 'validate', '--config', str(REPO / 'config' / 'Caddyfile'),
                               '--adapter', 'caddyfile'], env=env, capture_output=True, text=True)
    assert validate.returncode == 0, validate.stderr


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append({'path': self.path, **{k.lower(): v for k, v in self.headers.items()}})
        self.send_response(200)
        self.send_header('Remote-User', 'boss')
        self.send_header('Remote-Groups', 'admins')
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *args):
        pass


@pytest.mark.parametrize('trusted,expect_proto', [(None, 'https'), ([], 'http')])
def test_authelia_sees_the_forwarded_scheme_behind_a_proxy(tmp_path, ant_stack, trusted, expect_proto):
    """tls off + authelia: a proxy on a private address says https; Authelia
    (here a recorder) must get https in X-Forwarded-Proto and X-Original-URL.
    With trust switched off Caddy replaces it with http (the 2.40 bug)."""
    caddy = _caddy()
    if caddy is None:
        pytest.skip('caddy not installed')
    tls = {'mode': 'off'} if trusted is None else {'mode': 'off', 'trusted_proxies': trusted}
    site = _site(ant_stack, tmp_path, tls=tls, ports={'bind': '127.0.0.1'})
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    _host_snippets(ant_stack, stack)
    recorder_cls = type('Rec', (_Recorder,), {'seen': []})
    server = http.server.HTTPServer(('127.0.0.1', 0), recorder_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = _free_port()
    env = _caddy_env(ant_stack, site, stack, port, server.server_port, server.server_port)
    proc = subprocess.Popen([caddy, 'run', '--config', str(REPO / 'config' / 'Caddyfile'),
                             '--adapter', 'caddyfile'], env=env, cwd=tmp_path,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        import urllib.request  # noqa: PLC0415
        deadline = time.monotonic() + 15
        body = None
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/health",
                    headers={'Host': 'anthive.example.org', 'X-Forwarded-Proto': 'https',
                             'X-Forwarded-For': '203.0.113.9'})
                body = urllib.request.urlopen(request, timeout=3).read()
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode()[-2000:])
                time.sleep(0.3)
        assert body == b'ok'
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server.shutdown()
    auth_calls = [h for h in recorder_cls.seen if h['path'].startswith('/authelia/api/authz/auth-request-api')]
    assert auth_calls, recorder_cls.seen
    call = auth_calls[0]
    assert call['x-forwarded-proto'] == expect_proto
    if expect_proto == 'https':
        assert call['x-original-url'] == 'https://anthive.example.org/api/health'
        assert call['x-forwarded-for'].startswith('203.0.113.9')


# ── 4. pull renders first and pulls what site.yaml names now ────────────────

def test_pull_uses_the_current_tag_not_a_stale_render(tmp_path, ant_stack, monkeypatch, capsys):
    path = _write_site(tmp_path, images={'tag': '2.37.2'})
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    ant_stack.render(site, quiet=True)                       # the old site's .env
    _write_site(tmp_path, images={'tag': '2.40.0'})        # the operator edits the tag
    calls: list[list[str]] = []
    present: set[str] = set()

    class Done:
        def __init__(self, code=0, out=''):
            self.returncode, self.stdout, self.stderr = code, out, ''

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ['docker', 'pull']:
            calls.append(cmd)
            present.add(cmd[2])
            return Done()
        if cmd[:3] == ['docker', 'image', 'inspect']:
            ref = cmd[-1]
            return Done(0, 'sha256:' + 'a' * 64) if ref in present else Done(1)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ant_stack.subprocess, 'run', fake_run)
    code = ant_stack.cmd_pull(argparse.Namespace(site=str(path), no_ingest=False))
    assert code == 0
    pulled = [cmd[2] for cmd in calls]
    registry = 'registry.gitlab.kuleuven.be/bdslab/anthive/anthive4'
    assert f"{registry}/anthive-app:2.40.0" in pulled
    assert f"{registry}/anthive-ingest:2.40.0" in pulled
    assert not any('2.37.2' in ref for ref in pulled)
    assert any(ref.startswith('caddy:') for ref in pulled)
    assert any(ref.startswith('authelia/') for ref in pulled)
    # 2.46.1: pull only stages; the running stack keeps its .env until `up`
    staged = (site['_state'] / 'stack' / 'staging' / '.env').read_text()
    assert 'anthive-app:2.40.0' in staged and '2.37.2' not in staged
    live = (site['_state'] / 'stack' / '.env').read_text()
    assert 'anthive-app:2.37.2' in live
    out = capsys.readouterr()
    assert 'new' in out.out and 'anthive-app:2.40.0' in out.out
    assert 'the running stack uses' in out.out and 'ant-stack up' in out.out


def test_pull_reports_failure(tmp_path, ant_stack, monkeypatch):
    path = _write_site(tmp_path)
    ant_stack.ensure_secrets(ant_stack.load_site(path))

    class Fail:
        returncode, stdout, stderr = 1, '', ''

    real_run = subprocess.run
    monkeypatch.setattr(ant_stack.subprocess, 'run',
                        lambda cmd, *a, **k: Fail() if cmd[0] == 'docker' else real_run(cmd, *a, **k))
    assert ant_stack.cmd_pull(argparse.Namespace(site=str(path), no_ingest=True)) == 1


# ── 2. publish-library ──────────────────────────────────────────────────────

def _git(*args, cwd=None):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def library_repo(tmp_path):
    if shutil.which('git') is None:
        pytest.skip('git not installed')
    remote = tmp_path / 'remote.git'
    _git('init', '--bare', '-b', 'main', str(remote))
    library = tmp_path / 'library'
    _git('clone', str(remote), str(library))
    _git('checkout', '-B', 'main', cwd=library)
    for key, value in (('user.name', 'test'), ('user.email', 't@example.org')):
        _git('config', key, value, cwd=library)
    (library / 'papers').mkdir()
    (library / 'papers' / 'a.md').write_text('---\ntitle: A\n---\n')
    _git('add', '.', cwd=library)
    _git('commit', '-m', 'first', cwd=library)
    _git('push', '-u', 'origin', 'HEAD:main', cwd=library)
    return library, remote


def test_publish_library_pushes_the_records(tmp_path, ant_stack, library_repo):
    library, remote = library_repo
    (library / 'papers' / 'b.md').write_text('---\ntitle: B\n---\n')     # an upload commit
    _git('add', '.', cwd=library)
    _git('commit', '-m', 'upload b', cwd=library)
    key = tmp_path / 'deploy_key'
    key.write_text('dummy\n')
    os.chmod(key, 0o600)
    path = _write_site(tmp_path, data={'h5adstore': str(tmp_path / 'store'), 'library': str(library)},
                       library={'deploy_key': str(key)})
    code = ant_stack.cmd_publish_library(argparse.Namespace(
        site=str(path), no_push=False, no_s3=False, dry_run=False, lock_timeout=5))
    assert code == 0
    log = _git('--git-dir', str(remote), 'log', '--format=%s', 'main').stdout.split('\n')
    assert log[0] == 'upload b'


def test_publish_library_waits_for_the_upload_lock(tmp_path, ant_stack, library_repo):
    library, _remote = library_repo
    path = _write_site(tmp_path, data={'h5adstore': str(tmp_path / 'store'), 'library': str(library)})
    lock_path = ant_stack.library_lock_path(library)
    with open(lock_path, 'a+') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(ant_stack.SiteError, match='locked'):
            ant_stack.cmd_publish_library(argparse.Namespace(
                site=str(path), no_push=False, no_s3=True, dry_run=False, lock_timeout=1))


def test_lock_path_matches_the_upload_path(tmp_path, ant_stack, library_repo):
    library, _remote = library_repo
    library_upload = pytest.importorskip('library_upload')
    assert ant_stack.library_lock_path(library) == library_upload.private_path(library, library_upload.LOCK_NAME)


def test_git_ssh_command_uses_only_the_deploy_key(ant_stack):
    command = ant_stack.git_ssh_command(Path('/srv/keys/library key'))
    assert command == "ssh -i '/srv/keys/library key' -o IdentitiesOnly=yes -o BatchMode=yes"
    assert ant_stack.git_ssh_command(None) is None


def test_publish_library_runs_s3_sync_in_ingest(tmp_path, ant_stack, library_repo, monkeypatch):
    library, _remote = library_repo
    path = _write_site(tmp_path, data={'h5adstore': str(tmp_path / 'store'), 'library': str(library)},
                       s3=S3_ON)
    ant_stack.ensure_secrets(ant_stack.load_site(path))
    _put_s3_keys(tmp_path / 'state')
    seen = []
    monkeypatch.setattr(ant_stack, 'run_compose', lambda site, *args, check=True: seen.append(args) or 0)
    code = ant_stack.cmd_publish_library(argparse.Namespace(
        site=str(path), no_push=True, no_s3=False, dry_run=False, lock_timeout=5))
    assert code == 0
    assert seen and seen[0][-4:] == ('ant', 'sync', 'library', '--no-git')
    assert 'ingest' in seen[0]


def test_publish_library_dry_run_probes_the_remote(tmp_path, ant_stack, library_repo, capsys):
    library, _remote = library_repo
    path = _write_site(tmp_path, data={'h5adstore': str(tmp_path / 'store'), 'library': str(library)})
    code = ant_stack.cmd_publish_library(argparse.Namespace(
        site=str(path), no_push=False, no_s3=False, dry_run=True, lock_timeout=5))
    output = capsys.readouterr()
    assert code == 0
    assert 'remote: reachable' in output.out + output.err
    assert 'ant sync library' not in output.out + output.err      # S3 is off on this site


def test_publish_library_shows_all_of_gits_error(tmp_path, ant_stack, library_repo, capsys):
    library, _remote = library_repo
    _git('remote', 'set-url', 'origin', str(tmp_path / 'missing.git'), cwd=library)
    path = _write_site(tmp_path, data={'h5adstore': str(tmp_path / 'store'), 'library': str(library)})
    code = ant_stack.cmd_publish_library(argparse.Namespace(
        site=str(path), no_push=False, no_s3=True, dry_run=False, lock_timeout=5))
    output = capsys.readouterr()
    assert code == 1
    text = output.out + output.err
    assert 'missing.git' in text and 'does not appear to be a git repository' in text


# ── 2.45.1: fixes from the 2.45.0 scratch-site check ────────────────────────

def _config_shas(ant_stack, site) -> dict[str, str]:
    stack = ant_stack.render(site, quiet=True)
    services = yaml.safe_load((stack / 'compose.site.yaml').read_text())['services']
    return {name: (services[name].get('environment') or {}).get('ANTHIVE_CONFIG_SHA')
            for name in ('api', 'web', 'mcp', 'caddy', 'static', 'authelia', 'ingest')}


def test_config_sha_changes_with_the_rendered_config(tmp_path, ant_stack):
    """A change that only touches a rendered config (internal_requires_auth,
    auth.mode) must change the compose definition of the services that read
    it, or `docker compose up` keeps the old containers running."""
    site = _site(ant_stack, tmp_path)
    ant_stack.ensure_secrets(site)
    before = _config_shas(ant_stack, site)
    for name in ant_stack.CONFIG_HASHED_SERVICES:
        assert before[name] and len(before[name]) == 64
    for name in ('caddy', 'static', 'authelia', 'ingest'):
        assert before[name] is None                 # force-recreated or one-off instead
    assert _config_shas(ant_stack, site) == before   # same input, same hash: no needless recreate

    flipped = _site(ant_stack, tmp_path, auth={'internal_requires_auth': False})
    after = _config_shas(ant_stack, flipped)
    assert all(after[name] != before[name] for name in ('api', 'web', 'mcp'))

    basic = _site(ant_stack, tmp_path, auth={'mode': 'basic'})
    assert _config_shas(ant_stack, basic)['api'] != after['api']


def test_config_sha_only_changes_for_the_services_that_read_it(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, services={'chat': True})
    ant_stack.ensure_secrets(site)
    before = _config_shas(ant_stack, site)
    (site['_state'] / 'secrets' / 'chat_api_key').write_text('sk-NEW\n')   # api config only... and chat.enabled
    after = _config_shas(ant_stack, site)
    assert after['api'] != before['api']
    # chat.enabled flips in the web+mcp config too (the UI shows the chat)
    assert after['web'] != before['web']
    before = after
    (site['_state'] / 'secrets' / 'chat_api_key').write_text('sk-OTHER\n')  # key text only: api alone
    after = _config_shas(ant_stack, site)
    assert after['api'] != before['api']
    assert after['web'] == before['web'] and after['mcp'] == before['mcp']
    # a secret file mounted into mcp and api, not web
    (site['_state'] / 'secrets' / 'internal_secret').write_text('rotated\n')
    rotated = _config_shas(ant_stack, site)
    assert rotated['api'] != after['api'] and rotated['mcp'] != after['mcp']
    assert rotated['web'] == after['web']


def test_restart_takes_several_services(tmp_path, ant_stack, monkeypatch):
    path = _write_site(tmp_path)
    ant_stack.ensure_secrets(ant_stack.load_site(path))
    seen = []
    monkeypatch.setattr(ant_stack, 'run_compose', lambda site, *args, check=True: seen.append(args) or 0)
    monkeypatch.setattr('sys.argv', ['ant-stack', '--site', str(path), 'restart', 'api', 'web', 'mcp'])
    assert ant_stack.main() == 0
    assert seen[-1] == ('restart', 'api', 'web', 'mcp')
    monkeypatch.setattr('sys.argv', ['ant-stack', '--site', str(path), 'restart'])
    assert ant_stack.main() == 0
    assert seen[-1] == ('restart',)


def test_passwd_without_a_terminal_points_to_the_options(tmp_path, ant_stack, monkeypatch, capsys):
    path = _write_site(tmp_path)
    ant_stack.ensure_secrets(ant_stack.load_site(path))
    monkeypatch.delenv('ANTHIVE_NEW_PASSWORD', raising=False)
    import getpass  # noqa: PLC0415

    def no_tty(prompt=''):
        raise EOFError
    monkeypatch.setattr(getpass, 'getpass', no_tty)
    monkeypatch.setattr('sys.argv', ['ant-stack', '--site', str(path), 'passwd', 'boss'])
    assert ant_stack.main() == 2
    err = capsys.readouterr().err
    assert '--password' in err and 'ANTHIVE_NEW_PASSWORD' in err


def test_internal_access_line_in_every_auth_mode(tmp_path, ant_stack):
    cases = {
        ('authelia', True): ('visible only to signed-in users', False),
        ('basic', True): ('visible only to signed-in users', False),
        ('none', True): ('hidden on this site', False),
        ('trusted-proxy', True): ('upstream proxy', True),
        ('authelia', False): ('without an identity', True),
        ('none', False): ('ANYONE', True),
    }
    site = _site(ant_stack, tmp_path)
    for (mode, requires_auth), (expected, warns) in cases.items():
        site['auth']['mode'] = mode
        site['auth']['internal_requires_auth'] = requires_auth
        line, warning = ant_stack.internal_access_summary(site, 97, 159)
        assert line.startswith('97 of 159 datasets are internal') and expected in line, (mode, line)
        assert bool(warning) is warns, (mode, requires_auth)
    line, warning = ant_stack.internal_access_summary(site, 0, 159)
    assert line.startswith('0 of 159') and warning is None


def test_plan_labels_the_container_listener_and_the_host_address(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path, tls={'mode': 'internal'}, ports={'bind': '127.0.0.1', 'https': 9443})
    ant_stack.print_plan(site)
    out = capsys.readouterr().out
    assert '(in container): https://anthive.example.org:8443' in out
    assert '127.0.0.1:9443 (host) -> :8443 (caddy)' in out


def test_container_config_copies_info_logs_to_stderr(tmp_path, ant_stack):
    import config  # noqa: PLC0415
    site = _site(ant_stack, tmp_path)
    assert ant_stack.render_anthive_yaml(site, 'api')['log_stderr_level'] == 'info'
    assert config.get_log_stderr_level({}) == 'warning'          # dev supervisor: no duplicate INFO lines
    with pytest.raises(config.ConfigError):
        config.get_log_stderr_level({'log_stderr_level': 'loud'})


# ── 2.46.1: pull/check stage the render, only up applies it ──────────────────

def test_pull_and_check_stage_and_up_applies(tmp_path, ant_stack, monkeypatch):
    """A restarting old api must keep reading the config of its own release until `up`."""
    path = _write_site(tmp_path, images={'tag': '2.46.0'})
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    ant_stack.render(site, quiet=True)
    stack = site['_state'] / 'stack'
    live_before = {name: (stack / name).read_bytes() for name in ('anthive-api.yaml', 'anthive.yaml', '.env',
                                                                     'compose.site.yaml')}
    _write_site(tmp_path, images={'tag': '2.46.1'}, anthive={'max_obscat_values': 77})
    site = ant_stack.load_site(path)
    ant_stack.render(site, quiet=True, apply=False)            # what pull, check, ingest, logs do
    assert {name: (stack / name).read_bytes() for name in live_before} == live_before
    assert 'max_obscat_values: 77' in (stack / 'staging' / 'anthive-api.yaml').read_text()
    assert set(ant_stack.staged_changes(site)) >= {'anthive-api.yaml', '.env', 'compose.site.yaml'}
    ant_stack.render(site, quiet=True)                         # what up does
    assert 'max_obscat_values: 77' in (stack / 'anthive-api.yaml').read_text()
    assert 'anthive-app:2.46.1' in (stack / '.env').read_text()
    assert ant_stack.staged_changes(site) == []
    assert oct((stack / 'anthive-api.yaml').stat().st_mode & 0o777) == '0o600'


def test_staged_compose_digest_matches_the_applied_files(tmp_path, ant_stack):
    """`up` recreates exactly the services whose config changed: the digest in the staged
    compose file must equal the digest of the files once they are in place."""
    path = _write_site(tmp_path)
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    ant_stack.render(site, quiet=True)
    _write_site(tmp_path, anthive={'max_obscat_values': 55})
    site = ant_stack.load_site(path)
    ant_stack.render(site, quiet=True, apply=False)
    staged = yaml.safe_load((site['_state'] / 'stack' / 'staging' / 'compose.site.yaml').read_text())
    ant_stack.render(site, quiet=True)
    live = ant_stack.render_compose_site(site)
    for name in ant_stack.CONFIG_HASHED_SERVICES:
        assert staged['services'][name]['environment'] == live['services'][name]['environment']


def test_first_render_is_applied_even_when_only_staging(tmp_path, ant_stack):
    path = _write_site(tmp_path)
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    ant_stack.render(site, quiet=True, apply=False)
    assert (site['_state'] / 'stack' / '.env').exists()
    assert (site['_state'] / 'stack' / 'anthive-api.yaml').exists()


def test_render_validation_is_strict_but_the_api_only_warns(tmp_path, ant_stack, monkeypatch):
    """ant-stack still refuses a typo in site.yaml's anthive: block (strict validation of a fresh
    render), while a running api reading a config with a key it does not know only warns."""
    path = _write_site(tmp_path, anthive={'no_such_key_anywhere': 1})
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    with pytest.raises(ant_stack.SiteError, match='no_such_key_anywhere'):
        ant_stack.render(site, quiet=True)
    config_path = tmp_path / 'anthive.yaml'
    config_path.write_text('no_such_key_anywhere: 1\n')
    env = {**os.environ, 'ANTHIVE_CONFIG': str(config_path)}
    env.pop('ANTHIVE_CONFIG_STRICT', None)
    result = subprocess.run(['python3', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import config; '
                             'config.set_unknown_keys_lenient(); config.load_config(); print("loaded")',
                             str(REPO / 'lib')], env=env, capture_output=True, text=True)
    assert result.returncode == 0 and 'loaded' in result.stdout
    assert 'ignoring unknown configuration parameters: no_such_key_anywhere' in result.stderr
    # a host tool (no set_unknown_keys_lenient) stays strict
    result = subprocess.run(['python3', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import config; '
                             'config.load_config()', str(REPO / 'lib')], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and 'Unknown configuration parameters' in result.stderr
    # every server calls set_unknown_keys_lenient before it loads its config
    for script in ('bin/api/ant-serve', 'bin/api/ant-mcp', 'web/ant-browse'):
        assert '_anthive_config_module.set_unknown_keys_lenient()' in (REPO / script).read_text(), script


# ── 2.46.1: fixes from the anthive-intern report and the 2.46.0 scratch site ──

def test_check_counts_datasets_like_the_catalog_scan(tmp_path, ant_stack):
    """Store-root documents and static/*.h5ad.md are not datasets (they made `check` report
    'N datasets have no local .duckdb' on intern)."""
    store = tmp_path / 'store'
    for rel in ('CLAUDE.md', 'CONVENTIONS.md', 'HANDOFF.md', 'G/index.md', 'G/README.md', 'G/ds.md', 'G/ds2.md',
                'G/static/atlas.h5ad.md', '_archive/G/old.md', 'G/_versions/ds/1.0.0/ds.md', 'H/sub/deep.md'):
        (store / rel).parent.mkdir(parents=True, exist_ok=True)
        (store / rel).write_text('---\nid: x\n---\n')
    (store / 'G' / 'ds2.inactive').write_text('')
    found = sorted(str(rel) for _item, _resolved, rel in ant_stack._catalog_dataset_mds(store))
    assert found == ['G/ds.md', 'H/sub/deep.md']
    assert ant_stack._internal_dataset_count(store) == (2, 2)


def test_internal_access_line_for_an_empty_store(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path)
    line, warning = ant_stack.internal_access_summary(site, 0, 0)
    assert line.startswith('no datasets yet') and warning is None


def test_publish_library_says_up_to_date_when_nothing_was_pushed(tmp_path, ant_stack, library_repo, capsys):
    library, _remote = library_repo
    path = _write_site(tmp_path, data={'h5adstore': str(tmp_path / 'store'), 'library': str(library)})
    args = argparse.Namespace(site=str(path), no_push=False, no_s3=True, dry_run=False, lock_timeout=5)
    assert ant_stack.cmd_publish_library(args) == 0
    out = capsys.readouterr().out
    assert 'records: up to date' in out and 'records: pushed' not in out
    (library / 'papers' / 'c.md').write_text('---\ntitle: C\n---\n')
    _git('add', '.', cwd=library)
    _git('commit', '-m', 'upload c', cwd=library)
    assert ant_stack.cmd_publish_library(args) == 0
    assert 'records: pushed 1 commit(s)' in capsys.readouterr().out


def test_publish_library_s3_host_config_runs_ant_sync_on_the_host(tmp_path, ant_stack, library_repo, monkeypatch):
    library, _remote = library_repo
    host_config = tmp_path / 'host-anthive.yaml'
    host_config.write_text('s3: {bucket: b}\n')
    # S3 is off for the containers: the keys live only in the host config
    path = _write_site(tmp_path, data={'h5adstore': str(tmp_path / 'store'), 'library': str(library)})
    seen: list = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if str(cmd[0]).endswith('ant-sync'):
            seen.append((cmd, kwargs.get('env', {}).get('ANTHIVE_CONFIG')))
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ant_stack.subprocess, 'run', fake_run)
    monkeypatch.setattr(ant_stack, 'run_compose', lambda *a, **k: pytest.fail('ingest container used'))
    code = ant_stack.cmd_publish_library(argparse.Namespace(
        site=str(path), no_push=True, no_s3=False, dry_run=True, lock_timeout=5, s3_host_config=str(host_config)))
    assert code == 0
    assert seen == [([str(REPO / 'bin/ops/ant-sync'), 'library', '--no-git', '--dry-run'], str(host_config))]
    with pytest.raises(ant_stack.SiteError, match='exclude'):
        ant_stack.cmd_publish_library(argparse.Namespace(
            site=str(path), no_push=True, no_s3=True, dry_run=False, lock_timeout=5, s3_host_config=str(host_config)))
    with pytest.raises(ant_stack.SiteError, match='does not exist'):
        ant_stack.cmd_publish_library(argparse.Namespace(
            site=str(path), no_push=True, no_s3=False, dry_run=False, lock_timeout=5,
            s3_host_config=str(tmp_path / 'missing.yaml')))


def _deposit_site(ant_stack, tmp_path, **overrides):
    path = _write_site(tmp_path, services={'dataset_deposit': True}, s3=S3_ON, **overrides)
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    _put_s3_keys(site['_state'])
    return site


def test_watcher_config_uses_host_paths_and_the_tools_listener(tmp_path, ant_stack):
    site = _deposit_site(ant_stack, tmp_path)
    stack = ant_stack.render(site, quiet=True)
    watcher = yaml.safe_load((stack / 'anthive-watcher.yaml').read_text())
    state = str(site['_state'])
    for key in ('state_dir', 'ingest_state_dir', 'tmp_dir', 'log_dir', 'convert_staging_dir', 'ingest_work_dir',
                'duckdb_temp_directory', 'webapp_dir'):
        assert str(watcher[key]).startswith(state), (key, watcher[key])
    text = (stack / 'anthive-watcher.yaml').read_text()
    assert '/scratch' not in text and '/var/lib/anthive' not in text and 'http://api:8080' not in text
    assert watcher['api_url'] == 'http://127.0.0.1:8079' and watcher['ingest_watch']['enabled'] is True
    assert watcher['s3']['secret_access_key'] == 'RW-SECRET'
    caddy = yaml.safe_load((stack / 'compose.site.yaml').read_text())['services']['caddy']
    assert '127.0.0.1:8079:8079' in caddy['ports']
    assert caddy['environment']['ANTHIVE_HTTP_REDIRECT_SNIPPET'] == '/etc/anthive-caddy/tools.caddy'
    tools = (stack / 'caddy' / 'tools.caddy').read_text()
    assert 'not header X-Anthive-Internal *' in tools and 'request_header -Remote-User' in tools
    assert 'request_header -Remote-Groups' in tools and 'reverse_proxy' in tools
    command = ant_stack.watcher_command(site)
    for name in ('ANTHIVE_CONFIG=', 'ANTHIVE_TMP_DIR=', 'ANTHIVE_INGEST_WORK_DIR=', 'ANTHIVE_LOG_DIR=',
                 'ANTHIVE_CONVERT_STAGING_DIR=', 'ANTHIVE_API_URL=http://127.0.0.1:8079', 'ANTHIVE_INGEST_STATE_DIR=',
                 'ANTHIVE_INGEST_WATCH_PREFIX=__incoming/', 'bin/ingest/ant-ingest-watch'):
        assert name in command, name
    # the config loads on the host: the internal secret comes from the host state_dir
    env = {**os.environ, **ant_stack.watcher_env(site)}
    result = subprocess.run(['python3', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import config; '
                             'print(config.internal_auth_headers()["X-Anthive-Internal"]); print(config.get_tmp_dir())',
                             str(REPO / 'lib')], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    secret, tmp_dir = result.stdout.split()
    assert secret == (site['_state'] / 'secrets' / 'internal_secret').read_text().strip()
    assert tmp_dir.startswith(state)


def test_no_tools_listener_without_deposits(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path)
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    caddy = yaml.safe_load((stack / 'compose.site.yaml').read_text())['services']['caddy']
    assert not any(':8079' in port for port in caddy['ports'])
    assert 'no tools listener' in (stack / 'caddy' / 'tools.caddy').read_text()


def test_check_prints_the_watcher_command(tmp_path, ant_stack, capsys):
    site = _deposit_site(ant_stack, tmp_path)
    assert ant_stack._check_datasets(site) == []
    out = capsys.readouterr().out
    assert 'ant-stack watch' in out and 'ANTHIVE_LOG_DIR=' in out and 'ANTHIVE_API_URL=http://127.0.0.1:8079' in out


def test_internal_secret_read_problems_are_warned(tmp_path, monkeypatch, capsys):
    import config as config_module
    monkeypatch.delenv('ANTHIVE_INTERNAL_SECRET', raising=False)
    monkeypatch.setattr(config_module, 'load_config', lambda: {'state_dir': str(tmp_path / 'nowhere')})
    monkeypatch.setattr(config_module, '_WARNED_ONCE', set())
    assert config_module.internal_auth_headers() == {}
    assert 'no internal secret at' in capsys.readouterr().err


def test_second_init_does_not_claim_to_create_the_passwords_file(tmp_path, ant_stack, capsys):
    path = _write_site(tmp_path)
    site = ant_stack.load_site(path)
    assert 'secrets/initial-passwords.txt' in ant_stack.ensure_secrets(site)
    path = _write_site(tmp_path, auth={'users': [{'name': 'boss', 'groups': ['admins']}, {'name': 'new'}]})
    created = ant_stack.ensure_secrets(ant_stack.load_site(path))
    assert 'secrets/initial-passwords.txt' not in created
    assert 'added 1 initial password(s)' in capsys.readouterr().out


def test_up_recreates_authelia_once(tmp_path, ant_stack, monkeypatch):
    site_path_ = _write_site(tmp_path)
    ant_stack.ensure_secrets(ant_stack.load_site(site_path_))
    calls: list = []
    monkeypatch.setattr(ant_stack, 'run_compose', lambda site, *args, check=True: calls.append(args) or 0)
    monkeypatch.setattr(ant_stack, 'check', lambda site, running=False: [])
    assert ant_stack.cmd_up(argparse.Namespace(site=str(site_path_), no_wait=True, wait_timeout=10)) == 0
    recreated = [args for args in calls if '--force-recreate' in args and 'authelia' in args]
    assert len(recreated) == 1


# ── security review of 2.46.1 ────────────────────────────────────────────────

def test_caddy_logs_never_contain_the_internal_secret(tmp_path, ant_stack):
    """S1: a 502 on the tools listener logged every request header, X-Anthive-Internal included."""
    caddy = _caddy()
    if caddy is None:
        pytest.skip('caddy not installed')
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    site = _deposit_site(ant_stack, tmp_path, tls={'mode': 'off'}, ports={'bind': '127.0.0.1'})
    stack = ant_stack.render(site, quiet=True)
    _host_snippets(ant_stack, stack)
    tools_port, dead_port = _free_port(), _free_port()
    env = _caddy_env(ant_stack, site, stack, _free_port(), dead_port, dead_port)
    env.update({'ANTHIVE_HTTP_REDIRECT_SNIPPET': str(stack / 'caddy' / 'tools.caddy'),
                'ANTHIVE_TOOLS_PORT': str(tools_port), 'ANTHIVE_API_PORT': str(dead_port)})
    proc = subprocess.Popen([caddy, 'run', '--config', str(REPO / 'config' / 'Caddyfile'), '--adapter', 'caddyfile'],
                            env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    statuses = []
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len(statuses) < 2:
            try:
                for headers in ({'X-Anthive-Internal': 'SUPERSECRET123', 'Remote-User': 'boss'}, {}):
                    request = urllib.request.Request(f"http://127.0.0.1:{tools_port}/admin/rescan", headers=headers)
                    try:
                        urllib.request.urlopen(request, timeout=3)
                    except urllib.error.HTTPError as error:
                        statuses.append(error.code)
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode()[-2000:])
                statuses.clear()
                time.sleep(0.3)
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
    assert statuses == [502, 403]                 # with the secret: forwarded (dead upstream); without: refused
    log = (out + err).decode()
    assert '502' in log and '/admin/rescan' in log
    assert 'SUPERSECRET123' not in log and 'X-Anthive-Internal' not in log
    assert '"Remote-User"' not in log


def test_up_applies_nothing_when_check_blocks(tmp_path, ant_stack, monkeypatch):
    """S3: the live config changes only when `up` goes ahead."""
    path = _write_site(tmp_path)
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    ant_stack.render(site, quiet=True)
    stack = site['_state'] / 'stack'
    before = (stack / 'anthive-api.yaml').read_bytes()
    _write_site(tmp_path, anthive={'max_obscat_values': 66})
    calls: list = []
    monkeypatch.setattr(ant_stack, 'run_compose', lambda site, *args, check=True: calls.append(args) or 0)
    monkeypatch.setattr(ant_stack, 'check', lambda site, running=False: ['port in use'])
    assert ant_stack.cmd_up(argparse.Namespace(site=str(path), no_wait=True, wait_timeout=10)) == 1
    assert (stack / 'anthive-api.yaml').read_bytes() == before and calls == []
    assert b'max_obscat_values: 66' in (stack / 'staging' / 'anthive-api.yaml').read_bytes()
    monkeypatch.setattr(ant_stack, 'check', lambda site, running=False: [])
    assert ant_stack.cmd_up(argparse.Namespace(site=str(path), no_wait=True, wait_timeout=10)) == 0
    assert b'max_obscat_values: 66' in (stack / 'anthive-api.yaml').read_bytes()


def test_passwd_promotes_only_the_auth_files(tmp_path, ant_stack):
    path = _write_site(tmp_path, auth={'mode': 'basic', 'admins': ['boss'], 'users': [{'name': 'boss'}]})
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    ant_stack.render(site, quiet=True)
    stack = site['_state'] / 'stack'
    before_env, before_auth = (stack / '.env').read_bytes(), (stack / 'caddy' / 'auth.caddy').read_bytes()
    _write_site(tmp_path, auth={'mode': 'basic', 'admins': ['boss'], 'users': [{'name': 'boss'}]},
                images={'tag': '9.9.9'})                                    # a staged upgrade
    assert ant_stack.cmd_passwd(argparse.Namespace(site=str(path), user='boss', password='a-long-password')) == 0
    assert (stack / '.env').read_bytes() == before_env                        # the upgrade stays staged
    assert (stack / 'caddy' / 'auth.caddy').read_bytes() != before_auth      # the new hash is live
    assert oct((stack / 'caddy' / 'auth.caddy').stat().st_mode & 0o777) == '0o644'    # Caddy may be a remapped uid
    assert oct(stack.stat().st_mode & 0o777) == '0o700'                              # private on the host


def test_render_writes_atomically_under_a_lock(tmp_path, ant_stack, monkeypatch):
    """C1: no partial file is ever visible; two renders do not interleave."""
    path = _write_site(tmp_path)
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    ant_stack.render(site, quiet=True)
    stack = site['_state'] / 'stack'
    inode = (stack / 'anthive-api.yaml').stat().st_ino
    _write_site(tmp_path, anthive={'max_obscat_values': 61})
    ant_stack.render(ant_stack.load_site(path), quiet=True)
    assert (stack / 'anthive-api.yaml').stat().st_ino != inode               # replaced by rename, not rewritten
    assert not list(stack.rglob('.*.tmp'))
    import fcntl  # noqa: PLC0415
    with open(stack / '.lock', 'a+') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        done = []
        thread = threading.Thread(target=lambda: done.append(ant_stack.render(ant_stack.load_site(path), quiet=True)))
        thread.start()
        thread.join(1.0)
        assert not done                                                     # waits for the lock
    thread.join(20)
    assert done


def test_ports_must_be_numbers(tmp_path, ant_stack):
    for bad in (None, 0, 'x', 70000):
        with pytest.raises(ant_stack.SiteError, match='ports.http'):
            _site(ant_stack, tmp_path, ports={'http': bad})


def test_watcher_config_only_with_deposits(tmp_path, ant_stack):
    site = _deposit_site(ant_stack, tmp_path)
    stack = ant_stack.render(site, quiet=True)
    assert (stack / 'anthive-watcher.yaml').exists()
    path = _write_site(tmp_path, services={'dataset_deposit': False}, s3=S3_ON)
    site = ant_stack.load_site(path)
    ant_stack.render(site, quiet=True, apply=False)               # staging only: the running watcher keeps it
    assert (stack / 'anthive-watcher.yaml').exists() and not (stack / 'staging' / 'anthive-watcher.yaml').exists()
    ant_stack.render(site, quiet=True)
    assert not (stack / 'anthive-watcher.yaml').exists() and not (stack / 'staging' / 'anthive-watcher.yaml').exists()
    with pytest.raises(ant_stack.SiteError, match='dataset_deposit'):
        ant_stack.cmd_config_path(argparse.Namespace(site=str(path), role='watcher'))



# ── re-review of 2.46.1 ──────────────────────────────────────────────────────

def test_check_warns_when_caddy_cannot_read_its_snippets(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path)
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    for path in (stack / 'caddy').iterdir():
        assert path.stat().st_mode & 0o004, path                   # every snippet readable by any uid
    ant_stack._check_caddy_readable(site)
    assert 'Caddy may not be able to read' not in capsys.readouterr().err
    os.chmod(stack / 'caddy' / 'auth.caddy', 0o600)
    ant_stack._check_caddy_readable(site)
    assert 'auth.caddy' in capsys.readouterr().err


def test_up_holds_the_stack_lock_from_stage_to_promote(tmp_path, ant_stack, monkeypatch):
    path = _write_site(tmp_path)
    ant_stack.ensure_secrets(ant_stack.load_site(path))
    depth_during_check: list = []
    monkeypatch.setattr(ant_stack, 'run_compose', lambda site, *args, check=True: 0)
    monkeypatch.setattr(ant_stack, 'check',
                        lambda site, running=False: depth_during_check.append(ant_stack._STACK_LOCK_DEPTH['depth']) or [])
    assert ant_stack.cmd_up(argparse.Namespace(site=str(path), no_wait=True, wait_timeout=10)) == 0
    assert depth_during_check == [1]
