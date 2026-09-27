"""Personal MCP links: token store, ant-serve endpoints, ant-mcp middleware.

The ant-serve endpoints and the ant-mcp middleware live in single-file uv
scripts with heavy module-level imports, so the definitions under test are
lifted out of their AST and executed in a small namespace (same approach as
test_connection_pool.py).
"""
from __future__ import annotations

import ast
import asyncio
import collections
import contextvars
import hmac
import json
import logging
import sqlite3
import time
import types
import config
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import mcp_tokens
from mcp_tokens import McpLinkStore, looks_like_token

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
MCP_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-mcp'


# ── token store ─────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    link_store = McpLinkStore(tmp_path / 'links.db', 'server-secret', ttl_days=90)
    yield link_store
    link_store.close()


def test_token_format_and_verify(store):
    record = store.rotate('alice', ['users'])
    token = store.token_for(record)
    assert token.startswith('amcp_') and looks_like_token(token)
    verified = store.verify(token, '10.0.0.1')
    assert verified.user == 'alice' and verified.groups == ['users']
    assert store.get('alice').last_ip == '10.0.0.1'


def test_link_is_stable_until_rotated(store):
    first = store.token_for(store.rotate('alice'))
    assert store.token_for(store.get('alice')) == first      # re-displayable


def test_rotate_kills_old_token_immediately(store):
    old = store.token_for(store.rotate('alice'))
    new = store.token_for(store.rotate('alice'))
    assert old != new
    assert store.verify(old) is None
    assert store.verify(new).user == 'alice'


def test_revoke(store):
    token = store.token_for(store.rotate('alice'))
    assert store.revoke('alice') is True
    assert store.verify(token) is None
    assert store.revoke('nobody') is False
    assert not store.get('alice').is_active


def test_rotate_after_revoke_reactivates(store):
    store.rotate('alice')
    store.revoke('alice')
    token = store.token_for(store.rotate('alice'))
    assert store.verify(token).user == 'alice'


def test_expiry(store, tmp_path):
    token = store.token_for(store.rotate('alice'))
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
    con = sqlite3.connect(tmp_path / 'links.db')
    con.execute("UPDATE mcp_links SET expires_at = ? WHERE user = 'alice'", [past])
    con.commit()
    con.close()
    assert store.verify(token) is None


def test_ttl_zero_never_expires(tmp_path):
    link_store = McpLinkStore(tmp_path / 'x.db', 's', ttl_days=0)
    record = link_store.rotate('alice')
    assert record.expires_at is None
    assert link_store.verify(link_store.token_for(record)).user == 'alice'


@pytest.mark.parametrize('mutate', [
    lambda t: t[:-1] + ('A' if t[-1] != 'A' else 'B'),     # MAC tampered
    lambda t: t.replace('amcp_', 'amcx_'),                 # prefix
    lambda t: t + 'x',                                     # length
    lambda t: '',
    lambda t: 'amcp_' + 'A' * 16 + '_' + 'B' * 43,         # unknown public id
])
def test_tampered_tokens_rejected(store, mutate):
    token = store.token_for(store.rotate('alice'))
    assert store.verify(mutate(token)) is None


def test_other_users_public_id_with_own_mac_rejected(store):
    alice = store.token_for(store.rotate('alice'))
    bob = store.token_for(store.rotate('bob'))
    forged = alice[:5 + 16] + bob[5 + 16:]
    assert store.verify(forged) is None


def test_different_secret_invalidates(tmp_path):
    first = McpLinkStore(tmp_path / 'y.db', 'secret-one')
    token = first.token_for(first.rotate('alice'))
    first.close()
    second = McpLinkStore(tmp_path / 'y.db', 'secret-two')
    assert second.verify(token) is None


def test_database_leak_contains_no_token(store, tmp_path):
    token = store.token_for(store.rotate('alice'))
    mac = token.split('_', 2)[2]
    raw = (tmp_path / 'links.db').read_bytes()
    wal = tmp_path / 'links.db-wal'
    if wal.exists():
        raw += wal.read_bytes()
    assert mac.encode() not in raw


