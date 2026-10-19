"""ant-serve's _CrossSiteGuard (security review round 3, finding 4): state-changing requests a browser sends from
another site are refused, request bodies must be JSON (or a multipart upload), and non-browser clients (no Origin,
no Sec-Fetch-Site) are unaffected. The guard is lifted out of the single-file script with ast, as in
tests/test_artefacts_api.py, and also put in front of the real purge route harness."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

SERVE_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-serve'
WANTED = {'_UNSAFE_METHODS', '_BODY_TYPES', '_same_origin', '_CrossSiteGuard'}


def _guard_namespace(cfg: Dict[str, Any] = None) -> Dict[str, Any]:
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign):
            name = next((t.id for t in node.targets if isinstance(t, ast.Name)), None)
        if name in WANTED:
            body.append(node)
    namespace: Dict[str, Any] = {'Dict': Dict, 'JSONResponse': JSONResponse, 'get_config': lambda: cfg or {}}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec'), namespace)
    assert WANTED <= set(namespace)
    return namespace


@pytest.fixture
def client():
    app = FastAPI()

    @app.post('/thing')
    def thing(body: Any = Body(None)):
        return {'got': body}

    @app.get('/thing')
    def read():
        return {'ok': True}

    @app.post('/upload')
    def upload():
        return {'ok': True}

    namespace = _guard_namespace({'mcp': {'public_base_url': 'https://anthive.example.org'}})
    app.add_middleware(namespace['_CrossSiteGuard'])
    return TestClient(app, base_url='https://anthive.example.org')


def test_non_browser_clients_are_unaffected(client):
    assert client.post('/thing', json={'a': 1}).json() == {'got': {'a': 1}}
    assert client.post('/thing').status_code == 200                       # an empty POST (rescan, release)


def test_same_origin_browser_requests_pass(client):
    headers = {'Origin': 'https://anthive.example.org', 'Sec-Fetch-Site': 'same-origin'}
    assert client.post('/thing', json={'a': 1}, headers=headers).status_code == 200
    proxied = {'Origin': 'https://public.example', 'Host': 'api:8080', 'X-Forwarded-Host': 'public.example'}
    assert client.post('/thing', json={'a': 1}, headers=proxied).status_code == 200


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'},
    {'Sec-Fetch-Site': 'same-site'}, {'Origin': 'https://anthive.example.org.evil.example'},
])
def test_cross_site_requests_are_refused(client, headers):
    response = client.post('/thing', json={'a': 1}, headers=headers)
    assert response.status_code == 403 and response.json()['status'] == 'cross_site'
    assert client.get('/thing', headers=headers).status_code == 200      # reads stay open (CORS decides)


@pytest.mark.parametrize('content_type', [None, 'text/plain', 'application/x-www-form-urlencoded'])
def test_bodies_must_be_json(client, content_type):
    headers = {'Content-Type': content_type} if content_type else {}
    response = client.post('/thing', content=b'{"a": 1}', headers=headers)
    assert response.status_code == 415 and response.json()['status'] == 'unsupported_media_type'


def test_multipart_uploads_pass(client):
    assert client.post('/upload', files={'file': ('a.pdf', b'%PDF')}).status_code == 200


def test_the_review_probe_purge_without_content_type_is_refused(tmp_path, monkeypatch):
    """Round-3 probe: a Blob body without a type from another site was parsed as JSON and purged a version."""
    from tests.artefact_deposit_helpers import PNG
    from tests.artefact_helpers import write_artefact
    from tests.test_artefact_deposit_api import ANALYST, serve
    client, service, _s3 = serve(tmp_path, monkeypatch)
    client.app.add_middleware(_guard_namespace()['_CrossSiteGuard'])
    write_artefact(service.store_root, 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'v.png': PNG}}],
                   owner='ana')
    raw = json.dumps({'reason': 'csrf from another site', 'confirm': 'PU1/int/volcano@v1'}).encode()
    path = '/artefacts/PU1/int/volcano/v1/purge'
    assert client.post(path, headers={**ANALYST, 'Origin': 'https://evil.example'}, content=raw).status_code == 403
    assert client.post(path, headers=ANALYST, content=raw).status_code == 415
    assert client.post(path, headers={**ANALYST, 'Content-Type': 'text/plain'}, content=raw).status_code == 415
    ok = client.post(path, headers={**ANALYST, 'Content-Type': 'application/json'}, content=raw)
    assert ok.status_code == 202
