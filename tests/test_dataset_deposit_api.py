"""REST routes of dataset deposits (ant-serve, API 2.46.0): who may call them, and the answers.

The handlers are lifted out of ant-serve's AST (as in test_library_upload.py)
and wired to a real DepositService on a fake S3.
"""
from __future__ import annotations

import ast
import json
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import config
from tests.deposit_helpers import make_service, simple_deposit, upload

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'

_SERVE_WANTED = {'_require_dataset_uploader', '_deposit_call', '_caller_is_admin', '_require_admin',
                 'dataset_deposit_vocab', 'dataset_deposit_list', 'dataset_deposit_create', 'dataset_deposit_status',
                 'dataset_deposit_report', 'dataset_deposit_urls', 'dataset_deposit_complete',
                 'dataset_deposit_withdraw', 'dataset_deposit_approve', 'dataset_deposit_reject'}

CURATOR = {'Remote-User': 'alice', 'Remote-Groups': 'users,curators', 'Remote-Email': 'alice@example.org',
           'Remote-Name': 'Alice Smith'}
OTHER_CURATOR = {'Remote-User': 'bob', 'Remote-Groups': 'curators'}
ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'admins'}
USER = {'Remote-User': 'carol', 'Remote-Groups': 'users'}


def serve_app(tmp_path, monkeypatch, auth=None, cfg_extra=None):
    cfg = {'auth': auth or {'mode': 'authelia', 'admins': []}, **(cfg_extra or {})}
    monkeypatch.setattr(config, 'get_config', lambda: cfg)
    clock = [time.time()]
    service = make_service(tmp_path, clock)
    tree = ast.parse(SERVE_PATH.read_text())
    body = [node for node in tree.body if getattr(node, 'name', None) in _SERVE_WANTED]
    assert {getattr(node, 'name', None) for node in body} == _SERVE_WANTED
    app = FastAPI()
    namespace: Dict[str, Any] = {
        'app': app, 'get_config': lambda: cfg, 'Request': Request, 'HTTPException': HTTPException,
        'JSONResponse': JSONResponse, 'Body': Body, 'Query': Query, 'Optional': Optional, 'Dict': Dict,
        'List': List, 'Any': Any, 'Lock': Lock, 'Tuple': Tuple, 'Path': Path,
        'log': logging.getLogger('test-deposit'),
        'get_auth_settings': lambda cfg_arg=None: config.get_auth_settings(cfg),
        '_deposit_service': lambda: service,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return TestClient(app), service


def test_routes_are_hidden_and_tagged():
    source = SERVE_PATH.read_text()
    for method, path in (('get', '/datasets/deposit/vocab'), ('get', '/datasets/deposit'),
                         ('post', '/datasets/deposit'), ('get', '/datasets/deposit/{deposit_id}'),
                         ('get', '/datasets/deposit/{deposit_id}/report'),
                         ('post', '/datasets/deposit/{deposit_id}/urls'),
                         ('post', '/datasets/deposit/{deposit_id}/complete'),
                         ('post', '/datasets/deposit/{deposit_id}/withdraw'),
                         ('post', '/datasets/deposit/{deposit_id}/approve'),
                         ('post', '/datasets/deposit/{deposit_id}/reject')):
        assert f'@app.{method}("{path}", tags=["datasets-deposit"], include_in_schema=False)' in source
    assert source.index('"/datasets/deposit/vocab"') < source.index('"/datasets/deposit/{deposit_id}"')


@pytest.mark.parametrize('headers', [
    {},
    USER,
    {'X-Remote-User': 'alice', 'X-Remote-Groups': 'curators,admins'},   # forged, never read
    {'Remote-Groups': 'curators'},                                      # groups without a user
    {'Remote-User': '', 'Remote-Groups': 'admins'},
])
def test_no_access_without_a_signed_in_uploader(tmp_path, monkeypatch, headers):
    client, _ = serve_app(tmp_path, monkeypatch)
    for method, path in (('get', '/datasets/deposit/vocab'), ('get', '/datasets/deposit'),
                         ('post', '/datasets/deposit'), ('get', '/datasets/deposit/dep_' + 'a' * 20)):
        response = client.request(method, path, headers=headers, json={} if method == 'post' else None)
        assert response.status_code == 403, (path, headers)
        assert response.json()['status'] == 'forbidden'


def test_auth_mode_none_disables_everything(tmp_path, monkeypatch):
    client, _ = serve_app(tmp_path, monkeypatch, auth={'mode': 'none', 'admins': []})
    assert client.get('/datasets/deposit/vocab', headers=ADMIN).status_code == 403


def test_basic_mode_admins_count_as_admins(tmp_path, monkeypatch):
    client, _ = serve_app(tmp_path, monkeypatch, auth={'mode': 'basic', 'admins': ['root']})
    assert client.get('/datasets/deposit/vocab', headers={'Remote-User': 'root'}).status_code == 200
    assert client.get('/datasets/deposit/vocab', headers={'Remote-User': 'eve'}).status_code == 403


def test_upload_groups_come_from_config(tmp_path, monkeypatch):
    client, _ = serve_app(tmp_path, monkeypatch, cfg_extra={'datasets_upload_groups': ['depositors']})
    assert client.get('/datasets/deposit/vocab', headers=CURATOR).status_code == 403
    assert client.get('/datasets/deposit/vocab', headers={'Remote-User': 'x', 'Remote-Groups': 'depositors'}).status_code == 200
    assert client.get('/datasets/deposit/vocab', headers=ADMIN).status_code == 403   # admins not listed


def test_full_flow_over_http(tmp_path, monkeypatch):
    client, service = serve_app(tmp_path, monkeypatch)
    body, payloads = simple_deposit()
    response = client.post('/datasets/deposit', json=body, headers=CURATOR)
    assert response.status_code == 201, response.text
    created = response.json()
    deposit_id = created['deposit_id']
    assert service.store.get(deposit_id)['email'] == 'alice@example.org'
    assert client.get(f'/datasets/deposit/{deposit_id}', headers=OTHER_CURATOR).status_code == 404
    assert client.post(f'/datasets/deposit/{deposit_id}/complete', headers=OTHER_CURATOR).status_code == 404
    assert client.post(f'/datasets/deposit/{deposit_id}/withdraw', headers=OTHER_CURATOR).status_code == 404
    incomplete = client.post(f'/datasets/deposit/{deposit_id}/complete', headers=CURATOR)
    assert incomplete.status_code == 409 and incomplete.json()['status'] == 'incomplete'
    upload(service.s3, created, payloads)
    urls = client.post(f'/datasets/deposit/{deposit_id}/urls', json={'name': 'nope.pdf'}, headers=CURATOR)
    assert urls.status_code == 404
    done = client.post(f'/datasets/deposit/{deposit_id}/complete', headers=CURATOR)
    assert done.status_code == 200 and done.json()['state'] == 'uploaded'
    listed = client.get('/datasets/deposit', headers=CURATOR).json()['deposits']
    assert [entry['deposit_id'] for entry in listed] == [deposit_id]
    assert client.get('/datasets/deposit', headers=OTHER_CURATOR).json()['deposits'] == []
    assert client.get('/datasets/deposit?all=true', headers=OTHER_CURATOR).status_code == 403
    assert client.get('/datasets/deposit?all=true', headers=ADMIN).json()['deposits'][0]['user'] == 'alice'
    assert client.get(f'/datasets/deposit/{deposit_id}/report', headers=CURATOR).status_code == 404
    assert client.post(f'/datasets/deposit/{deposit_id}/approve', headers=CURATOR).status_code == 403
    not_held = client.post(f'/datasets/deposit/{deposit_id}/approve', headers=ADMIN, json={'note': 'x'})
    assert not_held.status_code == 409
    gone = client.post(f'/datasets/deposit/{deposit_id}/withdraw', headers=CURATOR)
    assert gone.status_code == 200 and gone.json()['state'] == 'withdrawn'


def test_errors_carry_status_words_and_retry_after(tmp_path, monkeypatch):
    client, service = serve_app(tmp_path, monkeypatch)
    service.settings.presign_per_minute = 1
    service.limiter.per_minute = 1
    body, _ = simple_deposit()
    body['files'][0]['name'] = '../../etc/passwd'
    bad = client.post('/datasets/deposit', json=body, headers=CURATOR)
    assert bad.status_code == 422 and bad.json()['status'] == 'invalid_name'
    body, _ = simple_deposit()
    assert client.post('/datasets/deposit', json=body, headers=CURATOR).status_code == 201
    body, _ = simple_deposit(group='Other2026')
    limited = client.post('/datasets/deposit', json=body, headers=CURATOR)
    assert limited.status_code == 429 and int(limited.headers['Retry-After']) > 0
    traversal = client.get('/datasets/deposit/..%2F..%2Fetc', headers=CURATOR)
    assert traversal.status_code == 404


# ── MCP: read-only deposit tools for depositors only ────────────────────────

def test_mcp_deposit_tools_only_for_depositors():
    import asyncio

    from tests.test_library_upload import _anthive_mcp_class

    cls, namespace = _anthive_mcp_class({'datasets_upload_groups': ['admins', 'curators']})
    cls.__mro__[1].tool_names = ('search_literature', 'my_deposits', 'deposit_status')
    server = cls()
    server.require_identity = True

    async def names(identity):
        token = namespace['_current_identity'].set(identity)
        try:
            return {tool.name for tool in await server.list_tools()}
        finally:
            namespace['_current_identity'].reset(token)

    async def call(identity, name):
        token = namespace['_current_identity'].set(identity)
        try:
            return await server.call_tool(name, {})
        finally:
            namespace['_current_identity'].reset(token)

    deposit_tools = {'my_deposits', 'deposit_status'}
    assert namespace['DEPOSIT_TOOLS'] == deposit_tools
    assert deposit_tools <= asyncio.run(names({'user': 'a', 'groups': ['curators']}))
    assert not deposit_tools & asyncio.run(names({'user': 'u', 'groups': ['users']}))
    assert not deposit_tools & asyncio.run(names(None))
    with pytest.raises(PermissionError):
        asyncio.run(call({'user': 'u', 'groups': ['users']}, 'my_deposits'))
    source = (REPO_ROOT / 'bin' / 'api' / 'ant-mcp').read_text()
    assert 'async def my_deposits(' in source and 'async def deposit_status(' in source
    assert 'never through MCP' in source