def test_describe_and_admin_rows(store):
    record = store.rotate('alice')
    body = mcp_tokens.describe(record, store, 'https://h.example', 'alice', True)
    assert body['has_link'] and body['url'].startswith('https://h.example/mcp/u/amcp_')
    assert body['url'] in body['snippets']['claude_code']
    assert body['url'] in body['snippets']['json_config']
    assert json.loads(body['snippets']['json_config'])['mcpServers']['anthive']['url'] == body['url']
    assert set(body) == {'enabled', 'user', 'has_link', 'url', 'created', 'rotated_at',
                         'expires_at', 'last_used', 'ttl_days', 'snippets'}
    disabled = mcp_tokens.describe(record, store, 'https://h', 'alice', False)
    assert disabled['enabled'] is False and disabled['url'] is None
    assert mcp_tokens.admin_row(record)['user'] == 'alice'


# ── ant-serve endpoints ─────────────────────────────────────────────────────

_SERVE_WANTED = {
    '_internal_secret_value', '_internal_secret', '_has_valid_internal_secret',
    '_InternalSecretGuard', '_is_local_tool_request', '_require_admin_or_local',
    '_mcp_link_store', '_mcp_link_store_lock', '_mcp_cfg', '_get_mcp_link_store',
    '_request_groups', '_mcp_public_base_url', '_mcp_link_body',
    'get_my_mcp_link', 'rotate_my_mcp_link', 'revoke_my_mcp_link', 'list_mcp_links',
    'admin_revoke_mcp_link', 'McpVerifyRequest', 'verify_mcp_link',
    '_chat_user_from_request', '_require_admin',
}

INTERNAL = 'internal-test-secret'


def _serve_app(tmp_path, monkeypatch, mcp_cfg: Optional[dict] = None):
    monkeypatch.setenv('ANTHIVE_STATE_DIR', str(tmp_path / 'state'))
    cfg = {'internal_secret': INTERNAL,
           'mcp': {'enabled': True, 'token_ttl_days': 90, 'token_secret': 'tok-secret',
                   **(mcp_cfg or {})}}
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if name in _SERVE_WANTED:
            body.append(node)
    app = FastAPI()
    namespace: Dict[str, Any] = {
        'app': app, 'get_config': lambda: cfg, 'Request': Request, 'HTTPException': HTTPException,
        'JSONResponse': JSONResponse, 'BaseModel': BaseModel, 'Field': Field, 'Depends': Depends,
        'hmac': hmac, 'Lock': Lock, 'Optional': Optional, 'Dict': Dict, 'List': List, 'Any': Any,
        'log': logging.getLogger('test-mcp-links'),
        'get_auth_settings': lambda cfg_arg=None: config.get_auth_settings(cfg),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    app.add_middleware(namespace['_InternalSecretGuard'])

    @app.post('/admin/rescan', dependencies=[Depends(namespace['_require_admin_or_local'])])
    def fake_rescan():
        return {'ok': True}

    return app, namespace


def _client(app, host='127.0.0.1'):
    return TestClient(app, client=(host, 50000))


ALICE = {'Remote-User': 'alice', 'Remote-Groups': 'users', 'X-Forwarded-For': '1.2.3.4',
         'X-Forwarded-Host': 'anthive.example.org', 'X-Forwarded-Proto': 'https'}
ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'users,admins', 'X-Forwarded-For': '1.2.3.4'}


def test_me_mcp_requires_user(tmp_path, monkeypatch):
    app, _ = _serve_app(tmp_path, monkeypatch)
    assert _client(app).get('/me/mcp').status_code == 401
    assert _client(app).post('/me/mcp/rotate').status_code == 401


