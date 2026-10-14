"""Who may reach what on the REST API (API 2.45.0 security fixes).

* access=internal datasets are hidden from callers without an authenticated
  identity (``lib/dataset_access.py``, site setting ``internal_requires_auth``):
  listings leave them out, their routes answer 404.
* ``X-Remote-User`` / ``X-Remote-Groups`` are never an identity: a forged
  ``X-Remote-Groups: admins`` gets 403.
* Server-control routes (caches, indexes, rescans, api-config) need an admin,
  a local tool or the internal secret.
* An unknown dataset is a 404 with one wording, also on the DE routes.
* ant-browse forwards the visitor (``Remote-User`` or ``X-Anthive-Anonymous``)
  on every server-side API call.

ant-serve is a single-file uv script with heavy imports, so the definitions
under test are lifted out of its AST (same approach as test_mcp_links.py).
"""
from __future__ import annotations

import ast
import hmac
import importlib.util
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import config
import dataset_access
from metadata import parse_md_file

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
INTERNAL = 'internal-test-secret'

PUBLIC_ID, INTERNAL_ID, NO_ACCESS_ID = 'G1/pub', 'G1/int', 'G2/none'

GUARDED_ROUTES = (
    '/admin/api-config/{db_id:path}', '/admin/plot-cache/stats', '/admin/plot-cache/clear',
    '/admin/gene-stats-cache/stats', '/admin/gene-stats-cache/clear', '/admin/gene-stats-index/stats',
    '/admin/gene-stats-index/reload', '/admin/gene-stats-index/rebuild', '/admin/gene-variance-index/stats',
    '/admin/gene-variance-index/reload', '/admin/gene-variance-index/rebuild', '/admin/gene-stats-cache/gc',
    '/admin/gene-variance-cache/stats', '/genesets/rescan', '/module-variability/rescan', '/catalogs/rescan',
    '/admin/rescan', '/admin/release/{db_id:path}', '/admin/restore/{db_id:path}', '/admin/refresh-views/{db_id:path}',
)

_SERVE_WANTED = {
    'UnknownDatabaseError', 'unknown_database_detail', '_HIDDEN_DB_IDS', '_hidden_db_ids', '_visible_db_ids',
    'DatabaseRegistry', '_unknown_database_handler',
    '_internal_secret_value', '_internal_secret', '_has_valid_internal_secret', '_InternalSecretGuard',
    '_is_local_tool_request', '_require_admin', '_require_admin_or_local', '_chat_user_from_request',
    '_ACCESS_SETTINGS', '_access_settings', '_caller_sees_internal', '_request_path', '_DatasetAccessGuard',
    'list_databases', 'get_database_info', 'get_group_info', '_pooled_db', 'list_de_studies',
    'get_de_study_summary', 'get_de_by_gene', 'get_de_study',
    'gene_stats_index_stats', 'gene_stats_cache_clear', 'plot_cache_clear', 'rescan_genesets',
    'rescan_module_variability', 'rescan_catalogs', 'gene_variance_index_stats',
}


class _FakeIndex:
    @staticmethod
    def stats() -> Dict[str, Any]:
        return {'loaded': True}


class _FakeDb:
    def __init__(self, **kwargs: Any) -> None:
        self.conn = object()


class _FakePool:
    def get_connection(self, db_url: str) -> object:
        return object()

    def return_connection(self, db_url: str, conn: object) -> None:
        pass


class _FakeDe:
    @staticmethod
    def list_studies(conn: object) -> List[Dict[str, Any]]:
        return []

    @staticmethod
    def study_summary(conn: object, study_id: str, padj_max: float = 0.05) -> None:
        return None


def _registry(namespace: Dict[str, Any], tmp_path: Path):
    registry = namespace['DatabaseRegistry'].__new__(namespace['DatabaseRegistry'])
    registry.data_folder = tmp_path
    registry.databases = {db_id: str(tmp_path / f'{db_id}.duckdb') for db_id in (PUBLIC_ID, INTERNAL_ID, NO_ACCESS_ID)}
    registry.metadata_cache = {PUBLIC_ID: {'title': 'Public', 'access': 'public'},
                               INTERNAL_ID: {'title': 'Internal', 'access': 'internal'},
                               NO_ACCESS_ID: {'title': 'No access value'}}
    registry._databases_list_cache = None
    registry.gene_stats_index = _FakeIndex()
    registry.gene_variance_index = _FakeIndex()
    registry.gene_stats_cache = type('Cache', (), {'clear': staticmethod(lambda: 0)})()
    md_bases = {}
    for db_id in (PUBLIC_ID, INTERNAL_ID, NO_ACCESS_ID):
        group, stem = db_id.split('/')
        path = tmp_path / group / f'{stem}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\ngroup_id: {group}\ntitle: {stem}\n---\n')
        md_bases[db_id] = path
    registry._db_md_bases = md_bases
    return registry


