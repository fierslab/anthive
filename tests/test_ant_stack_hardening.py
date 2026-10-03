"""Tests for the 2.41.0 ant-stack hardening (bin/ops/ant-stack).

Covers: S3 keys and other secrets reaching only the containers that use
them, the secret-permission warnings of `check`, Caddy's trusted proxies
and the forwarded scheme towards Authelia (tls off behind a proxy), `pull`
using the refs site.yaml names now, and the host-side `publish-library`.

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
    return {role: (stack / name).read_text() for role, name in ant_stack.CONFIG_ROLES.items()}


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
    for name in ant_stack.CONFIG_ROLES.values():
        assert oct((site['_state'] / 'stack' / name).stat().st_mode & 0o777) == '0o600'
    assert oct((site['_state'] / 'stack').stat().st_mode & 0o777) == '0o700'


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
    env = (site['_state'] / 'stack' / '.env').read_text()
    assert 'anthive-app:2.40.0' in env and '2.37.2' not in env
    out = capsys.readouterr()
    assert 'new' in out.out and 'anthive-app:2.40.0' in out.out
    assert 'previous render named' in out.out


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
