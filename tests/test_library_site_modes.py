"""Sites with a hand-picked, symlinked library (2.44.0).

Covers: papers: links to citekeys missing from the site's own library are
hidden on every surface (service, REST, web, MCP); a plain (non-git) library
of symlinked records works read-only; the library re-validates PDF presence
on status, index reload and rescan; a site without S3 never pulls the shared
literature index; ant-stack's data.extra_mounts and uv checks.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'lib'))

pytest.importorskip('yaml')
pytest.importorskip('fastapi')

import config  # noqa: E402
import library as L  # noqa: E402
import library_sync  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from library_service import LibraryService  # noqa: E402

SERVE_PATH = REPO / 'bin' / 'api' / 'ant-serve'
GHOST = 'ghost2020hidden'
GHOST_TITLE = 'Title of ghost2020hidden'


def write_record(folder: Path, citekey: str, **fields) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    data = {'entrytype': 'article', 'citekey': citekey, 'title': f'Title of {citekey}',
            'author': ['Smith, Anna'], 'date': '2024', **fields}
    path = folder / f'{citekey}.md'
    L.write_sidecar(path, data, '## Notes\n\nNotes for ' + citekey)
    return path


@pytest.fixture
def site(tmp_path):
    """A store whose collection and dataset link to one paper the library has
    and one it does not, plus the gene set store linking the missing one."""
    store = tmp_path / 'store'
    coll = store / 'Study2024'
    coll.mkdir(parents=True)
    (coll / 'index.md').write_text(
        f'---\ngroup: Study2024\ngroup_id: S24\npapers:\n  src2024: source\n  {GHOST}: source\n---\n')
    (coll / 'cells.md').write_text(f'---\nid: CEL\npapers:\n  {GHOST}: method\n  meth2023: method\n---\n')
    library = tmp_path / 'library'
    write_record(library / 'papers' / 'datasets', 'src2024', anthive_role='source',
                 related=[GHOST], relatedtype='companion')
    write_record(library / 'papers' / 'methods', 'meth2023', anthive_role='method')
    genesets = tmp_path / 'genesets' / 'study'
    genesets.mkdir(parents=True)
    (genesets / 'index.md').write_text(f'---\npapers:\n  {GHOST}: source\n---\n')
    service = LibraryService(library, tmp_path / 'index', store, tmp_path / 'genesets')
    service.set_datasets({'S24/CEL': coll / 'cells.md'})
    service.load()
    return service


# ── (4) hidden references: service ───────────────────────────────────────────

def test_service_never_names_the_missing_paper(site):
    papers = site.papers_for_dataset('S24/CEL')
    assert {p['citekey'] for p in papers} == {'src2024', 'meth2023'}
    pack = site.context('S24/CEL', {'title': 'x'})
    assert GHOST not in json.dumps(pack) and GHOST_TITLE not in json.dumps(pack)
    record = site.record(site.get('src2024'))
    assert GHOST not in json.dumps(record, default=str)
    assert 'related' not in site.get('src2024').fields
    assert GHOST not in site.bibtex(None)
    assert GHOST not in json.dumps(site.list(), default=str)
    assert GHOST not in json.dumps(site.state.papers_by_db) and GHOST not in site.state.geneset_links


def test_unresolved_only_in_admin_status(site):
    public = site.status()
    assert 'unresolved_references' not in public and GHOST not in json.dumps(public)
    assert public['issues'] == {'errors': [], 'warnings': []} and public['library_dir'] is None
    assert public['n_errors'] >= 1       # the dangling related: is counted, not named
    admin = site.status(admin=True)
    assert any(GHOST in item for item in admin['unresolved_references'])
    assert any('genesetstore' in item for item in admin['unresolved_references'])
    assert admin['library_dir']


# ── (4) hidden references: REST handlers lifted out of ant-serve ──────────────

_WANTED = {'_library', '_caller_is_admin', '_require_admin', '_csv', 'database_papers', 'library_status',
           'list_papers', 'database_context', 'get_paper', '_paper_or_404', '_upload_index_snapshot'}


def _rest_client(site: LibraryService, mode: str, admins=()) -> TestClient:
    cfg = {'auth': {'mode': mode, 'admins': list(admins)}}
    tree = ast.parse(SERVE_PATH.read_text())
    body = [node for node in tree.body if getattr(node, 'name', None) in _WANTED]
    found = {node.name for node in body}
    assert {'_library', 'library_status', 'database_papers', 'list_papers', 'database_context',
            '_caller_is_admin', '_require_admin'} <= found
    app = FastAPI()

    class Registry:
        databases = {'S24/CEL': object()}

        @staticmethod
        def get_database_metadata(db_id: str) -> Dict[str, Any]:
            return {'title': 'Cells'}

    namespace: Dict[str, Any] = {
        'app': app, 'Request': Request, 'HTTPException': HTTPException, 'Optional': Optional,
        'Dict': Dict, 'List': List, 'Any': Any, 'Lock': Lock, 'Tuple': Tuple,
        'Query': __import__('fastapi').Query, 'log': logging.getLogger('t'),
        'get_auth_settings': lambda: config.get_auth_settings(cfg),
        'LibraryService': LibraryService, 'Response': __import__('fastapi').Response,
        'FileResponse': object, '_library_service': site, 'registry': Registry, '_score_index_by_db': {},
        '_upload_index_status': {}, '_upload_index_queue': [], '_upload_index_recent': [],
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    app.get('/databases/{db_id:path}/papers')(namespace['database_papers'])
    app.get('/library/status')(namespace['library_status'])
    app.get('/papers')(namespace['list_papers'])
    app.get('/papers/{citekey}')(namespace['get_paper'])
    app.get('/databases/{db_id:path}/context')(namespace['database_context'])
    return TestClient(app)


ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'users,admins'}
USER = {'Remote-User': 'alice', 'Remote-Groups': 'users'}


@pytest.mark.parametrize('mode,headers', [('none', {}), ('none', ADMIN), ('authelia', USER),
                                          ('authelia', {}), ('basic', {'Remote-User': 'root'})])
def test_rest_hides_missing_paper_for_everyone_but_admins(site, mode, headers):
    client = _rest_client(site, mode, admins=['nobody'])
    for path in ('/databases/S24/CEL/papers', '/papers', '/papers/src2024', '/databases/S24/CEL/context',
                 '/library/status'):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, path
        assert GHOST not in response.text and GHOST_TITLE not in response.text, path
    status = client.get('/library/status', headers=headers).json()
    assert 'unresolved_references' not in status
    papers = client.get('/databases/S24/CEL/papers', headers=headers).json()['papers']
    assert {p['citekey'] for p in papers} == {'src2024', 'meth2023'}


def test_rest_admin_status_lists_unresolved(site):
    client = _rest_client(site, 'authelia')
    status = client.get('/library/status', headers=ADMIN).json()
    assert any(GHOST in item for item in status['unresolved_references'])
    basic = _rest_client(site, 'basic', admins=['root']).get('/library/status', headers={'Remote-User': 'root'})
    assert any(GHOST in item for item in basic.json()['unresolved_references'])


# ── (4) web: pages render what the API gives, and the API gives nothing about the ghost ─

def test_web_pages_never_show_the_missing_paper(site, monkeypatch):
    web_path = REPO / 'web'
    sys.path.insert(0, str(web_path))
    try:
        import browser  # noqa: PLC0415
    except Exception as exc:   # web deps not installed in this venv
        pytest.skip(f"web module not importable: {exc}")
    client = _rest_client(site, 'none')

    def fake_api_get(api_url: str, path: str):
        response = client.get(path)
        return response.json() if response.status_code == 200 else None

    monkeypatch.setattr(browser, '_api_get', fake_api_get)
    cards = browser._dataset_papers('http://api', 'S24/CEL')
    assert {card['citekey'] for card in cards} == {'src2024', 'meth2023'}
    assert GHOST not in json.dumps(cards, default=str)
    collection = browser._collection_papers('http://api', 'S24', ['CEL'])
    assert GHOST not in json.dumps(collection, default=str)


# ── (4) MCP is a pure REST pass-through ──────────────────────────────────────

def test_mcp_has_no_own_view_of_papers_links():
    source = (REPO / 'bin' / 'api' / 'ant-mcp').read_text()
    tree = ast.parse(source)
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not {'library', 'library_service', 'library_index'} & imported
    assert 'find_references' not in source and 'load_library' not in source
    assert "f'/papers/{citekey}'" in source and "f'/databases/{db_id}/context'" in source


# ── (2) a plain, symlinked, read-only library ────────────────────────────────

@pytest.fixture
def symlinked(tmp_path):
    origin = tmp_path / 'origin'
    write_record(origin / 'datasets', 'src2024', anthive_role='source', file='src2024.pdf')
    (origin / 'datasets' / 'src2024.pdf').write_bytes(b'%PDF-1.4')
    write_record(origin / 'datasets', 'other2020')
    mirror = tmp_path / 'mirror' / 'papers' / 'datasets'
    mirror.mkdir(parents=True)
    for name in ('src2024.md', 'src2024.pdf'):
        (mirror / name).symlink_to(origin / 'datasets' / name)
    store = tmp_path / 'mirror_store' / 'Study2024'
    store.mkdir(parents=True)
    (store / 'index.md').symlink_to(_write(tmp_path / 'origin_store' / 'Study2024' / 'index.md',
                                           f'---\ngroup: Study2024\ngroup_id: S24\npapers:\n  src2024: source\n  other2020: related\n---\n'))
    (store / 'cells.md').write_text('---\nid: CEL\n---\n')
    return tmp_path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_symlinked_library_loads_and_resolves_collection_links(symlinked):
    library = symlinked / 'mirror'
    assert not (library / '.git').exists()
    service = LibraryService(library, symlinked / 'idx', symlinked / 'mirror_store')
    service.set_datasets({'S24/CEL': symlinked / 'mirror_store' / 'Study2024' / 'cells.md'})
    state = service.load()
    assert set(state.papers) == {'src2024'}
    assert [p['citekey'] for p in service.papers_for_dataset('S24/CEL')] == ['src2024']
    assert service.summary(service.get('src2024'))['has_pdf'] is True
    assert service.pdf_path(service.get('src2024')).read_bytes() == b'%PDF-1.4'
    assert all(str(symlinked / 'origin') not in link.source
               for links in state.dataset_links.values() for link in links)
    assert 'other2020' not in json.dumps(service.status())


def test_dangling_symlink_is_reported_not_fatal(symlinked):
    library = symlinked / 'mirror'
    (library / 'papers' / 'datasets' / 'gone2021.md').symlink_to(symlinked / 'origin' / 'nowhere.md')
    papers, issues = L.load_library(library)
    assert set(papers) == {'src2024'}
    assert any('gone2021' in error and 'cannot read' in error for error in issues.errors)
    (symlinked / 'origin' / 'datasets' / 'src2024.pdf').unlink()
    service = LibraryService(library, symlinked / 'idx', None)
    service.load()
    assert service.summary(service.get('src2024'))['has_pdf'] is False
    assert any('src2024' in w for w in service.pdf_warnings())


def test_non_git_library_changes_are_noticed_and_git_is_never_run(symlinked, monkeypatch):
    library = symlinked / 'mirror'
    service = LibraryService(library, symlinked / 'idx', None)
    service.load()
    assert service._records_signature.startswith('fs:')
    assert service.refresh_if_changed(min_interval=0) is False
    (library / 'papers' / 'datasets' / 'other2020.md').symlink_to(symlinked / 'origin' / 'datasets' / 'other2020.md')
    assert service.refresh_if_changed(min_interval=0) is True
    import time
    deadline = time.time() + 5
    while service._reloading and time.time() < deadline:
        time.sleep(0.02)
    assert 'other2020' in service.state.papers

    def no_git(*args, **kwargs):
        raise AssertionError('git must not run in a library that is not a checkout')
    monkeypatch.setattr(library_sync, '_git', no_git)
    result = library_sync.git_sync(library, push=True)
    assert result['status'] == 'skipped' and 'not a git checkout' in result['detail']


def test_git_sync_skips_a_folder_inside_another_repository(tmp_path, monkeypatch):
    subprocess.run(['git', '-C', str(tmp_path), 'init', '-q'], check=True)
    inner = tmp_path / 'library'
    (inner / 'papers').mkdir(parents=True)
    monkeypatch.setattr(library_sync, '_git', lambda *a, **k: (_ for _ in ()).throw(AssertionError('git ran')))
    assert library_sync.git_sync(inner, push=False)['status'] == 'skipped'


# ── (2 bug) stale "file not found" warnings ───────────────────────────────────

def test_pdf_warnings_follow_the_disk_without_a_restart(tmp_path):
    library = tmp_path / 'library'
    write_record(library / 'papers' / 'datasets', 'src2024', file='src2024.pdf')
    service = LibraryService(library, tmp_path / 'idx', None)
    service.load()
    assert any('src2024' in w and 'not found' in w for w in service.status(admin=True)['issues']['warnings'])
    assert service.summary(service.get('src2024'))['has_pdf'] is False
    (library / 'papers' / 'datasets' / 'src2024.pdf').write_bytes(b'%PDF-1.4')   # arrives later from S3
    assert not any('not found next' in w for w in service.status(admin=True)['issues']['warnings'])
    assert service.summary(service.get('src2024'))['has_pdf'] is True
    service.load()                                     # what POST /admin/rescan does
    assert service.status(admin=True)['n_warnings'] == 0
    (library / 'papers' / 'datasets' / 'src2024.pdf').unlink()
    assert service.status(admin=True)['n_warnings'] == 1     # and back again
    service.load_index()                               # an index reload keeps the live view
    assert service.status(admin=True)['n_warnings'] == 1


# ── (3) no shared index for a site without S3 ────────────────────────────────

def test_api_and_stack_never_fetch_the_shared_index():
    serve = SERVE_PATH.read_text()
    assert 'pull_index_if_newer' not in serve and 'INDEX_S3_PREFIX' not in serve
    assert 'get_library_index_dir(cfg)' in serve          # the api reads only data.library_index
    stack = (REPO / 'bin' / 'ops' / 'ant-stack').read_text()
    assert 'pull_index_if_newer' not in stack and '_library_index/' not in stack
    # the only puller is ant-sync, which needs an s3 block, and honours library_index_pull
    sync = (REPO / 'bin' / 'ops' / 'ant-sync').read_text()
    assert "cfg.get('library_index_pull', True)" in sync
    assert "if not bucket:" in sync and "s3.enabled in site.yaml" in sync


def _stack():
    path = REPO / 'bin' / 'ops' / 'ant-stack'
    namespace = {'__name__': 'ant_stack_modes_test', '__file__': str(path)}
    exec(compile(path.read_text().split("\nif __name__ == '__main__':")[0], str(path), 'exec'), namespace)
    return namespace


def _site(tmp_path: Path, **overrides) -> dict:
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    site = {'_h5adstore': tmp_path / 'store', '_genesetstore': None, '_starcatstore': None,
            '_library': None, '_library_index': None, '_state': state, '_scratch': None,
            '_extra_mounts': [], 'data': {'readonly': True}, 'site': {'name': 'x'},
            's3': {'enabled': False}, 'library': {}}
    (tmp_path / 'store').mkdir(exist_ok=True)
    site.update(overrides)
    return site


@pytest.mark.parametrize('s3_enabled,index_pull,expected', [(False, True, False), (True, True, True),
                                                            (True, False, False), (False, False, False)])
def test_stack_renders_index_pull(tmp_path, s3_enabled, index_pull, expected):
    ns = _stack()
    site = _site(tmp_path, s3={'enabled': s3_enabled, 'api_access': 'none'}, library={'index_pull': index_pull})
    site.update({'resources': {}, 'services': {}, 'auth': {'mode': 'none', 'admins': []},
                 'site': {'name': 'x', 'public_url': 'http://h'}})
    assert ns['_index_pull_enabled'](site) is expected


def test_ant_sync_skips_index_pull_when_configured(monkeypatch, tmp_path, capsys):
    import importlib.machinery
    import importlib.util
    path = REPO / 'bin' / 'ops' / 'ant-sync'
    loader = importlib.machinery.SourceFileLoader('ant_sync_modes', str(path))
    spec = importlib.util.spec_from_loader('ant_sync_modes', loader)
    try:
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(REPO / 'bin' / 'ops'))
        loader.exec_module(module)
    except ImportError as exc:
        pytest.skip(f"ant-sync dependencies missing: {exc}")
    cfg = {'library': str(tmp_path / 'lib'), 'library_index': str(tmp_path / 'idx'), 'library_index_pull': False}
    monkeypatch.setattr(module, 'get_config', lambda: cfg)
    monkeypatch.setattr(module, '_s3_client', lambda s3_cfg: object())
    monkeypatch.setattr(config, 'get_config', lambda: cfg)
    called = []
    monkeypatch.setattr(library_sync, 'pull_index_if_newer', lambda *a, **k: called.append(1) or True)
    args = type('Args', (), {'no_git': True, 'dry_run': False, 'no_index': True})()
    errors = module._sync_library(args, {'bucket': 'b'})
    assert errors == 0 and not called
    assert 'library_index_pull is false' in capsys.readouterr().out


# ── data.extra_mounts ────────────────────────────────────────────────────────

def test_extra_mounts_validation():
    ns = _stack()
    assert ns['_extra_mounts'](None) == [] and ns['_extra_mounts']([]) == []
    assert ns['_extra_mounts'](['/a', '/b', '/a']) == [Path('/a'), Path('/b')]
    for bad in ('/a', ['relative/dir'], [None]):
        with pytest.raises(ns['SiteError']):
            ns['_extra_mounts'](bad)


def test_extra_mounts_rendered_read_only_everywhere(tmp_path):
    pytest.importorskip('bcrypt')
    ns = _stack()
    extra = tmp_path / 'intern_store'
    extra.mkdir()
    site = _site(tmp_path, _extra_mounts=[extra])
    ns['_listen_and_ports'] = lambda s: ('', [], '')
    ns['stack_dir'] = lambda s: tmp_path / 'stack'
    compose = ns['render_compose_site'](site)
    for service in ('api', 'web', 'mcp', 'ingest'):
        mounts = [v for v in compose['services'][service]['volumes'] if v.get('source') == str(extra)]
        assert len(mounts) == 1, service
        assert mounts[0]['target'] == str(extra) and mounts[0]['read_only'] is True, service
    assert not [v for v in compose['services']['caddy']['volumes'] if v.get('source') == str(extra)]


def test_extra_mounts_check(tmp_path, capsys):
    ns = _stack()
    state = tmp_path / 'state'
    state.mkdir()
    good = tmp_path / 'good'
    good.mkdir()
    inside = state / 'sub'
    inside.mkdir()
    store_child = tmp_path / 'store' / 'child'
    store_child.mkdir(parents=True)
    site = _site(tmp_path, _extra_mounts=[good, tmp_path / 'missing', inside, tmp_path, store_child])
    problems = ns['_check_extra_mounts'](site)
    text = '\n'.join(problems)
    assert 'missing does not exist' in text
    assert f'{inside} is inside state_dir' in text
    assert f'{tmp_path} contains state_dir' in text
    assert str(good) not in text
    warnings = capsys.readouterr().err
    assert 'overlaps data.h5adstore' in warnings and 'overlaps data.extra_mounts' in warnings
    assert not ns['_check_extra_mounts'](_site(tmp_path, _extra_mounts=[good]))


def test_check_warns_about_symlinks_that_dangle_in_containers(tmp_path, capsys):
    ns = _stack()
    origin = tmp_path / 'origin'
    origin.mkdir()
    (origin / 'a.md').write_text('x')
    store = tmp_path / 'mirror'
    store.mkdir()
    (store / 'a.md').symlink_to(origin / 'a.md')
    site = _site(tmp_path, _h5adstore=store)
    ns['_check_symlinks'](site)
    assert 'will dangle in the containers' in capsys.readouterr().err
    site['_extra_mounts'] = [origin]
    ns['_check_symlinks'](site)
    assert capsys.readouterr().err == ''


# ── (3 ops) uv on PATH ───────────────────────────────────────────────────────

def test_check_uv_messages(monkeypatch, capsys):
    ns = _stack()
    monkeypatch.setattr(ns['shutil'], 'which', lambda name, path=None: None)
    ns['_check_uv']()
    err = capsys.readouterr().err
    assert 'uv was not found on PATH' in err and '.local/bin' in err
    monkeypatch.setattr(ns['shutil'], 'which',
                        lambda name, path=None: None if path else '/home/u/.local/bin/uv')
    ns['_check_uv']()
    out = capsys.readouterr().out
    assert '/home/u/.local/bin' in out and 'cron/systemd' in out
    monkeypatch.setattr(ns['shutil'], 'which', lambda name, path=None: '/usr/bin/uv')
    ns['_check_uv']()
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('name', ['anthive-sync.cron.example', 'anthive-sync.service.example',
                                  'anthive-publish-library.cron.example',
                                  'anthive-publish-library.service.example'])
def test_cron_and_systemd_templates_set_path(name):
    text = (REPO / 'config' / 'examples' / name).read_text()
    assert '.local/bin' in text and 'PATH' in text


def test_ant_cron_run_adds_uv_to_path_and_fails_clearly(tmp_path):
    wrapper = REPO / 'bin' / 'ops' / 'ant-cron-run'
    home = tmp_path / 'home'
    (home / '.local' / 'bin').mkdir(parents=True)
    fake_uv = home / '.local' / 'bin' / 'uv'
    fake_uv.write_text('#!/bin/sh\necho uv-ran\n')
    fake_uv.chmod(0o755)
    env = {'HOME': str(home), 'PATH': '/usr/bin:/bin'}
    ok = subprocess.run([str(wrapper), 'uv'], env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == 'uv-ran'
    fake_uv.unlink()
    env = {'HOME': str(tmp_path / 'empty'), 'PATH': '/nonexistent'}
    # /usr/bin and /bin are always added; hide uv if the host has it there
    if any((Path(d) / 'uv').exists() for d in ('/usr/local/bin', '/usr/bin', '/bin')):
        pytest.skip('uv is installed system-wide on this host')
    bad = subprocess.run([str(wrapper), 'true'], env=env, capture_output=True, text=True)
    assert bad.returncode == 127 and 'uv was not found on PATH' in bad.stderr
    usage = subprocess.run([str(wrapper)], env=env, capture_output=True, text=True)
    assert usage.returncode == 2