def _serve(tmp_path: Path, auth_mode: str = 'authelia', requires_auth: bool = True,
           client_host: str = 'testclient') -> TestClient:
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
    cfg = {'internal_secret': INTERNAL}
    namespace: Dict[str, Any] = {
        'app': app, 'get_config': lambda: cfg, 'Request': Request, 'HTTPException': HTTPException,
        'JSONResponse': JSONResponse, 'Depends': Depends, 'Query': Query, 'hmac': hmac, 'Lock': Lock,
        'Optional': Optional, 'Dict': Dict, 'List': List, 'Any': Any, 'Tuple': Tuple, 'Path': Path,
        'contextmanager': contextmanager, 'log': logging.getLogger('test-api-access'),
        'get_auth_settings': lambda cfg_arg=None: {'mode': auth_mode, 'admins': set()},
        'get_internal_requires_auth': lambda cfg_arg=None: requires_auth,
        'dataset_access': dataset_access, 'parse_md_file': parse_md_file,
        'connection_pool': _FakePool(), 'AnthiveDB': _FakeDb, 'EvictedDatabaseError': RuntimeError,
        'lib_de_storage': _FakeDe, 'time': __import__('time'), 'ConnectionPool': _FakePool,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    namespace['registry'] = _registry(namespace, tmp_path)
    app.add_middleware(namespace['_InternalSecretGuard'])
    app.add_middleware(namespace['_DatasetAccessGuard'])

    @app.get('/whoami-chat')
    def whoami_chat(request: Request):
        return {'user': namespace['_chat_user_from_request'](request)}

    client = TestClient(app, client=(client_host, 50000))
    client.namespace = namespace
    return client


USER = {'Remote-User': 'alice', 'Remote-Groups': 'users'}
ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'admins'}


# ── lib/dataset_access.py ─────────────────────────────────────────────────────

@pytest.mark.parametrize('metadata, expected', [
    ({'access': 'public'}, True), ({'access': 'Public '}, True), ({'access': 'internal'}, False),
    ({}, False), (None, False), ({'access': ['public']}, True), ({'access': ['public', 'internal']}, False),
])
def test_only_an_explicit_public_is_public(metadata, expected):
    assert dataset_access.is_public(metadata) is expected


@pytest.mark.parametrize('headers, peer, mode, secret, expected', [
    ({'remote-user': 'alice'}, '10.0.0.5', 'authelia', False, True),
    ({'remote-user': 'alice'}, '10.0.0.5', 'none', False, False),          # no login: Remote-User means nothing
    ({}, '10.0.0.5', 'authelia', False, False),                            # anonymous through the proxy
    ({}, '127.0.0.1', 'authelia', False, True),                            # local maintenance tool
    ({'x-forwarded-for': '1.2.3.4'}, '127.0.0.1', 'authelia', False, False),
    ({'x-anthive-anonymous': '1'}, '127.0.0.1', 'authelia', False, False),  # ant-browse for a visitor
    ({'x-anthive-anonymous': '1', 'remote-user': 'x'}, '10.0.0.5', 'authelia', False, False),
    ({}, '10.0.0.5', 'authelia', True, True),                              # tool in another container
    ({'x-remote-user': 'alice', 'x-remote-groups': 'admins'}, '10.0.0.5', 'authelia', False, False),
])
def test_caller_classification(headers, peer, mode, secret, expected):
    assert dataset_access.caller_sees_internal(headers, peer, requires_auth=True, auth_mode=mode,
                                               has_internal_secret=secret) is expected


def test_setting_off_serves_everything():
    assert dataset_access.caller_sees_internal({}, '10.0.0.5', requires_auth=False, auth_mode='none',
                                               has_internal_secret=False)


def test_paths_and_queries_naming_a_hidden_dataset():
    hidden = frozenset({'G1/int'})
    assert dataset_access.path_names_hidden('/databases/G1/int/info', hidden) == 'G1/int'
    assert dataset_access.path_names_hidden('/databases/G1/int', hidden) == 'G1/int'
    assert dataset_access.path_names_hidden('/databases/G1/intx/info', hidden) is None
    assert dataset_access.path_names_hidden('/facets/ant_sex/tests', hidden) is None
    assert dataset_access.query_names_hidden({'db_id': ['G1/int']}, hidden) == 'G1/int'
    assert dataset_access.query_names_hidden({'dataset': ['G1/pub,G1/int']}, hidden) == 'G1/int'
    assert dataset_access.query_names_hidden({'db_id': ['G1/pub']}, hidden) is None