def test_me_mcp_lifecycle(tmp_path, monkeypatch):
    app, _ = _serve_app(tmp_path, monkeypatch)
    client = _client(app)
    empty = client.get('/me/mcp', headers=ALICE).json()
    assert empty['enabled'] and not empty['has_link'] and empty['url'] is None
    assert empty['ttl_days'] == 90
    issued = client.post('/me/mcp/rotate', headers=ALICE).json()
    assert issued['has_link'] and issued['url'].startswith('https://anthive.example.org/mcp/u/amcp_')
    again = client.get('/me/mcp', headers=ALICE).json()
    assert again['url'] == issued['url']                     # re-display, same link
    rotated = client.post('/me/mcp/rotate', headers=ALICE).json()
    assert rotated['url'] != issued['url'] and rotated['rotated_at']
    assert client.delete('/me/mcp', headers=ALICE).json() == {'ok': True}
    assert client.get('/me/mcp', headers=ALICE).json()['has_link'] is False


def test_public_base_url_config_wins(tmp_path, monkeypatch):
    app, _ = _serve_app(tmp_path, monkeypatch, {'public_base_url': 'https://cfg.example/'})
    body = _client(app).post('/me/mcp/rotate', headers=ALICE).json()
    assert body['url'].startswith('https://cfg.example/mcp/u/')


def test_disabled_feature(tmp_path, monkeypatch):
    app, _ = _serve_app(tmp_path, monkeypatch, {'enabled': False})
    client = _client(app)
    body = client.get('/me/mcp', headers=ALICE).json()
    assert body['enabled'] is False and body['url'] is None
    assert client.post('/me/mcp/rotate', headers=ALICE).status_code == 403


def test_internal_verify_needs_secret(tmp_path, monkeypatch):
    app, _ = _serve_app(tmp_path, monkeypatch)
    client = _client(app)
    url = client.post('/me/mcp/rotate', headers=ALICE).json()['url']
    token = url.rsplit('/', 1)[1]
    assert client.post('/internal/mcp/verify', json={'token': token}).status_code == 401
    bad = client.post('/internal/mcp/verify', json={'token': token},
                      headers={'X-Anthive-Internal': 'wrong'})
    assert bad.status_code == 401
    good = client.post('/internal/mcp/verify', json={'token': token, 'client_ip': '9.9.9.9'},
                       headers={'X-Anthive-Internal': INTERNAL})
    assert good.status_code == 200 and good.json() == {'user': 'alice', 'groups': ['users']}
    nope = client.post('/internal/mcp/verify', json={'token': token[:-1] + 'x'},
                       headers={'X-Anthive-Internal': INTERNAL})
    assert nope.status_code == 401


def test_wrong_internal_secret_rejected_everywhere(tmp_path, monkeypatch):
    app, _ = _serve_app(tmp_path, monkeypatch)
    response = _client(app).get('/me/mcp', headers={**ALICE, 'X-Anthive-Internal': 'guess'})
    assert response.status_code == 401


def test_groups_snapshot_refreshed_on_get(tmp_path, monkeypatch):
    app, namespace = _serve_app(tmp_path, monkeypatch)
    client = _client(app)
    client.post('/me/mcp/rotate', headers=ALICE)
    client.get('/me/mcp', headers={**ALICE, 'Remote-Groups': 'users,admins'})
    assert namespace['_get_mcp_link_store']().get('alice').groups == ['admins', 'users']


def test_admin_endpoints(tmp_path, monkeypatch):
    app, _ = _serve_app(tmp_path, monkeypatch)
    client = _client(app)
    client.post('/me/mcp/rotate', headers=ALICE)
    assert client.get('/admin/mcp-links', headers=ALICE).status_code == 403
    rows = client.get('/admin/mcp-links', headers=ADMIN).json()
    assert [r['user'] for r in rows] == ['alice'] and rows[0]['has_link']
    assert client.delete('/admin/mcp-links/alice', headers=ALICE).status_code == 403
    assert client.delete('/admin/mcp-links/alice', headers=ADMIN).json() == {'ok': True}
    assert client.delete('/admin/mcp-links/ghost', headers=ADMIN).status_code == 404
    assert client.get('/me/mcp', headers=ALICE).json()['has_link'] is False


