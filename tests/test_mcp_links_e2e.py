"""End-to-end: personal MCP links through Caddy -> ant-mcp -> ant-serve.

Spins up, on private ports, a fake Authelia (cookie "testuser=<name>" means
signed in), a real ant-serve on a scratch copy of datasets, a real ant-mcp,
and the real config/Caddyfile with the dev auth snippet re-pointed at the
fake Authelia. Then drives it with the official MCP python SDK.

Opt-in: ANTHIVE_E2E_MCP=1 and ANTHIVE_E2E_DATA=<folder with >=1 dataset>.
Needs the `mcp` SDK (<2) and a caddy binary (ANTHIVE_E2E_CADDY or
envs/caddy/caddy). Writes a temporary anthive.yaml in the repo root (removed
afterwards; refuses to run if one exists).
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = os.environ.get('ANTHIVE_E2E_DATA')
CADDY = os.environ.get('ANTHIVE_E2E_CADDY') or str(REPO_ROOT / 'envs' / 'caddy' / 'caddy')

pytestmark = pytest.mark.skipif(
    os.environ.get('ANTHIVE_E2E_MCP') != '1' or not DATA,
    reason='set ANTHIVE_E2E_MCP=1 and ANTHIVE_E2E_DATA to run the MCP-link e2e test')

mcp_sdk = pytest.importorskip('mcp')
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

import requests  # noqa: E402

API_PORT, MCP_PORT, CADDY_PORT, AUTH_PORT = 18180, 18190, 18501, 18991
BASE = f'http://127.0.0.1:{CADDY_PORT}'
USERS = {'alice': 'users', 'bob': 'users,admins'}


class _FakeAuthelia(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        match = re.search(r'testuser=(\w+)', self.headers.get('Cookie', ''))
        user = match.group(1) if match else None
        if user in USERS:
            self.send_response(200)
            self.send_header('Remote-User', user)
            self.send_header('Remote-Groups', USERS[user])
            self.send_header('Remote-Name', user.title())
            self.send_header('Remote-Email', f'{user}@example.org')
            self.end_headers()
            return
        if 'auth-request-api' in self.path:
            self.send_response(401)
        else:
            self.send_response(302)
            self.send_header('Location', '/authelia/')
        self.end_headers()

    do_POST = do_GET


def _wait_port(port, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                return
        time.sleep(0.5)
    raise RuntimeError(f'port {port} never opened')


@pytest.fixture(scope='module')
def stack(tmp_path_factory):
    for port in (API_PORT, MCP_PORT, CADDY_PORT, AUTH_PORT):
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                pytest.skip(f'port {port} busy')
    config_path = REPO_ROOT / 'anthive.yaml'
    if config_path.exists():
        pytest.skip('repo-root anthive.yaml exists; refusing to overwrite it')
    work = tmp_path_factory.mktemp('mcp_e2e')
    state_dir = work / 'state'
    config_path.write_text(
        'mcp:\n'
        '  verify_cache_seconds: 1\n'
        '  verify_negative_cache_seconds: 0\n'
        '  plots_dir: null\n')
    auth_snippet = work / 'caddy-auth.test'
    auth_snippet.write_text((REPO_ROOT / 'config' / 'caddy-auth.dev').read_text()
                            .replace('localhost:9091', f'localhost:{AUTH_PORT}'))
    env = dict(os.environ, ANTHIVE_STATE_DIR=str(state_dir))
    caddy_env = dict(
        env, ANTHIVE_LISTEN=f'http://:{CADDY_PORT}', ANTHIVE_CADDY_BIND='127.0.0.1',
        ANTHIVE_TLS_SNIPPET='caddy-tls.prod', ANTHIVE_HTTP_REDIRECT_SNIPPET='caddy-http-redirect.prod',
        ANTHIVE_AUTH_SNIPPET=str(auth_snippet), ANTHIVE_CADDY_LOG=str(work / 'access.log'),
        ANTHIVE_API_PORT=str(API_PORT), ANTHIVE_BROWSE_PORT='18181',
        ANTHIVE_MCP_PORT=str(MCP_PORT), ANTHIVE_AUTHELIA_PORT=str(AUTH_PORT))

    auth_server = http.server.ThreadingHTTPServer(('127.0.0.1', AUTH_PORT), _FakeAuthelia)
    threading.Thread(target=auth_server.serve_forever, daemon=True).start()

    logs = {name: open(work / f'{name}.log', 'w') for name in ('api', 'mcp', 'caddy')}
    procs = [
        subprocess.Popen(['uv', 'run', '--quiet', '--script', 'bin/api/ant-serve',
                          '--data-folder', DATA, '--host', '127.0.0.1', '--port', str(API_PORT)],
                         cwd=REPO_ROOT, env=env, stdout=logs['api'], stderr=subprocess.STDOUT),
    ]
    try:
        _wait_port(API_PORT)
        procs.append(subprocess.Popen(
            ['uv', 'run', '--quiet', '--script', 'bin/api/ant-mcp',
             '--api-url', f'http://127.0.0.1:{API_PORT}', '--port', str(MCP_PORT)],
            cwd=REPO_ROOT, env=env, stdout=logs['mcp'], stderr=subprocess.STDOUT))
        procs.append(subprocess.Popen(
            [CADDY, 'run', '--config', 'config/Caddyfile', '--adapter', 'caddyfile'],
            cwd=REPO_ROOT, env=caddy_env, stdout=logs['caddy'], stderr=subprocess.STDOUT))
        _wait_port(MCP_PORT)
        _wait_port(CADDY_PORT)
        for _ in range(120):
            try:
                if requests.get(f'http://127.0.0.1:{API_PORT}/health', timeout=2).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        yield {'work': work, 'state_dir': state_dir}
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        auth_server.shutdown()
        for handle in logs.values():
            handle.close()
        config_path.unlink(missing_ok=True)
        keep = os.environ.get('ANTHIVE_E2E_KEEP_LOGS')
        if keep:
            shutil.copytree(work, keep, dirs_exist_ok=True)


def _cookie(user):
    return {'Cookie': f'testuser={user}'}


def _rotate(user):
    response = requests.post(f'{BASE}/api/me/mcp/rotate', headers=_cookie(user), timeout=30)
    assert response.status_code == 200, response.text
    return response.json()


async def _session_call(url, headers=None, tool_calls=()):
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = sorted(tool.name for tool in (await session.list_tools()).tools)
            results = []
            for name, args in tool_calls:
                result = await session.call_tool(name, args)
                results.append(result)
            return tools, results


def _text(result):
    return ' '.join(getattr(block, 'text', '') for block in result.content)


def test_ui_endpoint_through_caddy(stack):
    assert requests.get(f'{BASE}/api/me/mcp', timeout=10).status_code == 401
    body = requests.get(f'{BASE}/api/me/mcp', headers=_cookie('alice'), timeout=10).json()
    assert body['enabled'] and body['user'] == 'alice' and not body['has_link']
    issued = _rotate('alice')
    assert issued['url'].startswith(f'http://127.0.0.1:{CADDY_PORT}/mcp/u/amcp_')
    again = requests.get(f'{BASE}/api/me/mcp', headers=_cookie('alice'), timeout=10).json()
    assert again['url'] == issued['url']


def test_path_link_lists_and_calls_tools(stack):
    url = _rotate('alice')['url']
    tools, results = asyncio.run(_session_call(url, tool_calls=[
        ('search_datasets', {}),
    ]))
    assert 'search_datasets' in tools and 'get_dataset_info' in tools
    assert 'analytics_viz' not in tools and 'rescan_databases' not in tools
    listing = _text(results[0])
    db_id = re.search(r'\b([A-Z0-9]{2,4}/[A-Za-z0-9]{2,4})\b', listing).group(1)
    _, info = asyncio.run(_session_call(url, tool_calls=[('get_dataset_info', {'db_id': db_id})]))
    assert db_id in _text(info[0]) and not info[0].isError


def test_bearer_header(stack):
    token = _rotate('alice')['url'].rsplit('/', 1)[1]
    tools, _ = asyncio.run(_session_call(f'{BASE}/mcp', headers={'Authorization': f'Bearer {token}'}))
    assert 'search_datasets' in tools


def test_admin_gets_admin_tools_and_user_cannot_call_them(stack):
    admin_url = _rotate('bob')['url']
    tools, _ = asyncio.run(_session_call(admin_url))
    assert {'analytics_viz', 'rescan_databases'} <= set(tools)
    user_url = _rotate('alice')['url']
    _, results = asyncio.run(_session_call(user_url, tool_calls=[('rescan_databases', {})]))
    assert results[0].isError and 'admins' in _text(results[0])
    _, admin_results = asyncio.run(_session_call(admin_url, tool_calls=[('rescan_databases', {})]))
    assert not admin_results[0].isError and 'Rescan complete' in _text(admin_results[0])


def _status(url, headers=None):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                       'clientInfo': {'name': 't', 'version': '0'}}}
    return requests.post(url, json=body, timeout=15, allow_redirects=False,
                         headers={'Accept': 'application/json, text/event-stream', **(headers or {})}
                         ).status_code


def test_rotate_revoke_and_bad_tokens(stack):
    old = _rotate('alice')['url']
    assert _status(old) == 200
    new = _rotate('alice')['url']
    time.sleep(1.5)                       # verify_cache_seconds = 1 in the test config
    assert _status(old) == 401
    assert _status(new) == 200
    assert requests.delete(f'{BASE}/api/me/mcp', headers=_cookie('alice'), timeout=10).json() == {'ok': True}
    time.sleep(1.5)
    assert _status(new) == 401
    assert _status(f'{BASE}/mcp/u/amcp_{"A" * 16}_{"B" * 43}') == 401
    assert _status(f'{BASE}/mcp/u/garbage') == 401


def test_mcp_without_token(stack):
    # no cookie, no token: the cookie gate redirects
    assert _status(f'{BASE}/mcp') == 302
    # signed-in browser cookie but no token: ant-mcp demands the link
    assert _status(f'{BASE}/mcp', _cookie('alice')) == 401


def test_spoofed_identity_headers_ignored(stack):
    spoof = {'Remote-User': 'bob', 'Remote-Groups': 'admins'}
    assert requests.get(f'{BASE}/api/admin/mcp-links', headers={**_cookie('alice'), **spoof},
                        timeout=10).status_code == 403
    assert requests.get(f'{BASE}/api/admin/mcp-links', headers=spoof, timeout=10).status_code == 401
    assert requests.get(f'{BASE}/api/admin/mcp-links', headers=_cookie('bob'), timeout=10).status_code == 200
    # spoofed admin group on the MCP path does not unlock admin tools
    url = _rotate('alice')['url']
    tools, _ = asyncio.run(_session_call(url, headers=spoof))
    assert 'rescan_databases' not in tools
    # a client-supplied internal secret header is stripped, not honoured
    response = requests.get(f'{BASE}/api/health', headers={**_cookie('alice'), 'X-Anthive-Internal': 'x'},
                            timeout=10)
    assert response.status_code == 200


def test_internal_endpoints_blocked_from_outside(stack):
    for headers in ({}, _cookie('bob')):
        response = requests.post(f'{BASE}/api/internal/mcp/verify', json={'token': 'x'},
                                 headers=headers, timeout=10)
        assert response.status_code in (401, 403) and 'user' not in response.text
    assert requests.post(f'{BASE}/api/internal/mcp/verify', json={'token': 'x'},
                         headers=_cookie('bob'), timeout=10).status_code == 403


def test_admin_rescan_needs_admin_through_proxy(stack):
    assert requests.post(f'{BASE}/api/admin/rescan', headers=_cookie('alice'), timeout=60).status_code == 403
    assert requests.post(f'{BASE}/api/admin/rescan', headers=_cookie('bob'), timeout=120).status_code == 200


def test_tokens_redacted_in_access_log(stack):
    url = _rotate('alice')['url']
    token = url.rsplit('/', 1)[1]
    _status(url)
    asyncio.run(_session_call(f'{BASE}/mcp', headers={'Authorization': f'Bearer {token}'}))
    time.sleep(1)
    log_text = (stack['work'] / 'access.log').read_text()
    assert 'amcp_REDACTED' in log_text
    assert token not in log_text
    assert token.split('_', 2)[2] not in log_text


def test_secrets_created_with_0600(stack):
    secrets_dir = stack['state_dir'] / 'secrets'
    for name in ('internal_secret', 'mcp_token_secret'):
        assert (secrets_dir / name).stat().st_mode & 0o777 == 0o600
    db_bytes = (stack['state_dir'] / 'mcp_tokens.db').read_bytes()
    assert b'amcp_' not in db_bytes
