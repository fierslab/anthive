"""The dataset journal routes of ant-serve (REST 2.49.0, doc/DESIGN_dataset_journal.md), over HTTP.

ant-serve is a single-file uv script with heavy imports, so the journal definitions and the access
middleware are lifted out of its AST (the approach of tests/test_artefacts_api.py) and run against a fake
registry: one public dataset, one internal dataset, a collection and an artefact policy stub.
"""
from __future__ import annotations

import ast
import hmac
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import dataset_access

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
INTERNAL = 'internal-test-secret'

_HELPERS = {
    'UnknownDatabaseError', 'unknown_database_detail', '_HIDDEN_DB_IDS', '_hidden_db_ids',
    '_internal_secret_value', '_internal_secret', '_has_valid_internal_secret',
    '_is_local_tool_request', '_require_admin', '_caller_is_admin',
    '_ACCESS_SETTINGS', '_access_settings', '_caller_sees_internal', '_request_path', '_DatasetAccessGuard',
}

USER = {'Remote-User': 'alice', 'Remote-Groups': 'users'}
BOB = {'Remote-User': 'bob', 'Remote-Groups': 'users'}
CURATOR = {'Remote-User': 'carol', 'Remote-Groups': 'curators'}
ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'admins'}
OWNER = {'Remote-User': 'dora', 'Remote-Groups': 'users'}


class FakeRegistry:
    def __init__(self, namespace):
        self._ns = namespace
        self._databases_all = {'S26/SLM': '/x/SLM.duckdb', 'S99/SEC': '/x/SEC.duckdb'}
        self._metadata_all = {'S26/SLM': {'version': '1.2.0'}, 'S99/SEC': {'version': '1.0.0'}}
        self._artefact_index = None

    @property
    def databases(self):
        hidden = self._ns['_HIDDEN_DB_IDS'].get()
        return {k: v for k, v in self._databases_all.items() if k not in hidden}

    def internal_db_ids(self):
        return frozenset({'S99/SEC'})

    def collection_registry(self):
        return {'S26': object()}

    def artefact_index(self):
        class Index:
            entries: Dict[Any, Any] = {}

            def get(self, *parts):
                return None
        return Index()


class FakePolicy:
    def collection_visible(self, group_id, viewer):
        return group_id == 'S26'

    def artefact_visible(self, entry, viewer):
        return False