def test_internal_requires_auth_setting(monkeypatch):
    monkeypatch.delenv('ANTHIVE_INTERNAL_REQUIRES_AUTH', raising=False)
    assert config.get_internal_requires_auth({}) is True
    assert config.get_internal_requires_auth({'internal_requires_auth': False}) is False
    monkeypatch.setenv('ANTHIVE_INTERNAL_REQUIRES_AUTH', 'false')
    assert config.get_internal_requires_auth({'internal_requires_auth': True}) is False
    monkeypatch.setenv('ANTHIVE_INTERNAL_REQUIRES_AUTH', 'yes')
    assert config.get_internal_requires_auth({'internal_requires_auth': False}) is True
    assert 'internal_requires_auth: true' in (REPO_ROOT / 'anthive.default.yaml').read_text()


# ── internal datasets: anonymous versus authenticated ─────────────────────────

def test_anonymous_listing_has_public_datasets_only(tmp_path):
    client = _serve(tmp_path)
    ids = [db['id'] for db in client.get('/databases').json()['databases']]
    assert ids == [PUBLIC_ID]
    ids = [db['id'] for db in client.get('/databases', headers=USER).json()['databases']]
    assert ids == sorted([PUBLIC_ID, INTERNAL_ID, NO_ACCESS_ID])


def test_anonymous_gets_404_on_internal_routes(tmp_path):
    client = _serve(tmp_path)
    for path in (f'/databases/{INTERNAL_ID}/info', f'/databases/{NO_ACCESS_ID}/info',
                 f'/databases/{INTERNAL_ID}/de', f'/databases/{INTERNAL_ID}/de/x/summary',
                 f'/databases/{INTERNAL_ID}/anything/else'):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()['detail'].startswith("Database 'G")
    assert client.get('/donor-variability/top', params={'db_id': INTERNAL_ID}).status_code == 404
    assert client.get(f'/databases/{PUBLIC_ID}/info').json()['title'] == 'Public'


def test_authenticated_and_local_callers_see_internal(tmp_path):
    client = _serve(tmp_path)
    assert client.get(f'/databases/{INTERNAL_ID}/info', headers=USER).json()['title'] == 'Internal'
    assert client.get(f'/databases/{INTERNAL_ID}/de', headers=USER).json() == {'studies': []}
    assert client.get(f'/databases/{INTERNAL_ID}/info', headers={'X-Anthive-Internal': INTERNAL}).status_code == 200
    local = _serve(tmp_path, client_host='127.0.0.1')
    assert local.get(f'/databases/{INTERNAL_ID}/info').status_code == 200
    assert local.get(f'/databases/{INTERNAL_ID}/info', headers={'X-Anthive-Anonymous': '1'}).status_code == 404


def test_hidden_and_unknown_datasets_look_the_same(tmp_path):
    client = _serve(tmp_path)
    hidden = client.get(f'/databases/{INTERNAL_ID}/info').json()
    unknown = client.get('/databases/G9/nope/info').json()
    assert hidden['detail'] == f"Database '{INTERNAL_ID}' not found"
    assert unknown['detail'] == "Database 'G9/nope' not found"


def test_collection_of_internal_datasets_is_hidden(tmp_path):
    client = _serve(tmp_path)
    assert client.get('/groups/G2').status_code == 404
    assert [d['db_id'] for d in client.get('/groups/G1').json()['datasets']] == [PUBLIC_ID]
    assert client.get('/groups/G2', headers=USER).status_code == 200


def test_auth_mode_none_ignores_remote_user(tmp_path):
    client = _serve(tmp_path, auth_mode='none')
    assert [db['id'] for db in client.get('/databases', headers=USER).json()['databases']] == [PUBLIC_ID]


def test_setting_off_serves_internal_to_anonymous(tmp_path):
    client = _serve(tmp_path, requires_auth=False)
    assert len(client.get('/databases').json()['databases']) == 3