def test_admin_or_local_guard(tmp_path, monkeypatch):
    app, _ = _serve_app(tmp_path, monkeypatch)
    # direct local tool call: loopback, no proxy headers, no user
    assert _client(app).post('/admin/rescan').status_code == 200
    # through the proxy as a normal user
    assert _client(app).post('/admin/rescan', headers=ALICE).status_code == 403
    # through the proxy as admin
    assert _client(app).post('/admin/rescan', headers=ADMIN).status_code == 200
    # a tool in another container: not loopback, internal secret, no user
    remote = _client(app, host='172.18.0.5')
    assert remote.post('/admin/rescan').status_code == 403
    assert remote.post('/admin/rescan', headers={'X-Anthive-Internal': INTERNAL}).status_code == 200
    # ant-mcp acting for a non-admin user: internal secret does not elevate
    as_user = {'X-Anthive-Internal': INTERNAL, 'Remote-User': 'alice', 'Remote-Groups': 'users'}
    assert remote.post('/admin/rescan', headers=as_user).status_code == 403


def test_secret_files_created_0600(tmp_path, monkeypatch):
    import os
    from config import get_or_create_secret
    monkeypatch.setenv('ANTHIVE_STATE_DIR', str(tmp_path / 'st'))
    value = get_or_create_secret('thing', None, None, {})
    again = get_or_create_secret('thing', None, None, {})
    assert value == again and len(value) > 40
    mode = os.stat(tmp_path / 'st' / 'secrets' / 'thing').st_mode & 0o777
    assert mode == 0o600


# ── ant-mcp middleware ──────────────────────────────────────────────────────

def _load_mcp_middleware():
    tree = ast.parse(MCP_PATH.read_text())
    body = [node for node in tree.body
            if getattr(node, 'name', None) in {'_McpLinkAuthMiddleware', '_is_admin'}
            or (isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(isinstance(t, ast.Name) and t.id in {'ADMIN_ONLY_TOOLS', '_current_identity'}
                        for t in (node.targets if isinstance(node, ast.Assign) else [node.target])))]
    namespace: Dict[str, Any] = {
        'httpx': httpx, 'json': json, 'time': time, 'collections': collections,
        'contextvars': contextvars, 'Optional': Optional,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MCP_PATH), 'exec', dont_inherit=True), namespace)
    return types.SimpleNamespace(**namespace)


GOOD = 'amcp_' + 'A' * 16 + '_' + 'G' * 43
ADMIN_TOKEN = 'amcp_' + 'B' * 16 + '_' + 'H' * 43


def _make_middleware(mod, calls, **kwargs):
    def verify_handler(request: httpx.Request):
        calls.append(json.loads(request.content))
        if request.headers.get('X-Anthive-Internal') != 'sek':
            return httpx.Response(401)
        token = json.loads(request.content)['token']
        if token == GOOD:
            return httpx.Response(200, json={'user': 'alice', 'groups': ['users']})
        if token == ADMIN_TOKEN:
            return httpx.Response(200, json={'user': 'root', 'groups': ['admins']})
        return httpx.Response(401)

    seen = []

    async def inner_app(scope, receive, send):
        seen.append({'path': scope['path'], 'identity': (scope.get('state') or {}).get('anthive_identity'),
                     'ctx': mod._current_identity.get()})
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    client = httpx.AsyncClient(transport=httpx.MockTransport(verify_handler))
    middleware = mod._McpLinkAuthMiddleware(
        inner_app, api_base_url='http://api', internal_secret='sek', http_client=client, **kwargs)
    return middleware, seen


def _request(middleware, path, headers=None):
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        sent.append(message)

    scope = {'type': 'http', 'method': 'POST', 'path': path, 'raw_path': path.encode(),
             'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
             'client': ('5.6.7.8', 1234)}
    asyncio.run(middleware(scope, receive, send))
    status = sent[0]['status']
    body = b''.join(m.get('body', b'') for m in sent[1:])
    return status, body


