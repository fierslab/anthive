"""Who may deposit artefacts and create collections through the REST routes (ant-serve, REST 2.47.0).

Upload rights matrix: analysts, curators (dataset uploaders count as artefact uploaders, the design's
default), admins (always), signed-in users without a group, anonymous and forged callers; collection
creation (curators are not automatically collection creators); the artefact switch; configured groups;
the purge route. The handlers are lifted out of ant-serve's AST and wired to the real service on a fake S3.
"""
from __future__ import annotations

import ast
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import artefacts
import config
from tests.artefact_deposit_helpers import PNG, artefact_body, context, make_service, upload

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'

_SERVE_WANTED = {'_require_dataset_uploader', '_deposit_call', '_caller_is_admin',
                 '_require_admin', 'dataset_deposit_vocab', 'dataset_deposit_create', 'dataset_deposit_status',
                 'dataset_deposit_complete', 'dataset_deposit_withdraw', 'dataset_deposit_list',
                 'purge_artefact_version', '_deposit_rights', '_signed_in_identity', '_owner_call', 'artefact_purges'}

ANALYST = {'Remote-User': 'ana', 'Remote-Groups': 'users,analysts', 'Remote-Email': 'ana@example.org'}
ANALYST2 = {'Remote-User': 'otto', 'Remote-Groups': 'analysts'}
CURATOR = {'Remote-User': 'cora', 'Remote-Groups': 'curators'}
ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'admins'}
USER = {'Remote-User': 'carol', 'Remote-Groups': 'users'}
LAB = {'Remote-User': 'lena', 'Remote-Groups': 'laba'}