def test_registry_views_filter_inside_a_request_only(tmp_path):
    client = _serve(tmp_path)
    registry = client.namespace['registry']
    assert set(registry.databases) == {PUBLIC_ID, INTERNAL_ID, NO_ACCESS_ID}     # no request: everything
    token = dataset_access.HIDDEN_DB_IDS.set(registry.internal_db_ids())
    try:
        assert set(registry.databases) == {PUBLIC_ID}
        assert set(registry.metadata_cache) == {PUBLIC_ID}
        with pytest.raises(KeyError):
            registry.get_database_path(INTERNAL_ID)
        assert dataset_access.visible([INTERNAL_ID, PUBLIC_ID]) == [PUBLIC_ID]
    finally:
        dataset_access.HIDDEN_DB_IDS.reset(token)


# ── unknown datasets: one 404 everywhere, DE included ─────────────────────────

@pytest.mark.parametrize('path', ['/databases/NOPE/X/de', '/databases/NOPE/X/de/foo/summary',
                                  '/databases/NOPE/X/de/by-gene/TREM2', '/databases/NOPE/X/de/foo',
                                  '/databases/NOPE/X/info'])
def test_unknown_dataset_is_404_not_500(tmp_path, path):
    response = _serve(tmp_path).get(path, headers=USER)
    assert response.status_code == 404
    assert response.json() == {'detail': "Database 'NOPE/X' not found"}


def test_every_unknown_dataset_404_uses_the_one_wording():
    source = SERVE_PATH.read_text()
    assert 'Database not found:' not in source
    assert not re.search(r"""detail=f["']Dataset '""", source)
    assert not re.search(r"""detail=f["']db_id \{db_id!r\} not found""", source)


# ── X-Remote-* is never an identity ──────────────────────────────────────────

def test_forged_x_remote_groups_is_not_admin(tmp_path):
    client = _serve(tmp_path)
    forged = {'Remote-User': 'bob', 'X-Remote-Groups': 'admins', 'X-Remote-User': 'root'}
    for path in ('/admin/gene-stats-index/stats', '/admin/gene-variance-index/stats'):
        assert client.get(path, headers=forged).status_code == 403
    assert client.post('/genesets/rescan', headers=forged).status_code == 403
    assert client.get('/whoami-chat', headers={'X-Remote-User': 'root'}).status_code == 401


def test_x_remote_fallback_is_gone_from_every_server():
    for path in (SERVE_PATH, REPO_ROOT / 'bin' / 'api' / 'ant-mcp', REPO_ROOT / 'web' / 'browser.py'):
        text = path.read_text()
        assert "get('X-Remote" not in text and 'get("X-Remote' not in text, path


def test_proxy_snippets_drop_x_remote_headers():
    for name in ('caddy-auth.dev', 'caddy-auth.prod', 'caddy-auth.none'):
        text = (REPO_ROOT / 'config' / name).read_text()
        assert 'request_header -X-Remote-User' in text and 'request_header -X-Remote-Groups' in text, name
    public = (REPO_ROOT / 'config' / 'examples' / 'nginx-vhost.public.conf.example').read_text()
    for header in ('Remote-User', 'Remote-Groups', 'X-Remote-User', 'X-Remote-Groups'):
        assert re.search(rf'proxy_set_header {header}\s+"";', public), header
    authelia = (REPO_ROOT / 'config' / 'examples' / 'nginx-vhost.authelia.conf.example').read_text()
    # /api/, / and the ungated /mcp location (personal MCP links)
    assert authelia.count('proxy_set_header X-Remote-Groups   "";') == 3
    assert authelia.count('proxy_set_header Remote-Groups     $auth_groups;') == 2
    mcp_block = authelia.split('location /mcp {', 1)[1].split('}', 1)[0]
    for header in ('Remote-User', 'Remote-Groups', 'Remote-Email', 'Remote-Name'):
        assert re.search(rf'proxy_set_header {header}\s+"";', mcp_block), header


# ── server-control routes need an admin, a local tool or the internal secret ──

def test_guarded_routes_carry_the_guard_in_the_source():
    source = SERVE_PATH.read_text()
    for route in GUARDED_ROUTES:
        match = re.search(r'@app\.(?:get|post)\("' + re.escape(route) + r'"[^\n]*', source)
        assert match, route
        assert 'Depends(_require_admin_or_local)' in match.group(0), route


def test_server_control_routes_refuse_users_and_anonymous(tmp_path):
    client = _serve(tmp_path)
    for method, path in (('get', '/admin/gene-stats-index/stats'), ('post', '/admin/gene-stats-cache/clear'),
                         ('post', '/admin/plot-cache/clear'), ('post', '/genesets/rescan'),
                         ('post', '/module-variability/rescan'), ('post', '/catalogs/rescan')):
        assert getattr(client, method)(path).status_code == 403, path
        assert getattr(client, method)(path, headers=USER).status_code == 403, path


