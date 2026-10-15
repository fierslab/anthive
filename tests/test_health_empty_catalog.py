"""GET /health: 200 for a running server with an empty catalogue (a new site), 503 only for real failures.

ant-stack's compose healthcheck waits on /health before web, mcp and caddy start; a site whose h5adstore is
still empty must come up (it returned 503 "No databases available" up to 2.45.1).
"""
from __future__ import annotations

import ast
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

SERVE_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-serve'


class _Registry:
    def __init__(self, databases, broken=False):
        self._databases = databases
        self.broken = broken

    @property
    def databases(self):
        if self.broken:
            raise RuntimeError('registry broken')
        return self._databases

    @property
    def metadata_cache(self):
        return {key: {} for key in self._databases}

    def get_uptime(self):
        return 12.0


def client(registry):
    tree = ast.parse(SERVE_PATH.read_text())
    body = [node for node in tree.body if getattr(node, 'name', None) == 'health_check']
    app = FastAPI()
    namespace = {'app': app, 'HTTPException': HTTPException, 'registry': registry, 'API_VERSION': 'test',
                 '_timing_snapshot': lambda: {'mean_ms': None, 'p50_ms': None, 'n_samples': 0}}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return TestClient(app)


def test_an_empty_catalogue_is_healthy():
    response = client(_Registry({})).get('/health')
    assert response.status_code == 200
    assert response.json()['databases'] == 0 and response.json()['note'] == 'empty catalog'


def test_a_catalogue_with_datasets_has_no_note():
    response = client(_Registry({'G1/a': object()})).get('/health')
    assert response.status_code == 200 and response.json()['databases'] == 1 and 'note' not in response.json()


def test_real_failures_are_503():
    assert client(None).get('/health').status_code == 503
    assert client(_Registry({}, broken=True)).get('/health').status_code == 503