def serve(tmp_path, monkeypatch, cfg_extra: Optional[Dict[str, Any]] = None):
    cfg = {'auth': {'mode': 'authelia', 'admins': []}, 'artefacts_enabled': True, **(cfg_extra or {})}
    for key in [name for name in __import__('os').environ if name.startswith('ANTHIVE_ARTEFACTS')
                or name.startswith('ANTHIVE_COLLECTIONS')]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(config, 'get_config', lambda: cfg)
    clock = [time.time()]
    service, s3 = make_service(tmp_path, clock,
                               artefact_settings={'artefacts_enabled': bool(cfg['artefacts_enabled'])})
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign):
            name = next((target.id for target in node.targets if isinstance(target, ast.Name)), None)
        if name in _SERVE_WANTED:
            body.append(node)
    app = FastAPI()
    namespace: Dict[str, Any] = {
        'app': app, 'get_config': lambda: cfg, 'Request': Request, 'HTTPException': HTTPException,
        'JSONResponse': JSONResponse, 'Body': Body, 'Query': Query, 'Optional': Optional, 'Dict': Dict,
        'List': List, 'Any': Any, 'Lock': Lock, 'Tuple': Tuple, 'Path': Path, 'log': logging.getLogger('t'),
        'get_auth_settings': lambda cfg_arg=None: config.get_auth_settings(cfg),
        '_deposit_service': lambda: service,
        '_artefact_context': lambda request: context(service.store_root, artefacts.Viewer(
            sees_internal=True, user=request.headers.get('Remote-User') or '')),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return TestClient(app), service, s3


def _artefact(client, headers, artefact_id='volcano'):
    return client.post('/datasets/deposit', headers=headers,
                       json=artefact_body({'volcano.png': PNG}, artefact_id=artefact_id, scope='int',
                                          made_from=[{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]))


def _collection(client, headers, name='Fiers2026'):
    return client.post('/datasets/deposit', headers=headers, json={
        'deposit_kind': 'collection', 'data_project': name,
        'collection': {'title': 'Microglia atlas work', 'authors': ['Mark Fiers']}})


@pytest.mark.parametrize('who, headers, allowed', [
    ('analyst', ANALYST, True), ('curator', CURATOR, True), ('admin', ADMIN, True), ('user', USER, False),
    ('anonymous', {}, False), ('forged', {'X-Remote-User': 'ana', 'X-Remote-Groups': 'analysts,admins'}, False),
    ('groups without a user', {'Remote-Groups': 'analysts'}, False),
])
def test_who_may_deposit_artefacts(tmp_path, monkeypatch, who, headers, allowed):
    client, _service, _s3 = serve(tmp_path, monkeypatch)
    response = _artefact(client, headers, artefact_id=f'plot-{who.replace(" ", "-")}')
    if allowed:
        assert response.status_code == 201, response.text
        assert response.json()['deposit_kind'] == 'artefact'
    else:
        assert response.status_code == 403 and response.json()['status'] == 'forbidden'


@pytest.mark.parametrize('who, headers, allowed', [
    ('analyst', ANALYST, True), ('curator', CURATOR, False), ('admin', ADMIN, True), ('user', USER, False),
    ('anonymous', {}, False),
])
def test_who_may_create_collections(tmp_path, monkeypatch, who, headers, allowed):
    client, _service, _s3 = serve(tmp_path, monkeypatch)
    response = _collection(client, headers)
    if allowed:
        assert response.status_code == 201 and response.json()['group_id'] == 'F26', response.text
    else:
        assert response.status_code == 403


def test_analysts_may_not_deposit_datasets(tmp_path, monkeypatch):
    client, _service, _s3 = serve(tmp_path, monkeypatch)
    response = client.post('/datasets/deposit', headers=ANALYST,
                           json={'data_project': 'New2026', 'basename': 'cells', 'files': []})
    assert response.status_code == 403
    response = client.post('/datasets/deposit', headers=CURATOR,
                           json={'data_project': 'New2026', 'basename': 'cells', 'files': []})
    assert response.status_code == 503                      # past the rights check; dataset deposits are off here


def test_artefacts_switched_off_keeps_the_2_46_rules(tmp_path, monkeypatch):
    client, _service, _s3 = serve(tmp_path, monkeypatch, {'artefacts_enabled': False})
    assert _artefact(client, ANALYST).status_code == 403
    assert client.get('/datasets/deposit/vocab', headers=ANALYST).status_code == 403
    assert _collection(client, ANALYST).status_code == 403
    assert _artefact(client, ADMIN).status_code == 403       # the artefact rights exist only where artefacts are on
    assert client.get('/datasets/deposit/vocab', headers=ADMIN).status_code == 200      # 2.46: admins upload datasets


def test_configured_groups(tmp_path, monkeypatch):
    client, _service, _s3 = serve(tmp_path, monkeypatch, {'artefacts_upload_groups': ['laba'],
                                                          'collections_create_groups': ['laba'],
                                                          'datasets_upload_groups': ['curators']})
    assert _artefact(client, ANALYST, 'a1').status_code == 403
    assert _artefact(client, LAB, 'a2').status_code == 201
    assert _artefact(client, CURATOR, 'a3').status_code == 201          # dataset uploaders too
    assert _artefact(client, ADMIN, 'a4').status_code == 201            # admins always
    assert _collection(client, ANALYST).status_code == 403
    assert _collection(client, ADMIN, 'Admin2026').status_code == 201


def test_an_analyst_follows_their_own_deposit_only(tmp_path, monkeypatch):
    client, _service, s3 = serve(tmp_path, monkeypatch)
    answer = _artefact(client, ANALYST).json()
    upload(s3, answer, {'volcano.png': PNG})
    assert client.post(f'/datasets/deposit/{answer["deposit_id"]}/complete', headers=ANALYST2).status_code == 404
    response = client.post(f'/datasets/deposit/{answer["deposit_id"]}/complete', headers=ANALYST)
    assert response.status_code == 200 and response.json()['state'] == 'uploaded'
    assert client.get(f'/datasets/deposit/{answer["deposit_id"]}', headers=ANALYST2).status_code == 404
    status = client.get(f'/datasets/deposit/{answer["deposit_id"]}', headers=ANALYST).json()
    assert status['artefact']['ref'] == 'PU1/int/volcano'
    assert [entry['deposit_id'] for entry in client.get('/datasets/deposit', headers=ANALYST).json()['deposits']] == \
        [answer['deposit_id']]
    assert client.get('/datasets/deposit', headers=ANALYST2).json()['deposits'] == []
    assert client.get('/datasets/deposit?all=true', headers=ANALYST).status_code == 403
    vocab = client.get('/datasets/deposit/vocab', headers=ANALYST).json()
    assert vocab['artefacts']['uploads_enabled'] is True


def test_purge_route_rights(tmp_path, monkeypatch):
    from tests.artefact_helpers import write_artefact
    client, service, _s3 = serve(tmp_path, monkeypatch)
    write_artefact(service.store_root, 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'v.png': PNG}}],
                   owner='ana')
    body = {'reason': 'wrong contrast', 'confirm': 'PU1/int/volcano@v1'}
    path = '/artefacts/PU1/int/volcano/v1/purge'
    assert client.post(path, headers=USER, json=body).status_code == 403
    assert client.post(path, headers={}, json=body).status_code == 403
    assert client.post(path, headers=ANALYST2, json=body).status_code == 403   # not uploader, owner or admin
    assert client.post(path, headers=ANALYST, json={**body, 'confirm': 'x'}).status_code == 422
    response = client.post(path, headers=ANALYST, json=body)
    assert response.status_code == 202 and response.json()['state'] == 'purge_requested'
    assert client.post('/artefacts/PU1/int/nothing/v1/purge', headers=ADMIN, json=body).status_code == 404