def test_server_control_routes_admit_admins_local_tools_and_the_secret(tmp_path):
    client = _serve(tmp_path)
    assert client.get('/admin/gene-stats-index/stats', headers=ADMIN).json() == {'loaded': True}
    assert client.get('/admin/gene-stats-index/stats', headers={'X-Anthive-Internal': INTERNAL}).status_code == 200
    local = _serve(tmp_path, client_host='127.0.0.1')
    assert local.get('/admin/gene-stats-index/stats').status_code == 200
    assert local.get('/admin/gene-stats-index/stats', headers={'X-Anthive-Anonymous': '1'}).status_code == 403


# ── ant-browse forwards the visitor ──────────────────────────────────────────

def _browser_module():
    spec = importlib.util.spec_from_file_location('anthive_web_browser_access', REPO_ROOT / 'web' / 'browser.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browser_forwards_the_visitor_on_every_api_call(monkeypatch):
    from fastapi import APIRouter
    browser = _browser_module()
    assert any(dep.dependency is browser._bind_request_identity for dep in browser.router.dependencies)
    seen: List[Optional[Dict[str, str]]] = []

    class _Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> Dict[str, Any]:
            return {}

    def fake_get(url, timeout=None, headers=None):
        seen.append(headers)
        return _Response()

    monkeypatch.setattr(browser.requests, 'get', fake_get)
    router = APIRouter(dependencies=[Depends(browser._bind_request_identity)])

    @router.get('/page')
    def page():
        browser._api_get('http://api', '/databases')
        return {}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client.get('/page', headers={'Remote-User': 'alice'})
    client.get('/page')
    assert seen == [{'Remote-User': 'alice'}, {'X-Anthive-Anonymous': '1'}]


def test_mcp_marks_calls_without_a_link_owner_as_anonymous():
    text = (REPO_ROOT / 'bin' / 'api' / 'ant-mcp').read_text()
    assert "return {'X-Anthive-Anonymous': '1'}" in text


# ── deployment: ant-stack renders and checks the setting ─────────────────────

@pytest.fixture(scope='module')
def ant_stack():
    pytest.importorskip('bcrypt')
    import importlib.machinery
    loader = importlib.machinery.SourceFileLoader('ant_stack_access', str(REPO_ROOT / 'bin' / 'ops' / 'ant-stack'))
    spec = importlib.util.spec_from_loader('ant_stack_access', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _stack_site(tmp_path, ant_stack, auth: Dict[str, Any]):
    import yaml
    store = tmp_path / 'store'
    for stem, access in (('pub', 'public'), ('int', 'internal'), ('bare', None)):
        (store / 'G1').mkdir(parents=True, exist_ok=True)
        (store / 'G1' / f'{stem}.md').write_text('---\nid: x\n---\n')
        if access:
            (store / 'G1' / f'{stem}.h5ad.mus').write_text(f'access={access}\n')
    doc = {'site': {'name': 'unit', 'public_url': 'https://anthive.example.org:8443'},
           'data': {'h5adstore': str(store)}, 'state_dir': str(tmp_path / 'state'), 'auth': auth}
    path = tmp_path / 'site.yaml'
    path.write_text(yaml.safe_dump(doc))
    return ant_stack.load_site(path)


def test_stack_renders_internal_requires_auth(tmp_path, ant_stack):
    site = _stack_site(tmp_path, ant_stack, {'mode': 'none'})
    assert ant_stack.render_anthive_yaml(site)['internal_requires_auth'] is True
    site['auth']['internal_requires_auth'] = False
    assert ant_stack.render_anthive_yaml(site)['internal_requires_auth'] is False


def test_stack_refuses_a_non_boolean_setting(tmp_path, ant_stack):
    with pytest.raises(ant_stack.SiteError):
        _stack_site(tmp_path, ant_stack, {'mode': 'none', 'internal_requires_auth': 'maybe'})


def test_stack_counts_internal_datasets_and_warns(tmp_path, ant_stack, capsys):
    site = _stack_site(tmp_path, ant_stack, {'mode': 'none', 'internal_requires_auth': False})
    assert ant_stack._internal_dataset_count(site['_h5adstore']) == (2, 3)     # no access value counts as internal
    ant_stack._check_internal_access(site)
    assert 'WITHOUT login' in capsys.readouterr().err
    site['auth']['internal_requires_auth'] = True
    ant_stack._check_internal_access(site)
    assert 'hidden on this site' in capsys.readouterr().out