def test_middleware_path_token_rewrites_and_sets_identity():
    mod = _load_mcp_middleware()
    calls = []
    middleware, seen = _make_middleware(mod, calls)
    status, _ = _request(middleware, f'/mcp/u/{GOOD}')
    assert status == 200
    assert seen[-1]['path'] == '/mcp'
    assert seen[-1]['identity'] == {'user': 'alice', 'groups': ['users']}
    assert seen[-1]['ctx'] == {'user': 'alice', 'groups': ['users']}
    assert calls[-1]['client_ip'] == '5.6.7.8'
    status, _ = _request(middleware, f'/mcp/u/{GOOD}/extra')
    assert seen[-1]['path'] == '/mcp/extra'


def test_middleware_bearer_and_forwarded_ip():
    mod = _load_mcp_middleware()
    calls = []
    middleware, seen = _make_middleware(mod, calls, cache_seconds=0)
    status, _ = _request(middleware, '/mcp', {'Authorization': f'Bearer {ADMIN_TOKEN}',
                                              'X-Forwarded-For': '8.8.8.8, 10.0.0.1'})
    assert status == 200 and seen[-1]['identity']['user'] == 'root'
    assert calls[-1]['client_ip'] == '8.8.8.8'


def test_middleware_rejects_missing_and_bad_tokens():
    mod = _load_mcp_middleware()
    middleware, seen = _make_middleware(mod, [])
    status, body = _request(middleware, '/mcp')
    assert status == 401 and b'personal link' in body
    status, body = _request(middleware, '/mcp/u/amcp_nope')
    assert status == 401 and b'rotated' in body
    status, _ = _request(middleware, '/mcp', {'Authorization': 'Bearer junk'})
    assert status == 401
    assert seen == []


def test_middleware_positive_and_negative_cache():
    mod = _load_mcp_middleware()
    calls = []
    middleware, _ = _make_middleware(mod, calls, cache_seconds=60, negative_cache_seconds=60)
    for _ in range(3):
        _request(middleware, f'/mcp/u/{GOOD}')
        _request(middleware, '/mcp/u/amcp_bad')
    assert len(calls) == 2


def test_internal_auth_headers_never_creates_secret(tmp_path, monkeypatch):
    from config import internal_auth_headers
    monkeypatch.delenv('ANTHIVE_INTERNAL_SECRET', raising=False)
    monkeypatch.setenv('ANTHIVE_STATE_DIR', str(tmp_path / 'st'))
    assert internal_auth_headers() == {}
    assert not (tmp_path / 'st' / 'secrets' / 'internal_secret').exists()
    monkeypatch.setenv('ANTHIVE_INTERNAL_SECRET', 'from-env')
    assert internal_auth_headers() == {'X-Anthive-Internal': 'from-env'}


def test_middleware_rate_limit():
    mod = _load_mcp_middleware()
    middleware, _ = _make_middleware(mod, [], rate_limit_per_minute=3)
    statuses = [_request(middleware, f'/mcp/u/{GOOD}')[0] for _ in range(5)]
    assert statuses == [200, 200, 200, 429, 429]


def test_middleware_fails_closed_when_api_down():
    mod = _load_mcp_middleware()

    def boom(request):
        raise httpx.ConnectError('down')

    async def inner_app(scope, receive, send):
        raise AssertionError('must not be reached')

    middleware = mod._McpLinkAuthMiddleware(
        inner_app, api_base_url='http://api', internal_secret='sek',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    assert _request(middleware, f'/mcp/u/{GOOD}')[0] == 401


def test_middleware_passes_non_mcp_paths():
    mod = _load_mcp_middleware()
    middleware, seen = _make_middleware(mod, [])
    status, _ = _request(middleware, '/other')
    assert status == 200 and seen[-1]['identity'] is None


def test_admin_only_tool_set():
    mod = _load_mcp_middleware()
    assert {'analytics_viz', 'rescan_databases'} <= mod.ADMIN_ONLY_TOOLS
    assert mod._is_admin({'user': 'r', 'groups': ['Admins']})
    assert not mod._is_admin({'user': 'a', 'groups': ['users']})
    assert not mod._is_admin(None)
