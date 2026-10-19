"""The artefact read routes of ant-serve (REST 2.47.0, doc/DESIGN_artefacts.md phase A1), over HTTP.

* access matrix: anonymous / signed-in / admin callers x public / internal x dataset, collection and mixed
  collection scopes, analysis collections, artefacts narrowed to internal, orphans; on every route;
* every ``/artefacts`` route is enumerated and must answer 404 to a caller who may not see the artefact;
* downloads: 307 to a presigned GET with no-store and no-referrer, or a verified local copy as an
  attachment with nosniff and a sandbox CSP;
* ``/groups/{group_id}`` for collections without datasets, ``artefacts_count``, ``/admin/rescan?what=artefacts``;
* the Caddy access log drops the query (signature) of a redirect's Location (needs a ``caddy`` binary).

ant-serve is a single-file uv script with heavy imports, so the definitions under test are lifted out of
its AST (the approach of tests/test_api_access.py) and run with the real access middleware.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import http.server
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.testclient import TestClient

import artefacts
import dataset_access
from metadata import parse_md_file
from tests.artefact_helpers import StrictFakeS3, write_artefact, write_dataset_md, write_index_md

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
INTERNAL = 'internal-test-secret'

ARTEFACT_ROUTES = {
    '/artefacts': 'list_artefacts',
    '/artefacts/{group_id}/{scope}/{artefact_id}': 'get_artefact',
    '/artefacts/{group_id}/{scope}/{artefact_id}/versions': 'get_artefact_versions',
    '/artefacts/{group_id}/{scope}/{artefact_id}/{version}': 'get_artefact_version',
    '/artefacts/{group_id}/{scope}/{artefact_id}/{version}/files/{name:path}': 'download_artefact_file',
    '/artefacts/{group_id}/{scope}/{artefact_id}/{version}/preview/{name:path}': 'get_artefact_preview',
    '/artefacts/{group_id}/{scope}/{artefact_id}/{version}/purge': 'purge_artefact_version',
}
#: routes not lifted into the read-route harness (tested in tests/test_artefact_deposit_api.py)
WRITE_ROUTES = {'/artefacts/{group_id}/{scope}/{artefact_id}/{version}/purge'}

_SERVE_WANTED = {
    'UnknownDatabaseError', 'unknown_database_detail', '_HIDDEN_DB_IDS', '_hidden_db_ids', '_visible_db_ids',
    'DatabaseRegistry', '_unknown_database_handler',
    '_internal_secret_value', '_internal_secret', '_has_valid_internal_secret', '_InternalSecretGuard',
    '_is_local_tool_request', '_require_admin', '_caller_is_admin', '_require_admin_or_local',
    '_ACCESS_SETTINGS', '_access_settings', '_caller_sees_internal', '_request_path', '_DatasetAccessGuard',
    'get_group_info', 'get_database_info', 'rescan_databases',
    '_artefact_service_instance', '_artefact_service_lock', '_artefact_s3_client', '_artefact_service',
    '_artefact_viewer', '_artefact_policy', '_artefact_error', '_artefact_resolve', '_artefact_count',
    *(name for path, name in ARTEFACT_ROUTES.items() if path not in WRITE_ROUTES),
}

USER = {'Remote-User': 'alice', 'Remote-Groups': 'users'}
ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'admins'}
ANON: Dict[str, str] = {}

# dataset db_id -> (folder, stem, access)
DATASETS = {
    'PU1/pub': ('Public2021', 'pub', 'public'),
    'MX0/pub': ('Mixed2020', 'pub', 'public'),
    'MX0/int': ('Mixed2020', 'int', 'internal'),
    'IN2/int': ('Inner2022', 'int', None),
}
# ref -> (folder, kind, access of the artefact, visible to anonymous callers)
ARTEFACTS = {
    'PU1/pub/fig-public': ('Public2021', 'figure', 'inherit', True),
    'PU1/pub/fig-secret': ('Public2021', 'figure', 'internal', False),
    'PU1/_group/coll-public': ('Public2021', 'table', 'inherit', True),
    'MX0/pub/fig-mx-pub': ('Mixed2020', 'figure', 'inherit', True),
    'MX0/int/fig-mx-int': ('Mixed2020', 'figure', 'inherit', False),
    'MX0/_group/mx-group': ('Mixed2020', 'table', 'inherit', False),
    'IN2/int/in-fig': ('Inner2022', 'figure', 'inherit', False),
    'IN2/_group/in-group': ('Inner2022', 'report', 'inherit', False),
    'AT6/_group/atlas-umap': ('Atlas2026', 'figure', 'inherit', True),
    'NO6/_group/notes': ('Notes2026', 'notebook', 'inherit', False),
}
ORPHAN = 'PU1/gone/orphan-fig'
FILE_BODY = b'\x89PNG artefact bytes'


def _build_store(store: Path) -> Dict[str, Any]:
    write_index_md(store, 'Public2021', group='Public2021', group_id='PU1', default_dataset='pub')
    write_index_md(store, 'Mixed2020', group='Mixed2020', group_id='MX0', default_dataset='pub')
    write_index_md(store, 'Inner2022', group='Inner2022', group_id='IN2', default_dataset='int')
    for folder, group_id, access in (('Atlas2026', 'AT6', 'public'), ('Notes2026', 'NO6', 'internal')):
        write_index_md(store, folder, group=folder, group_id=group_id, collection_kind='analysis',
                       title=f'{folder} analyses', authors=['Mark Fiers'], year=2026, access=access,
                       created_by='mfiers')
    write_index_md(store, 'Bad2026', group='Bad2026', group_id='BA6', collection_kind='analysis',
                   title='No authors', access='public')
    for db_id, (folder, stem, _access) in DATASETS.items():
        write_dataset_md(store, folder, stem, stem)
    made = {}
    for ref, (folder, kind, access, _anon) in {**ARTEFACTS, ORPHAN: ('Public2021', 'figure', 'inherit', False)}.items():
        group_id, scope, artefact_id = ref.split('/')
        made[ref] = write_artefact(store, folder, group_id, scope, kind, artefact_id,
                                   [{'files': {'a.png': FILE_BODY + b'1'}, 'access': access},
                                    {'files': {'a.png': FILE_BODY, 'panels/b.png': b'b'}}], previews=True)
    # a purged artefact of a public dataset, purged by an admin
    made['PU1/pub/gone-fig'] = write_artefact(
        store, 'Public2021', 'PU1', 'pub', 'figure', 'gone-fig',
        [{'files': {'a.png': b'x'}, 'state': 'purged', 'purged_by': 'root', 'purged_by_admin': True}])
    return made


def _registry(namespace: Dict[str, Any], store: Path):
    registry = namespace['DatabaseRegistry'].__new__(namespace['DatabaseRegistry'])
    registry.data_folder = store
    registry.databases = {db_id: str(store / f'{db_id}.duckdb') for db_id in DATASETS}
    registry.metadata_cache = {db_id: ({'title': db_id, 'access': access} if access else {'title': db_id})
                               for db_id, (_f, _s, access) in DATASETS.items()}
    registry._databases_list_cache = None
    registry._db_md_bases = {db_id: store / folder / f'{stem}.md' for db_id, (folder, stem, _a) in DATASETS.items()}
    registry._db_extra = {db_id: {'group_id': db_id.split('/')[0], 'file_id': stem,
                                  'rel_path': f'{folder}/{stem}.duckdb'}
                          for db_id, (folder, stem, _a) in DATASETS.items()}
    registry._scan_artefacts()
    return registry


def _serve(store: Path, auth_mode: str = 'authelia', requires_auth: bool = True, client_host: str = 'testclient',
           s3: Optional[StrictFakeS3] = None, settings: Optional[Dict[str, Any]] = None) -> TestClient:
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if name in _SERVE_WANTED:
            body.append(node)
    found = {getattr(node, 'name', None) for node in body} | {
        t.id for node in body if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)} | {
        node.target.id for node in body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
    assert _SERVE_WANTED <= found, _SERVE_WANTED - found
    app = FastAPI()
    cfg = {'internal_secret': INTERNAL}
    namespace: Dict[str, Any] = {
        'app': app, 'get_config': lambda: cfg, 'Request': Request, 'HTTPException': HTTPException,
        'JSONResponse': JSONResponse, 'RedirectResponse': RedirectResponse, 'FileResponse': FileResponse,
        'Depends': Depends, 'Query': Query, 'hmac': hmac, 'Lock': Lock,
        'Optional': Optional, 'Dict': Dict, 'List': List, 'Any': Any, 'Tuple': Tuple, 'Path': Path,
        'contextmanager': contextmanager, 'log': logging.getLogger('test-artefacts-api'),
        'get_auth_settings': lambda cfg_arg=None: {'mode': auth_mode, 'admins': set()},
        'get_internal_requires_auth': lambda cfg_arg=None: requires_auth,
        'dataset_access': dataset_access, 'parse_md_file': parse_md_file, 'time': time,
        'ConnectionPool': object, 'EvictedDatabaseError': RuntimeError,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    namespace['registry'] = _registry(namespace, store)
    merged = {'artefacts_download_per_minute': 1000, 'artefacts_url_ttl_seconds': 300,
              'artefacts_serve_local': 'fallback', 's3_readable': s3 is not None, 'bucket': 'labbucket',
              'catalog_prefix': '', **(settings or {})}
    namespace['_artefact_service_instance'] = artefacts.ArtefactService(
        store, merged, s3_factory=(lambda: s3) if s3 is not None else None)
    app.add_middleware(namespace['_InternalSecretGuard'])
    app.add_middleware(namespace['_DatasetAccessGuard'])
    client = TestClient(app, client=(client_host, 50000))
    client.namespace = namespace
    return client


@pytest.fixture(scope='module')
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp('artefact_store')
    made = _build_store(root)
    return root, made


@pytest.fixture(scope='module')
def client(store):
    return _serve(store[0])


def _paths(ref: str) -> Dict[str, str]:
    base = f'/artefacts/{ref}'
    return {'get': base, 'versions': f'{base}/versions', 'v1': f'{base}/v1', 'latest': f'{base}/latest',
            'file': f'{base}/v2/files/a.png', 'subfile': f'{base}/v2/files/panels/b.png',
            'preview': f'{base}/v2/preview/a.png.webp'}


def _listed(client: TestClient, headers: Dict[str, str], **params: Any) -> List[str]:
    response = client.get('/artefacts', headers=headers, params={'limit': 200, **params})
    assert response.status_code == 200, response.text
    return [row['ref'] for row in response.json()['artefacts']]


# ── route inventory ───────────────────────────────────────────────────────────

def test_every_artefact_route_is_known_and_checks_access():
    """A new /artefacts route must be added here: each resolves the artefact through _artefact_resolve
    (access on the resolved artefact) or, for the listing, filters with the access policy."""
    tree = ast.parse(SERVE_PATH.read_text())
    routes = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and getattr(decorator.func, 'attr', '') in (
                    'get', 'post', 'put', 'delete', 'patch', 'api_route'):
                path = decorator.args[0].value if decorator.args else ''
                if path.startswith('/artefacts'):
                    routes[path] = node
    assert {path: node.name for path, node in routes.items()} == ARTEFACT_ROUTES
    for path, node in routes.items():
        source = ast.unparse(node)
        if path == '/artefacts':
            assert '_artefact_policy()' in source and '_artefact_viewer(request)' in source
        elif path in WRITE_ROUTES:          # the service resolves the artefact with the caller's view
            assert 'request_purge(' in source and '_artefact_context(request)' in source
        else:
            assert '_artefact_resolve(request, group_id, scope, artefact_id)' in source, path


def test_live_routes_match_the_inventory(client):
    live = {route.path for route in client.app.routes if getattr(route, 'path', '').startswith('/artefacts')}
    assert live == set(ARTEFACT_ROUTES) - WRITE_ROUTES


# ── the access matrix ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('ref', sorted(ARTEFACTS))
@pytest.mark.parametrize('who', ['anonymous', 'user', 'admin', 'anonymous_tool'])
def test_access_matrix_on_every_route(store, client, ref, who):
    headers = {'anonymous': ANON, 'user': USER, 'admin': ADMIN, 'anonymous_tool': ANON}[who]
    caller = client
    if who == 'anonymous_tool':                    # ant-browse / ant-mcp acting for a visitor from loopback
        caller = _serve(store[0], client_host='127.0.0.1')
        headers = {'X-Anthive-Anonymous': '1'}
    visible = ARTEFACTS[ref][3] or who in ('user', 'admin')
    for route, path in _paths(ref).items():
        response = caller.get(path, headers=headers, follow_redirects=False)
        if visible:
            assert response.status_code == 200, (route, response.status_code, response.text)
        else:
            assert response.status_code == 404, (route, response.status_code)
            assert response.json() == {'status': 'not_found', 'message': response.json()['message'],
                                       'ref': response.json()['ref']}
            assert 'v2/files' not in response.text and 'manifest' not in response.text
    assert (ref in _listed(caller, headers)) is visible


def test_invisible_and_missing_artefacts_answer_alike(client):
    hidden = client.get('/artefacts/MX0/int/fig-mx-int')
    missing = client.get('/artefacts/MX0/int/fig-nothing')
    assert hidden.status_code == missing.status_code == 404
    assert set(hidden.json()) == set(missing.json()) == {'status', 'message', 'ref'}
    assert hidden.json()['message'] == 'no artefact MX0/int/fig-mx-int'
    hidden_file = client.get('/artefacts/MX0/int/fig-mx-int/v2/files/a.png')
    missing_file = client.get('/artefacts/MX0/int/fig-nothing/v2/files/a.png')
    assert hidden_file.status_code == missing_file.status_code == 404
    assert hidden_file.json()['status'] == missing_file.json()['status'] == 'not_found'


def test_the_dataset_query_parameter_cannot_reveal_an_internal_dataset(client):
    assert client.get('/artefacts', params={'db_id': 'MX0/int'}).status_code == 404        # the middleware
    assert _listed(client, USER, db_id='MX0/int') == ['MX0/int/fig-mx-int']
    assert _listed(client, ANON, group_id='MX0') == ['MX0/pub/fig-mx-pub']
    assert sorted(_listed(client, USER, group_id='MX0')) == ['MX0/_group/mx-group', 'MX0/int/fig-mx-int',
                                                            'MX0/pub/fig-mx-pub']


def test_orphans_are_for_admins_only(client):
    for headers in (ANON, USER):
        for path in _paths(ORPHAN).values():
            assert client.get(path, headers=headers, follow_redirects=False).status_code == 404
        assert ORPHAN not in _listed(client, headers, all='true')
    assert ORPHAN not in _listed(client, ADMIN)
    assert ORPHAN in _listed(client, ADMIN, all='true')
    assert client.get(f'/artefacts/{ORPHAN}', headers=ADMIN).status_code == 200


def test_forged_identity_headers_do_not_open_internal_artefacts(client):
    for headers in ({'X-Remote-User': 'alice', 'X-Remote-Groups': 'admins'},
                    {'X-Anthive-Anonymous': '1', 'Remote-User': 'alice'},
                    {'X-Anthive-Internal': 'wrong'}):
        response = client.get('/artefacts/MX0/int/fig-mx-int', headers=headers)
        assert response.status_code in (401, 404), headers
    assert client.get('/artefacts/MX0/int/fig-mx-int', headers={'X-Anthive-Internal': INTERNAL}).status_code == 200


def test_auth_mode_none_ignores_remote_user(store):
    client = _serve(store[0], auth_mode='none')
    assert client.get('/artefacts/MX0/int/fig-mx-int', headers=ADMIN).status_code == 404
    assert client.get(f'/artefacts/{ORPHAN}', headers=ADMIN).status_code == 404
    assert 'MX0/int/fig-mx-int' not in _listed(client, ADMIN)


def test_internal_requires_auth_off_opens_internal_scopes_like_datasets(store):
    client = _serve(store[0], requires_auth=False)
    listed = _listed(client, ANON)
    assert set(listed) == set(ARTEFACTS)
    assert client.get('/artefacts/NO6/_group/notes').status_code == 200


def test_bad_path_parts_are_plain_404s(client):
    for path in ('/artefacts/MX0/int%2F..%2Fint/fig-mx-int', '/artefacts/MX00/int/fig-mx-int',
                 '/artefacts/PU1/_x/fig-public', '/artefacts/PU1/pub/FIG-PUBLIC', '/artefacts/PU1/pub/fig-public/v0',
                 '/artefacts/PU1/pub/fig-public/V2', '/artefacts/PU1/pub/fig-public/v2/files/../v1/files/a.png',
                 '/artefacts/PU1/pub/fig-public/v2/files/A.PNG', '/artefacts/PU1/pub/fig-public/v2/files/%2e%2e/x',
                 '/artefacts/PU1/pub/fig-public/v2/files/panels', '/artefacts/PU1/pub/fig-public/v2/preview/a.png',
                 '/artefacts/PU1/pub/fig-public/v2/files/a.png%00', '/artefacts/PU1/pub/fig-public/v2/files/'):
        response = client.get(path, headers=ADMIN, follow_redirects=False)
        assert response.status_code == 404, (path, response.status_code)


# ── payloads ──────────────────────────────────────────────────────────────────

def test_detail_versions_and_manifest_payloads(client):
    detail = client.get('/artefacts/PU1/pub/fig-public').json()
    assert detail['ref'] == 'PU1/pub/fig-public' and detail['latest'] == 2 and detail['db_id'] == 'PU1/pub'
    assert detail['manifest']['ref'] == 'PU1/pub/fig-public@v2' and detail['manifest']['version'] == 2
    assert [v['version'] for v in detail['versions']] == [1, 2]
    assert {f['name']: f['download'] for f in detail['files']} == {
        'a.png': '/artefacts/PU1/pub/fig-public/v2/files/a.png',
        'panels/b.png': '/artefacts/PU1/pub/fig-public/v2/files/panels/b.png'}
    assert detail['files'][0]['preview'] == '/artefacts/PU1/pub/fig-public/v2/preview/a.png.webp'
    assert 'http' not in str(detail['files'])                       # API paths, never signed URLs
    version = client.get('/artefacts/PU1/pub/fig-public/v1').json()
    assert version['ref'] == 'PU1/pub/fig-public@v1' and version['manifest']['version'] == 1
    assert client.get('/artefacts/PU1/pub/fig-public/latest').json()['version'] == 2
    assert client.get('/artefacts/PU1/pub/fig-public/v3').status_code == 404


def test_purged_versions_are_tombstones(client):
    response = client.get('/artefacts/PU1/pub/gone-fig/v1')
    assert response.status_code == 410 and response.json()['status'] == 'purged'
    assert response.json()['tombstone']['purged_by'] == 'an admin'
    admin = client.get('/artefacts/PU1/pub/gone-fig/v1', headers=ADMIN).json()
    assert admin['tombstone']['purged_by'] == 'root'
    assert client.get('/artefacts/PU1/pub/gone-fig/v1/files/a.png').status_code == 410
    versions = client.get('/artefacts/PU1/pub/gone-fig/versions').json()['versions']
    assert versions == [{'version': 1, 'state': 'purged', 'published': '2026-10-16', 'purged': '2026-10-17',
                         'purged_by': 'an admin', 'purge_reason': None}]      # anonymous: no reason
    signed = client.get('/artefacts/PU1/pub/gone-fig/versions', headers=USER).json()['versions']
    assert signed[0]['purge_reason'] == 'wrong contrast' and signed[0]['purged_by'] == 'an admin'
    assert 'PU1/pub/gone-fig' not in _listed(client, ANON)
    assert 'PU1/pub/gone-fig' in _listed(client, ANON, include_purged='true')


# ── downloads ─────────────────────────────────────────────────────────────────

def test_download_redirect_headers(store):
    root, made = store
    s3 = StrictFakeS3()
    path = made['PU1/pub/fig-public']['path']
    s3.put(artefacts.s3_key('', path.file_rel(2, 'a.png')), FILE_BODY, hashlib.sha256(FILE_BODY).hexdigest())
    client = _serve(root, s3=s3)
    response = client.get('/artefacts/PU1/pub/fig-public/v2/files/a.png', follow_redirects=False)
    assert response.status_code == 307
    assert response.headers['location'].startswith('https://s3.test/labbucket/Public2021/static/artefacts/pub/'
                                                   'figure/fig-public/v2/files/a.png?')
    assert response.headers['cache-control'] == 'no-store, private'
    assert response.headers['referrer-policy'] == 'no-referrer'
    latest = client.get('/artefacts/PU1/pub/fig-public/latest/files/a.png', follow_redirects=False)
    assert latest.status_code == 307
    # an internal artefact never reaches S3 for an anonymous caller
    calls = len(s3.calls)
    assert client.get('/artefacts/MX0/int/fig-mx-int/v2/files/a.png', follow_redirects=False).status_code == 404
    assert len(s3.calls) == calls


def test_local_download_headers(client):
    response = client.get('/artefacts/PU1/pub/fig-public/v2/files/panels/b.png')
    assert response.status_code == 200 and response.content == b'b'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert 'filename="b.png"' in response.headers['content-disposition']
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['content-security-policy'] == 'sandbox'
    assert response.headers['content-type'] == 'image/png'
    preview = client.get('/artefacts/PU1/pub/fig-public/v2/preview/a.png.webp')
    assert preview.status_code == 200 and preview.headers['content-type'] == 'image/webp'
    assert preview.headers['content-disposition'] == 'inline'
    assert preview.headers['x-content-type-options'] == 'nosniff'


def test_download_not_on_this_site(store, tmp_path):
    root, made = store
    copy = tmp_path / 'store'
    shutil.copytree(root, copy, symlinks=True)
    os.remove(copy / str(made['PU1/pub/fig-public']['path'].file_rel(2, 'a.png')))
    client = _serve(copy)
    response = client.get('/artefacts/PU1/pub/fig-public/v2/files/a.png')
    assert response.status_code == 404
    assert response.json() == {'status': 'not_on_this_site', 'ref': 'PU1/pub/fig-public@v2', 'file': 'a.png',
                               'message': 'a.png of PU1/pub/fig-public@v2 is not available on this site'}


def test_download_rate_limit_answers_429(store):
    client = _serve(store[0], settings={'artefacts_download_per_minute': 2})
    path = '/artefacts/PU1/pub/fig-public/v2/files/a.png'
    assert client.get(path, headers=USER).status_code == 200
    assert client.get(path, headers=USER).status_code == 200
    response = client.get(path, headers=USER)
    assert response.status_code == 429 and response.headers['retry-after']
    assert response.json()['status'] == 'rate_limited'
    assert client.get(path, headers=ADMIN).status_code == 200            # another caller


# ── collections and counts ────────────────────────────────────────────────────

def test_analysis_collection_without_datasets(client):
    response = client.get('/groups/AT6')
    assert response.status_code == 200
    body = response.json()
    assert body['datasets'] == [] and body['collection_kind'] == 'analysis' and body['group'] == 'Atlas2026'
    assert body['title'] == 'Atlas2026 analyses' and body['access'] == 'public' and body['artefacts_count'] == 1
    assert body['authors'] == ['Mark Fiers'] and body['default_dataset'] is None
    assert client.get('/groups/NO6').status_code == 404
    assert client.get('/groups/NO6', headers=USER).json()['access'] == 'internal'
    assert client.get('/groups/BA6', headers=ADMIN).status_code == 404       # incomplete index.md: left out
    assert client.get('/groups/XX9', headers=ADMIN).status_code == 404


def test_dataset_collections_keep_their_rules(store, tmp_path):
    client = _serve(store[0])
    assert client.get('/groups/IN2').status_code == 404                      # every dataset internal
    assert client.get('/groups/IN2', headers=USER).status_code == 200
    # a dataset collection whose datasets are all inactive is still a 404, also for admins
    copy = tmp_path / 'store'
    shutil.copytree(store[0], copy, symlinks=True)
    write_index_md(copy, 'Retired2019', group='Retired2019', group_id='RE9')
    retired = _serve(copy)
    assert 'RE9' in retired.namespace['registry'].collection_registry()
    assert retired.get('/groups/RE9', headers=ADMIN).status_code == 404
    anonymous = client.get('/groups/MX0').json()
    assert [d['db_id'] for d in anonymous['datasets']] == ['MX0/pub']
    assert anonymous['collection_kind'] == 'datasets' and anonymous['artefacts_count'] == 1
    assert 'title' not in anonymous and 'access' not in anonymous
    assert client.get('/groups/MX0', headers=USER).json()['artefacts_count'] == 3
    assert client.get('/groups/PU1').json()['artefacts_count'] == 2          # fig-public, coll-public


def test_dataset_info_counts_visible_artefacts(client):
    assert client.get('/databases/PU1/pub/info').json()['artefacts_count'] == 1
    assert client.get('/databases/PU1/pub/info', headers=USER).json()['artefacts_count'] == 2
    assert client.get('/databases/MX0/int/info').status_code == 404


def test_rescan_artefacts_only(store, tmp_path):
    root, _made = store
    copy = tmp_path / 'store'
    shutil.copytree(root, copy, symlinks=True)
    client = _serve(copy, client_host='127.0.0.1')
    write_artefact(copy, 'Public2021', 'PU1', 'pub', 'figure', 'brand-new', [{'files': {'a.png': b'n'}}])
    assert client.get('/artefacts/PU1/pub/brand-new').status_code == 404
    response = client.post('/admin/rescan', params={'what': 'artefacts'})
    assert response.status_code == 200 and response.json()['status'] == 'success'
    assert client.get('/artefacts/PU1/pub/brand-new').status_code == 200
    assert client.post('/admin/rescan', params={'what': 'everything'}).status_code == 400
    remote = _serve(copy)
    assert remote.post('/admin/rescan', params={'what': 'artefacts'}, headers=USER).status_code == 403


def test_a_broken_artefact_tree_keeps_the_previous_index(store, tmp_path, monkeypatch):
    root, _made = store
    client = _serve(root)
    registry = client.namespace['registry']
    before = registry._artefact_index

    def explode(*args, **kwargs):
        raise OSError('disk gone')
    monkeypatch.setattr(artefacts.ArtefactIndex, 'build', classmethod(lambda cls, *a, **k: explode()))
    result = registry._scan_artefacts()
    assert 'error' in result and registry._artefact_index is before


# ── Caddy: the signed URL in Location never reaches the access log ────────────

def _caddy() -> Optional[str]:
    local = REPO_ROOT / 'envs' / 'caddy' / 'caddy'
    return str(local) if local.is_file() else shutil.which('caddy')


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


SIGNED = ('https://s3.example/labbucket/G/static/artefacts/x/figure/y/v1/files/a.png'
          '?X-Amz-Credential=AKIDREAD%2F20261018&X-Amz-Expires=300&X-Amz-Signature=deadbeefSIGNATURE123')


class _RedirectingApi(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(307)
        self.send_header('Location', SIGNED)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


def test_caddy_access_log_drops_the_signature_of_a_redirect(tmp_path):
    caddy = _caddy()
    if caddy is None:
        pytest.skip('caddy not installed')
    import urllib.error
    import urllib.request
    upstream = http.server.HTTPServer(('127.0.0.1', 0), _RedirectingApi)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = _free_port()
    config = REPO_ROOT / 'config'
    env = dict(os.environ)
    env.update({
        'ANTHIVE_LISTEN': f'http://127.0.0.1:{port}', 'ANTHIVE_CADDY_BIND': '127.0.0.1',
        'ANTHIVE_TLS_SNIPPET': str(config / 'caddy-tls.prod'), 'ANTHIVE_AUTH_SNIPPET': str(config / 'caddy-auth.none'),
        'ANTHIVE_SERVERS_SNIPPET': str(config / 'caddy-servers.dev'),
        'ANTHIVE_HTTP_REDIRECT_SNIPPET': str(config / 'caddy-http-redirect.prod'),
        'ANTHIVE_AUTO_HTTPS': 'auto_https off', 'ANTHIVE_WEBAPP_ROOT': str(tmp_path),
        'ANTHIVE_CADDY_LOG_OUTPUT': 'stderr', 'ANTHIVE_DEFAULT_SNI': '',
        'ANTHIVE_API_HOST': '127.0.0.1', 'ANTHIVE_API_PORT': str(upstream.server_address[1]),
        'ANTHIVE_BROWSE_HOST': '127.0.0.1', 'ANTHIVE_BROWSE_PORT': str(upstream.server_address[1]),
        'ANTHIVE_MCP_HOST': '127.0.0.1', 'ANTHIVE_MCP_PORT': str(upstream.server_address[1]),
    })
    proc = subprocess.Popen([caddy, 'run', '--config', str(config / 'Caddyfile'), '--adapter', 'caddyfile'],
                            env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    location = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and location is None:
            try:
                opener.open(f'http://127.0.0.1:{port}/api/artefacts/G/x/y/v1/files/a.png', timeout=3)
            except urllib.error.HTTPError as error:
                if error.code == 307:
                    location = error.headers['Location']
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode()[-2000:])
                time.sleep(0.3)
        time.sleep(0.5)
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
        upstream.shutdown()
    assert location == SIGNED                                  # the client still gets the whole URL
    log = (out + err).decode()
    assert '/api/artefacts/G/x/y/v1/files/a.png' in log and '"status": 307' in log
    assert 'SIGNATURE123' not in log and 'X-Amz-Signature' not in log and 'AKIDREAD' not in log
    assert 'files/a.png?REDACTED' in log