def test_new_routes_are_hidden_from_the_schema():
    source = SERVE_PATH.read_text()
    assert ('@app.post("/artefacts/{group_id}/{scope}/{artefact_id}/{version}/purge", tags=["artefacts"],\n'
            '          include_in_schema=False)') in source


# ── vark findings on the branch (2.47.0) ────────────────────────────────────────

def test_a_former_uploader_may_purge_their_own_version(tmp_path, monkeypatch):
    from tests.artefact_helpers import write_artefact
    client, service, _s3 = serve(tmp_path, monkeypatch)
    write_artefact(service.store_root, 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'v.png': PNG}}],
                   owner='ana')
    body = {'reason': 'wrong contrast', 'confirm': 'PU1/int/volcano@v1'}
    former = {'Remote-User': 'ana', 'Remote-Groups': 'users'}            # left the analysts group
    assert client.post('/artefacts/PU1/int/volcano/v1/purge', headers=USER, json=body).status_code == 403
    response = client.post('/artefacts/PU1/int/volcano/v1/purge', headers=former, json=body)
    assert response.status_code == 202, response.text


def test_purge_requests_are_listed_for_the_requester_and_admins(tmp_path, monkeypatch):
    from tests.artefact_helpers import write_artefact
    client, service, _s3 = serve(tmp_path, monkeypatch)
    write_artefact(service.store_root, 'Public2021', 'PU1', 'int', 'figure', 'volcano', [{'files': {'v.png': PNG}}],
                   owner='ana')
    client.post('/artefacts/PU1/int/volcano/v1/purge', headers=ANALYST,
                json={'reason': 'wrong contrast', 'confirm': 'PU1/int/volcano@v1'})
    with service.store._lock:
        service.store.conn.execute("UPDATE artefact_purges SET state = 'needs_decision', detail = 'kept: sha differs'")
    mine = client.get('/artefacts/purges', headers=ANALYST).json()['purges']
    assert [(row['ref'], row['version'], row['state'], row['detail'], row['reason']) for row in mine] == \
        [('PU1/int/volcano', 1, 'needs_decision', 'kept: sha differs', 'wrong contrast')]
    assert client.get('/artefacts/purges', headers=ANALYST2).json()['purges'] == []
    assert len(client.get('/artefacts/purges', headers=ADMIN).json()['purges']) == 1
    assert client.get('/artefacts/purges?state=done', headers=ADMIN).json()['purges'] == []
    assert client.get('/artefacts/purges?state=bogus', headers=ADMIN).status_code == 422
    assert client.get('/artefacts/purges').status_code == 403


def test_the_vocabulary_says_what_the_caller_may_deposit(tmp_path, monkeypatch):
    client, _service, _s3 = serve(tmp_path, monkeypatch)
    analyst = client.get('/datasets/deposit/vocab', headers=ANALYST).json()
    assert analyst['caller']['may_deposit_artefacts'] is True and analyst['caller']['may_create_collections'] is True
    assert analyst['caller']['may_deposit_datasets'] is False
    assert analyst['artefacts']['may_deposit_artefacts'] is True
    curator = client.get('/datasets/deposit/vocab', headers=CURATOR).json()
    assert curator['caller']['may_deposit_artefacts'] is True and curator['caller']['may_create_collections'] is False


def test_collection_create_answers_next_as_an_object_of_routes(tmp_path, monkeypatch):
    client, _service, _s3 = serve(tmp_path, monkeypatch)
    answer = _collection(client, ANALYST).json()
    assert answer['next'] == {'status': f'/datasets/deposit/{answer["deposit_id"]}'} and answer['message']
    artefact = _artefact(client, ANALYST).json()
    assert set(artefact['next']) == {'urls', 'complete', 'status'}
