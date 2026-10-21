"""ant-serve side of the ui switches (anthive.yaml ``ui:``).

* ``ui.api: false`` hides the interactive API docs (/docs, /redoc) from callers
  that are not signed in; the REST API and /openapi.json stay.
* ``ui.mcp: false`` reports personal MCP links as disabled whatever
  ``mcp.enabled`` says.

ant-serve is a single-file uv script with heavy imports, so the definitions
under test are lifted out of its AST (as in test_api_access.py).
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import config
import dataset_access

SERVE_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-serve'
WANTED = {'_UI_SETTINGS', '_ui_settings', '_mcp_cfg', '_API_DOC_PATHS', '_ApiDocsGuard', '_request_path',
          '_is_local_tool_request'}


def _serve(cfg: Dict[str, Any], client_host: str = 'testclient') -> TestClient:
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if name in WANTED:
            body.append(node)
    found = {getattr(node, 'name', None) or next(
        (t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name)),
        None) for node in body}
    assert found == WANTED, WANTED - found
    app = FastAPI(docs_url='/docs', redoc_url='/redoc')
    namespace: Dict[str, Any] = {
        'app': app, 'get_config': lambda: cfg, 'get_ui_settings': config.get_ui_settings, 'Request': Request,
        'JSONResponse': JSONResponse, 'Optional': Optional, 'Dict': Dict, 'Any': Any,
        'log': logging.getLogger('test-serve-ui'), 'dataset_access': dataset_access,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    app.add_middleware(namespace['_ApiDocsGuard'])

    @app.get('/databases')
    def databases():
        return {'databases': []}

    client = TestClient(app, client=(client_host, 50000))
    client.namespace = namespace
    return client


@pytest.fixture(autouse=True)
def _no_ui_env(monkeypatch):
    for name in ('FACETS', 'CAVEATS', 'API', 'MCP'):
        monkeypatch.delenv(f'ANTHIVE_UI_{name}', raising=False)


def test_docs_open_by_default():
    client = _serve({})
    for path in ('/docs', '/redoc', '/openapi.json', '/databases'):
        assert client.get(path).status_code == 200, path


def test_api_off_hides_docs_from_anonymous_callers_only():
    client = _serve({'ui': {'api': False}})
    for path in ('/docs', '/redoc', '/docs/oauth2-redirect'):
        assert client.get(path).status_code == 404, path
    # the REST API and its machine-readable contract stay
    assert client.get('/databases').status_code == 200
    assert client.get('/openapi.json').status_code == 200
    # a signed-in user (identity set by the proxy) still gets the docs
    assert client.get('/docs', headers={'Remote-User': 'alice'}).status_code == 200


def test_api_off_docs_stay_open_for_local_tools():
    client = _serve({'ui': {'api': False}}, client_host='127.0.0.1')
    assert client.get('/docs').status_code == 200
    # through the proxy (X-Forwarded-For) a loopback peer is an anonymous visitor
    assert client.get('/docs', headers={'X-Forwarded-For': '203.0.113.9'}).status_code == 404


def test_mcp_off_disables_personal_links():
    on = _serve({'mcp': {'enabled': True}})
    assert on.namespace['_mcp_cfg']()['enabled'] is True
    off = _serve({'mcp': {'enabled': True}, 'ui': {'mcp': False}})
    assert off.namespace['_mcp_cfg']()['enabled'] is False