def _serve(tmp_path: Path, auth_mode: str = 'authelia', journal: Optional[dict] = None) -> TestClient:
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if name and (name in _HELPERS or name.startswith('_journal') or name.startswith('_JOURNAL')
                     or name.startswith('Journal') or 'journal' in name):
            body.append(node)
    app = FastAPI()
    cfg = {'internal_secret': INTERNAL, 'state_dir': str(tmp_path), 'auth': {'mode': auth_mode},
           'journal': {'writes_per_minute': 0, **(journal or {})}}
    import config as config_module
    namespace: Dict[str, Any] = {
        'app': app, 'get_config': lambda: cfg, 'Request': Request, 'HTTPException': HTTPException,
        'JSONResponse': JSONResponse, 'RedirectResponse': RedirectResponse, 'FileResponse': FileResponse,
        'Depends': Depends, 'Query': Query, 'Body': Body, 'BaseModel': BaseModel, 'Field': Field,
        'hmac': hmac, 'Lock': Lock, 'Optional': Optional, 'Dict': Dict, 'List': List, 'Any': Any,
        'Tuple': Tuple, 'Path': Path, 'contextmanager': contextmanager, 'time': time,
        'log': logging.getLogger('test-journal-api'),
        'get_auth_settings': lambda cfg_arg=None: {'mode': auth_mode, 'admins': set()},
        'get_internal_requires_auth': lambda cfg_arg=None: True,
        'dataset_access': dataset_access,
        '_artefact_policy': lambda: FakePolicy(), '_artefact_viewer': lambda request: None,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    namespace['registry'] = FakeRegistry(namespace)
    namespace['_journal_settings'] = lambda: config_module.get_journal_settings(cfg)
    namespace['_journal_owner'] = lambda db_id: 'dora' if db_id == 'S26/SLM' else None
    namespace['_journal_depositor'] = lambda db_id, version: None
    app.add_middleware(namespace['_DatasetAccessGuard'])
    client = TestClient(app, client=('203.0.113.9', 50000))
    client.namespace = namespace
    return client


@pytest.fixture
def client(tmp_path):
    return _serve(tmp_path)


def _post(client, path, json, headers):
    return client.post(path, json=json, headers=headers)


def test_status_route(client):
    answer = client.get('/journal/status', headers=CURATOR).json()
    assert answer['enabled'] and answer['notes'] and answer['may_triage'] and not answer['is_admin']
    assert 'data-error' in answer['actionable_categories']
    assert client.get('/journal/status').json()['signed_in'] is False


def test_note_lifecycle_over_http(client):
    made = _post(client, '/databases/S26/SLM/journal',
                 {'category': 'data-error', 'body': 'sex labels swapped in donor 3',
                  'refs': {'column': 'ant_sex'}, 'links': [{'url': 'https://eln.example.org/9', 'type': 'eln'}]},
                 USER)
    assert made.status_code == 201, made.text
    entry = made.json()
    assert entry['dataset_version'] == '1.2.0' and entry['status'] == 'open'
    listed = client.get('/databases/S26/SLM/journal', headers=BOB).json()
    assert [item['id'] for item in listed['entries']] == [entry['id']]
    reply = _post(client, f"/journal/entries/{entry['id']}/replies", {'category': 'comment', 'body': 'seen it too'},
                  BOB)
    assert reply.status_code == 201 and reply.json()['parent_id'] == entry['id']
    assert reply.json()['dataset_version'] == '1.2.0'      # replies are pinned to the dataset version too
    assert listed['may_set_status'] is False
    assert client.get('/databases/S26/SLM/journal', headers=OWNER).json()['may_set_status'] is True
    assert client.get('/databases/S26/SLM/journal', headers=CURATOR).json()['may_set_status'] is True
    assert client.patch(f"/journal/entries/{entry['id']}", json={'body': 'x'}, headers=BOB).status_code == 403
    assert client.patch(f"/journal/entries/{entry['id']}", json={'body': 'donor 3 and 4'},
                        headers=USER).json()['body'] == 'donor 3 and 4'
    assert _post(client, f"/journal/entries/{entry['id']}/status", {'status': 'acknowledged'}, BOB).status_code == 403
    assert _post(client, f"/journal/entries/{entry['id']}/status", {'status': 'acknowledged'},
                 CURATOR).json()['status'] == 'acknowledged'
    refused = _post(client, f"/journal/entries/{entry['id']}/status", {'status': 'fixed'}, OWNER)
    assert refused.status_code == 400 and refused.json()['status'] == 'changelog_required'
    fixed = _post(client, f"/journal/entries/{entry['id']}/status",
                  {'status': 'fixed', 'changelog_ref': '1.2.1: sex corrected', 'fixed_in_version': '1.2.1'}, OWNER)
    assert fixed.json()['status'] == 'fixed'   # the dataset's owner may close it
    history = client.get(f"/journal/entries/{entry['id']}/history", headers=USER).json()['history']
    assert [item['action'] for item in history] == ['edit', 'status:acknowledged', 'status:fixed']
    assert _post(client, f"/journal/entries/{entry['id']}/hide", {'reason': 'x'}, USER).status_code == 403
    assert _post(client, f"/journal/entries/{entry['id']}/hide", {'reason': 'dup'}, ADMIN).json()['hidden']
    assert client.get(f"/journal/entries/{entry['id']}", headers=USER).status_code == 404
    assert client.get(f"/journal/entries/{entry['id']}/history", headers=USER).status_code == 200
    hidden_edit = client.patch(f"/journal/entries/{entry['id']}", json={'body': 'again'}, headers=USER)
    assert hidden_edit.status_code == 409 and hidden_edit.json()['status'] == 'hidden'


def test_sign_in_needed_and_internal_datasets_hidden(client):
    assert client.get('/databases/S26/SLM/journal').status_code == 401
    assert _post(client, '/databases/S26/SLM/journal', {'category': 'comment', 'body': 'x'}, {}).status_code == 401
    # an internal dataset: signed-in users see it (internal_requires_auth), its journal works
    assert client.get('/databases/S99/SEC/journal', headers=USER).status_code == 200
    # an anonymous caller gets 404 from the access guard before the journal is asked
    assert client.get('/databases/S99/SEC/journal/downloads').status_code == 404
    assert client.get('/databases/NOPE/X/journal', headers=USER).status_code == 404


def test_generic_targets_and_open_issues(client):
    assert _post(client, '/journal', {'target_type': 'collection', 'target_id': 'S26', 'category': 'data-request',
                                     'body': 'please add the mouse cohort'}, USER).status_code == 201
    assert _post(client, '/journal', {'target_type': 'collection', 'target_id': 'S77', 'category': 'comment',
                                     'body': 'x'}, USER).status_code == 404
    assert _post(client, '/journal', {'target_type': 'artefact', 'target_id': 'S26/SLM/fig', 'category': 'comment',
                                     'body': 'x'}, USER).status_code == 404
    assert _post(client, '/journal', {'target_type': 'facet', 'target_id': 'sex', 'category': 'question',
                                     'body': 'why is S26 excluded?'}, USER).status_code == 201
    issues = client.get('/journal/open', headers=BOB).json()
    assert issues['counts'] == {'data-request': 1}
    listed = client.get('/journal', params={'target_type': 'facet', 'target_id': 'sex'}, headers=BOB).json()
    assert listed['entries'][0]['category'] == 'question'


def test_declared_use_and_download_counts(client):
    used = _post(client, '/databases/S26/SLM/journal/use',
                 {'note': 'WGCNA', 'tool': 'vark 0.0.30', 'links': [{'url': 'https://eln.example.org/2', 'type': 'eln'}]},
                 USER)
    assert used.status_code == 200 and used.json()['category'] == 'used-in-analysis'
    service = client.namespace['_journal_service']()
    service.record_download('dataset', 'S26/SLM', 'h5ad', 'alice', '1.2.0', 'SLM.h5ad')
    service.record_download('dataset', 'S26/SLM', 'h5ad', '', '1.2.0', 'SLM.h5ad')
    plain = client.get('/databases/S26/SLM/journal/downloads', headers=BOB).json()
    assert (plain['n_downloads'], plain['n_people'], plain['names_visible']) == (2, 1, False)
    assert 'rows' not in plain
    assert client.get('/databases/S26/SLM/journal/downloads', headers=OWNER).json()['rows'][0]['user'] is None
    assert len(client.get('/databases/S26/SLM/journal/downloads', headers=ADMIN).json()['rows']) == 2


def test_version_events_from_the_catalog(client):
    namespace = client.namespace
    namespace['_journal_sync'](force=True)           # baseline: no events
    namespace['registry']._metadata_all = {'S26/SLM': {'version': '1.3.0'}, 'S99/SEC': {'version': '1.0.0'}}
    namespace['_journal_sync']()
    events = client.get('/databases/S26/SLM/journal', params={'kind': 'event'}, headers=USER).json()['entries']
    assert [(item['category'], item['dataset_version']) for item in events] == [('version-published', '1.3.0')]


def test_download_hook_never_raises(client):
    namespace = client.namespace

    class Boom:
        def record_download(self, *args, **kwargs):
            raise RuntimeError('disk full')
    namespace['_journal_service_instance'] = Boom()
    request = Request({'type': 'http', 'headers': [], 'method': 'GET', 'path': '/'})
    namespace['_journal_record_download'](request, 'dataset', 'S26/SLM', 'h5ad', 'x.h5ad')


def test_no_login_site_keeps_counts_only(tmp_path):
    client = _serve(tmp_path, auth_mode='none')
    assert client.get('/journal/status').json()['notes'] is False
    answer = client.get('/databases/S26/SLM/journal', headers=USER)
    assert answer.status_code == 404 and answer.json()['status'] == 'journal_notes_off'
    service = client.namespace['_journal_service']()
    request = Request({'type': 'http', 'headers': [(b'remote-user', b'alice')], 'method': 'GET', 'path': '/'})
    client.namespace['_journal_record_download'](request, 'dataset', 'S26/SLM', 'h5ad', 'x.h5ad')
    assert service.store.download_rows('dataset', 'S26/SLM')[0]['user'] == ''   # no identity is trusted
    counts = client.get('/databases/S26/SLM/journal/downloads').json()
    assert counts['n_downloads'] == 1 and 'rows' not in counts


def test_switched_off(tmp_path):
    client = _serve(tmp_path, journal={'enabled': False})
    assert client.get('/databases/S26/SLM/journal', headers=USER).json()['status'] == 'journal_off'
    assert client.get('/databases/S26/SLM/journal/downloads').status_code == 404
    request = Request({'type': 'http', 'headers': [], 'method': 'GET', 'path': '/'})
    client.namespace['_journal_record_download'](request, 'dataset', 'S26/SLM', 'h5ad')
    assert not (tmp_path / 'journal.sqlite').exists()
