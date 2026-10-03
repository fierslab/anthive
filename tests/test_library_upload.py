"""Paper upload (lib/library_upload.py, POST /papers/upload, MCP gate) and the
two-way library sync (lib/library_sync.py) against an in-memory fake S3."""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import config
import library_sync as ls
import library_upload as lu
from library import load_library, parse_sidecar

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
MCP_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-mcp'

PDF = b'%PDF-1.4\n% test\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n'


def _vcs(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_library(tmp_path: Path, with_vcs: bool = True) -> Path:
    lib = tmp_path / 'library'
    for folder in ('lab', 'datasets', 'reading'):
        (lib / 'papers' / folder).mkdir(parents=True)
    (lib / '.gitignore').write_text('*.pdf\n')
    existing = lib / 'papers' / 'datasets' / 'lee2026plasticity.md'
    existing.write_text(
        "---\nentrytype: article\ncitekey: lee2026plasticity\nauthor:\n- Lee, Donghoon\n"
        "title: Plasticity of human microglia\njournaltitle: Nature Genetics\ndate: 2026\n"
        "doi: 10.1038/s41588-026-02716-6\npmid: '41000000'\nfile: lee2026plasticity.pdf\n"
        "anthive_role: source\n---\n")
    (lib / 'papers' / 'datasets' / 'lee2026plasticity.pdf').write_bytes(PDF + b'lee')
    if with_vcs:
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(lib)], check=True)
        _vcs(lib, 'config', 'user.email', 't@example.org')
        _vcs(lib, 'config', 'user.name', 'Test')
        _vcs(lib, 'add', '-A')
        _vcs(lib, 'commit', '-q', '-m', 'init')
    return lib


def no_fetch(fields):
    return {}


def settings(lib: Path, **kw) -> lu.UploadSettings:
    return lu.UploadSettings(library_dir=lib, **kw)


NEW = {'entrytype': 'article', 'title': 'Microglia in aging brains', 'author': ['Smith, Anna', 'Jones, Bob'],
       'date': '2024-05-01', 'doi': '10.1000/new.1', 'anthive_role': 'background'}


# ── validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('drop,missing', [
    ('entrytype', 'entrytype'), ('title', 'title'), ('author', 'author'),
    ('date', 'date or year'), ('doi', 'identifier'),
])
def test_each_required_field(tmp_path, drop, missing):
    lib = make_library(tmp_path)
    record = {k: v for k, v in NEW.items() if k != drop}
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), record, fetch=no_fetch)
    assert exc.value.status == 422
    assert missing in exc.value.body['missing_required']
    fields_reported = [e['field'] for e in exc.value.body['errors']]
    assert len(fields_reported) == len(set(fields_reported))


def test_unknown_entrytype_and_bad_author_shape(tmp_path):
    lib = make_library(tmp_path)
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), {**NEW, 'entrytype': 'blogpost'}, fetch=no_fetch)
    assert exc.value.status == 422
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), {**NEW, 'author': [{'name': 'x'}]}, fetch=no_fetch)
    assert any(e['field'] == 'author' for e in exc.value.body['errors'])


def test_unpublished_needs_note_not_identifier(tmp_path):
    lib = make_library(tmp_path)
    base = {k: v for k, v in NEW.items() if k != 'doi'}
    with pytest.raises(lu.UploadError):
        lu.upload_paper(settings(lib), {**base, 'entrytype': 'unpublished'}, fetch=no_fetch)
    result = lu.upload_paper(settings(lib), {**base, 'entrytype': 'unpublished',
                                             'note': 'Manuscript in preparation'}, fetch=no_fetch)
    assert result.status == 'created'


@pytest.mark.parametrize('entrytype', ['article', 'online', 'book', 'inbook', 'incollection', 'inproceedings'])
def test_published_types_need_an_identifier(entrytype):
    base = {k: v for k, v in NEW.items() if k != 'doi'}
    errors, missing = lu.check_required({**base, 'entrytype': entrytype, 'institution': 'KU Leuven',
                                         'note': 'a note'})
    assert 'identifier' in missing
    errors, missing = lu.check_required({**base, 'entrytype': entrytype, 'url': 'https://example.org/x'})
    assert not errors and not missing


@pytest.mark.parametrize('entrytype', ['report', 'thesis', 'unpublished', 'misc'])
def test_unpublished_types_need_institution_or_note(entrytype):
    base = {k: v for k, v in NEW.items() if k != 'doi'}
    errors, missing = lu.check_required({**base, 'entrytype': entrytype})
    assert 'identifier' in missing and any(e['field'] == 'institution' for e in errors)
    for extra in ({'institution': 'KU Leuven'}, {'note': 'internal document'}, {'doi': '10.1000/x'}):
        errors, missing = lu.check_required({**base, 'entrytype': entrytype, **extra})
        assert not errors and not missing, (entrytype, extra, errors)


def test_project_proposal_goes_to_proposals_with_background_role(tmp_path):
    lib = make_library(tmp_path)
    proposal = {'entrytype': 'report', 'type': 'project proposal', 'title': 'Microglia states in ageing',
                'author': ['De Strooper, Bart'], 'year': 2026, 'institution': 'KU Leuven'}
    result = lu.upload_paper(settings(lib), proposal, fetch=no_fetch)
    assert result.status == 'created' and result.body['folder'] == 'proposals'
    record = result.body['record']
    assert record['anthive_role'] == 'background' and record['type'] == 'project proposal'
    assert (lib / 'papers' / 'proposals' / f"{result.body['citekey']}.md").is_file()
    thesis = {'entrytype': 'thesis', 'type': 'phd thesis', 'title': 'Microglia and amyloid',
              'author': ['Doe, Jane'], 'year': 2025, 'institution': 'KU Leuven'}
    assert lu.upload_paper(settings(lib), thesis, fetch=no_fetch).body['folder'] == 'reading'


def test_eprint_needs_eprinttype(tmp_path):
    lib = make_library(tmp_path)
    base = {k: v for k, v in NEW.items() if k != 'doi'}
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), {**base, 'eprint': '2024.01.01.1'}, fetch=no_fetch)
    assert any(e['field'] == 'eprinttype' for e in exc.value.body['errors'])
    ok = lu.upload_paper(settings(lib), {**base, 'eprint': '2024.01.01.1', 'eprinttype': 'biorxiv'}, fetch=no_fetch)
    assert ok.status == 'created'


def test_md_text_metadata_accepted(tmp_path):
    lib = make_library(tmp_path)
    text = ("---\nentrytype: article\ntitle: From markdown\nauthor:\n- Doe, Jane\nyear: 2023\n"
            "url: https://example.org/paper\n---\nSome notes.\n")
    result = lu.upload_paper(settings(lib), text, fetch=no_fetch)
    paper = parse_sidecar(lib / result.body['path'])
    assert paper.fields['title'] == 'From markdown' and 'Some notes.' in paper.body


def test_requirements_shape():
    req = lu.requirements(30)
    assert req['max_pdf_mb'] == 30
    assert {'required', 'recommended', 'fields', 'roles', 'folders', 'entrytypes',
            'document_types', 'notice', 'uploads_enabled', 'default_role'} <= set(req)
    assert 'papers_for' in req['recommended'] and 'source' in req['roles']
    assert req['folders'] == ['lab', 'datasets', 'methods', 'reading', 'proposals']
    assert set(req['roles']) == {'source', 'reanalysis', 'method', 'related', 'background'}
    assert req['default_role'] == 'background'
    assert req['fields']['anthive_role']['description'] == (
        'default: background (of interest to the lab, not linked to a dataset); '
        'set source or reanalysis only when the data in anthive come from the paper')
    assert set(req['entrytypes']) == {'article', 'online', 'book', 'inbook', 'incollection', 'inproceedings',
                                      'thesis', 'report', 'unpublished', 'misc'}
    assert all(isinstance(text, str) and text for text in req['entrytypes'].values())
    assert {'project proposal', 'grant proposal'} <= set(req['document_types']['report'])
    assert set(req['document_types']['thesis']) == {'phd thesis', 'master thesis'}
    assert 'anthive_access' not in req['fields'] and 'anthive_llm' not in req['fields']
    assert 'anthive_access' not in req['recommended'] and 'anthive_llm' not in req['recommended']
    assert req['uploads_enabled'] is True and lu.requirements(uploads_enabled=False)['uploads_enabled'] is False


def test_notice_says_who_sees_and_where_text_goes():
    notice = lu.requirements()['notice']
    assert notice == lu.NOTICE
    assert 'Everyone who can sign in' in notice and 'whole library' in notice
    assert 'AI client' in notice and 'external LLM provider' in notice


def test_role_defaults_to_background(tmp_path):
    lib = make_library(tmp_path)
    record = {k: v for k, v in NEW.items() if k != 'anthive_role'}
    result = lu.upload_paper(settings(lib), record, fetch=no_fetch)
    assert result.body['record']['anthive_role'] == 'background' and result.body['folder'] == 'reading'


def test_no_role_with_papers_for_stays_background(tmp_path):
    lib = make_library(tmp_path)
    record = {k: v for k, v in NEW.items() if k != 'anthive_role'}
    result = lu.upload_paper(settings(lib), {**record, 'papers_for': {'L26/FMG': 'source'}}, fetch=no_fetch)
    assert result.body['record']['anthive_role'] == 'background'
    assert result.body['folder'] == 'reading'
    assert result.body['record']['anthive_proposed_links'] == {'L26/FMG': 'source'}
    # precedence still applies: a lab author goes to lab, a proposal to proposals
    lab = lu.upload_paper(settings(lib), {**record, 'doi': '10.1000/lab.1', 'author': ['De Strooper, Bart'],
                                          'papers_for': ['L26/FMG']}, fetch=no_fetch)
    assert lab.body['record']['anthive_role'] == 'background' and lab.body['folder'] == 'lab'
    proposal = lu.upload_paper(settings(lib), {**record, 'doi': None, 'entrytype': 'report',
                                               'type': 'grant proposal', 'institution': 'FWO',
                                               'title': 'A grant', 'papers_for': ['L26/FMG']}, fetch=no_fetch)
    assert proposal.body['record']['anthive_role'] == 'background' and proposal.body['folder'] == 'proposals'


def test_legacy_access_and_llm_are_dropped_with_a_warning(tmp_path):
    lib = make_library(tmp_path)
    record = {**NEW, 'anthive_access': 'restricted', 'anthive_llm': 'never',
              'anthive_draft': ['anthive_llm', 'anthive_summary'], 'anthive_summary': 'S.'}
    result = lu.upload_paper(settings(lib), record, fetch=no_fetch)
    saved = parse_sidecar(lib / result.body['path']).fields
    assert 'anthive_access' not in saved and 'anthive_llm' not in saved
    assert saved['anthive_draft'] == ['anthive_summary']
    warnings = result.body['warnings']
    assert 'anthive_access ignored: access follows the site login' in warnings
    assert any(w.startswith('anthive_llm ignored') for w in warnings)


# ── enrichment, duplicates, folders, PDFs ───────────────────────────────────

def test_enrichment_fills_missing_never_overwrites(tmp_path):
    lib = make_library(tmp_path)
    fetched = {'title': 'CrossRef title', 'author': ['Smith, Anna'], 'date': '2024-05-02',
               'journaltitle': 'Cell', 'abstract': 'An abstract.', 'entrytype': 'article'}
    record = {'doi': '10.1000/x.2', 'title': 'My own title', 'anthive_role': 'background'}
    result = lu.upload_paper(settings(lib), record, fetch=lambda f: fetched)
    fields = result.body['record']
    assert fields['title'] == 'My own title'
    assert fields['journaltitle'] == 'Cell' and fields['author'] == ['Smith, Anna']
    assert set(result.body['filled_fields']) >= {'journaltitle', 'abstract', 'author', 'date'}
    assert 'title' not in result.body['filled_fields']
    assert set(fields['anthive_draft']) >= {'journaltitle', 'abstract'}


def test_enrichment_failure_is_a_warning(tmp_path):
    lib = make_library(tmp_path)

    def boom(fields):
        raise OSError('offline')
    result = lu.upload_paper(settings(lib), NEW, fetch=boom)
    assert result.status == 'created'
    assert any('enrichment failed' in w for w in result.body['warnings'])


@pytest.mark.parametrize('change,matched', [
    ({'doi': '10.1038/S41588-026-02716-6'}, 'doi'),
    ({'doi': None, 'pmid': '41000000'}, 'pmid'),
    ({'doi': None, 'citekey': 'lee2026plasticity', 'url': 'https://x.org'}, 'citekey'),
])
def test_duplicates(tmp_path, change, matched):
    lib = make_library(tmp_path)
    record = {**NEW, **change}
    record = {k: v for k, v in record.items() if v is not None}
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), record, fetch=no_fetch)
    assert exc.value.status == 409
    assert exc.value.body == {'status': 'duplicate', 'existing_citekey': 'lee2026plasticity',
                              'matched_on': matched}


def test_duplicate_without_identifiers_by_title(tmp_path):
    lib = make_library(tmp_path)
    record = {'entrytype': 'online', 'title': 'Microglia states, a preprint', 'author': ['Doe, Jane'],
              'year': 2025, 'url': 'https://example.org/p'}
    assert lu.upload_paper(settings(lib), record, fetch=no_fetch).body['citekey'] == 'doe2025microglia'
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), {**record, 'url': 'https://example.org/other'}, fetch=no_fetch)
    assert exc.value.body == {'status': 'duplicate', 'existing_citekey': 'doe2025microglia', 'matched_on': 'citekey'}
    other = lu.upload_paper(settings(lib), {**record, 'title': 'Microglia states, another preprint'}, fetch=no_fetch)
    assert other.body['citekey'] == 'doe2025microgliaa'


def test_duplicate_by_pdf_checksum(tmp_path):
    lib = make_library(tmp_path)
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), NEW, PDF + b'lee', fetch=no_fetch)
    assert exc.value.body['matched_on'] == 'sha256'


def test_update_existing(tmp_path):
    lib = make_library(tmp_path)
    result = lu.upload_paper(settings(lib), {'doi': '10.1038/s41588-026-02716-6',
                                             'anthive_summary': 'Updated summary.'},
                             fetch=no_fetch, update=True)
    assert result.status == 'updated' and result.http_status == 200
    paper = parse_sidecar(lib / 'papers' / 'datasets' / 'lee2026plasticity.md')
    assert paper.fields['anthive_summary'] == 'Updated summary.'
    assert paper.fields['title'] == 'Plasticity of human microglia'      # kept
    assert not any('not applied' in w for w in result.body['warnings'])


@pytest.mark.parametrize('record,folder', [
    ({**NEW, 'author': ['De Strooper, Bart'], 'anthive_role': 'source'}, 'lab'),
    ({**NEW, 'anthive_role': 'source'}, 'datasets'),
    ({**NEW, 'anthive_role': 'reanalysis'}, 'datasets'),
    ({**NEW, 'anthive_role': 'method'}, 'methods'),
    ({**NEW, 'anthive_role': 'background'}, 'reading'),
])
def test_folder_rule(tmp_path, record, folder):
    lib = make_library(tmp_path)
    result = lu.upload_paper(settings(lib), record, fetch=no_fetch)
    assert result.body['folder'] == folder
    assert (lib / 'papers' / folder / f"{result.body['citekey']}.md").is_file()


def test_explicit_folder_and_invalid_folder(tmp_path):
    lib = make_library(tmp_path)
    assert lu.upload_paper(settings(lib), NEW, folder='lab', fetch=no_fetch).body['folder'] == 'lab'
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), {**NEW, 'doi': '10.1000/ty'}, folder='../etc', fetch=no_fetch)
    assert exc.value.status == 422


def test_pdf_magic_and_size(tmp_path):
    lib = make_library(tmp_path)
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib), NEW, b'<html>not a pdf</html>', fetch=no_fetch)
    assert exc.value.status == 415
    with pytest.raises(lu.UploadError) as exc:
        lu.upload_paper(settings(lib, max_pdf_mb=1), NEW, PDF + b'x' * (1024 * 1024 + 10), fetch=no_fetch)
    assert exc.value.status == 413


def test_dry_run_writes_nothing(tmp_path):
    lib = make_library(tmp_path)
    before = sorted(p.as_posix() for p in lib.rglob('*') if '.git' not in p.parts)
    head = _vcs(lib, 'rev-parse', 'HEAD')
    result = lu.upload_paper(settings(lib), NEW, PDF + b'new', dry_run=True, fetch=no_fetch)
    assert result.status == 'dry_run' and result.http_status == 200
    assert result.body['citekey'] == 'smith2024microglia'
    assert sorted(p.as_posix() for p in lib.rglob('*') if '.git' not in p.parts) == before
    assert _vcs(lib, 'rev-parse', 'HEAD') == head


def test_created_commit_authorship_and_files(tmp_path):
    lib = make_library(tmp_path)
    result = lu.upload_paper(settings(lib, committer='anthive bot <bot@example.org>'), NEW, PDF + b'new',
                             user='asmith', user_name='Anna Smith', user_email='anna@example.org',
                             fetch=no_fetch)
    assert result.status == 'created' and result.http_status == 201
    assert result.body['pdf_sha256'] == hashlib.sha256(PDF + b'new').hexdigest()
    record = lib / 'papers' / 'reading' / 'smith2024microglia.md'
    assert record.is_file() and (lib / 'papers' / 'reading' / 'smith2024microglia.pdf').is_file()
    assert parse_sidecar(record).fields['file'] == 'smith2024microglia.pdf'
    log = _vcs(lib, 'log', '-1', '--format=%an|%ae|%cn|%ce|%s')
    assert log.startswith('Anna Smith|anna@example.org|anthive bot|bot@example.org|Add smith2024microglia')
    tracked = _vcs(lib, 'ls-files')
    assert 'papers/reading/smith2024microglia.md' in tracked and '.pdf' not in tracked.replace('lee', '')
    papers, issues = load_library(lib)
    assert 'smith2024microglia' in papers and not issues.errors


def test_not_a_vcs_repo_is_a_warning(tmp_path):
    lib = make_library(tmp_path, with_vcs=False)
    result = lu.upload_paper(settings(lib), NEW, fetch=no_fetch)
    assert result.status == 'created'
    assert any('not a git repository' in w for w in result.body['warnings'])


def test_papers_for_becomes_a_proposal_only(tmp_path):
    lib = make_library(tmp_path)
    result = lu.upload_paper(settings(lib), {**NEW, 'papers_for': ['L26/FMG', 'S25:reanalysis']},
                             fetch=no_fetch)
    links = result.body['record']['anthive_proposed_links']
    assert links == {'L26/FMG': 'source', 'S25': 'reanalysis'}
    assert result.body['record']['anthive_role'] == 'background'
    assert any('not applied' in w for w in result.body['warnings'])
    with pytest.raises(lu.UploadError):
        lu.upload_paper(settings(lib), {**NEW, 'doi': '10.1000/tz', 'papers_for': {'L26/FMG': 'owner'}},
                        fetch=no_fetch)


# ── REST endpoints (lifted out of ant-serve) ─────────────────────────────────

_SERVE_WANTED = {'_require_uploader', '_upload_settings', 'paper_upload_requirements', 'upload_paper',
                 '_caller_is_admin', '_require_admin'}


def _serve_app(tmp_path, monkeypatch, lib: Path, cfg_extra: Optional[dict] = None):
    cfg = {'library': str(lib), 'auth': {'mode': 'authelia', 'admins': []}, **(cfg_extra or {})}
    monkeypatch.setattr(config, 'get_config', lambda: cfg)
    monkeypatch.setattr(config, 'load_config', lambda: cfg)
    monkeypatch.setattr(lu, 'default_fetch', no_fetch)
    tree = ast.parse(SERVE_PATH.read_text())
    body = [node for node in tree.body if getattr(node, 'name', None) in _SERVE_WANTED]
    app = FastAPI()
    namespace: Dict[str, Any] = {
        'app': app, 'get_config': lambda: cfg, 'Request': Request, 'HTTPException': HTTPException,
        'JSONResponse': JSONResponse, 'Form': Form, 'File': File, 'UploadFile': UploadFile,
        'Optional': Optional, 'Dict': Dict, 'List': List, 'Any': Any, 'Lock': Lock, 'Tuple': Tuple,
        'log': logging.getLogger('test-upload'),
        'get_library_dir': config.get_library_dir,
        'get_auth_settings': lambda cfg_arg=None: config.get_auth_settings(cfg),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    after = []
    namespace['_after_upload'] = lambda result, switches: after.append(result.body['citekey']) or []
    return TestClient(app), after


ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'users,admins', 'Remote-Name': 'Root Admin',
         'Remote-Email': 'root@example.org'}
USER = {'Remote-User': 'alice', 'Remote-Groups': 'users'}


FORBIDDEN = {'status': 'forbidden', 'message': 'Only users in admins can add papers to the library.'}


def test_endpoint_requirements_and_auth(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    client, _ = _serve_app(tmp_path, monkeypatch, lib, {'library_upload_max_pdf_mb': 7})
    assert client.get('/papers/upload/requirements', headers=ADMIN).json()['max_pdf_mb'] == 7
    for headers in (USER, {}):
        response = client.get('/papers/upload/requirements', headers=headers)
        assert response.status_code == 403 and response.json() == FORBIDDEN
    form = {'metadata': json.dumps(NEW)}
    response = client.post('/papers/upload', data=form, headers=USER)
    assert response.status_code == 403 and response.json() == FORBIDDEN
    assert client.post('/papers/upload', data=form).status_code == 403


def test_upload_routes_not_in_public_openapi(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    client, _ = _serve_app(tmp_path, monkeypatch, lib)
    paths = client.get('/openapi.json').json().get('paths', {})
    assert '/papers/upload' not in paths and '/papers/upload/requirements' not in paths
    source = SERVE_PATH.read_text()
    assert '@app.get("/papers/upload/requirements", tags=["library"], include_in_schema=False)' in source
    assert '@app.post("/papers/upload", tags=["library"], include_in_schema=False)' in source


def test_endpoint_created_duplicate_invalid_and_groups(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    client, after = _serve_app(tmp_path, monkeypatch, lib, {'library_upload_groups': ['admins', 'curators']})
    response = client.post('/papers/upload', data={'metadata': json.dumps(NEW)},
                           files={'file': ('x.pdf', PDF + b'new', 'application/pdf')}, headers=ADMIN)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['status'] == 'created' and body['citekey'] == 'smith2024microglia' and after == ['smith2024microglia']
    assert 'Root Admin' in _vcs(lib, 'log', '-1', '--format=%an')
    dup = client.post('/papers/upload', data={'metadata': json.dumps(NEW)}, headers=ADMIN)
    assert dup.status_code == 409 and dup.json()['matched_on'] == 'doi'
    bad = client.post('/papers/upload', data={'metadata': json.dumps({'title': 'x'})}, headers=ADMIN)
    assert bad.status_code == 422 and bad.json()['status'] == 'invalid'
    curator = {'Remote-User': 'cur', 'Remote-Groups': 'curators'}
    dry = client.post('/papers/upload', data={'metadata': json.dumps({**NEW, 'doi': '10.1000/tc'}), 'dry_run': 'true'},
                      headers=curator)
    assert dry.status_code == 200 and dry.json()['status'] == 'dry_run', dry.text
    notpdf = client.post('/papers/upload', data={'metadata': json.dumps({**NEW, 'doi': '10.1000/td'})},
                         files={'file': ('x.pdf', b'GIF89a', 'application/pdf')}, headers=ADMIN)
    assert notpdf.status_code == 415


def test_endpoint_unwritable_library_is_a_clear_error(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    client, after = _serve_app(tmp_path, monkeypatch, lib)
    target = lib / 'papers' / 'reading'
    target.chmod(0o555)
    try:
        response = client.post('/papers/upload', data={'metadata': json.dumps(NEW)}, headers=ADMIN)
    finally:
        target.chmod(0o755)
    assert response.status_code == 500 and 'cannot write to the library' in response.json()['message']
    assert after == [] and 'smith2024microglia' not in _vcs(lib, 'log', '--format=%s')


def test_endpoint_disabled_when_library_not_writable(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    client, after = _serve_app(tmp_path, monkeypatch, lib)
    papers_dir = lib / 'papers'
    head_before = _vcs(lib, 'rev-parse', 'HEAD')
    papers_dir.chmod(0o555)
    try:
        if os.access(papers_dir, os.W_OK):
            pytest.skip('running as a user that ignores directory permissions')
        response = client.post('/papers/upload', data={'metadata': json.dumps(NEW)},
                               files={'file': ('x.pdf', PDF + b'x', 'application/pdf')}, headers=ADMIN)
        requirements = client.get('/papers/upload/requirements', headers=ADMIN).json()
        not_uploader = client.post('/papers/upload', data={'metadata': json.dumps(NEW)}, headers=USER)
    finally:
        papers_dir.chmod(0o755)
    assert response.status_code == 503
    assert response.json() == {'status': 'disabled',
                               'message': 'Paper uploads are switched off on this site (services.paper_upload).'}
    assert requirements['uploads_enabled'] is False
    assert not_uploader.status_code == 403 and not_uploader.json() == FORBIDDEN   # 403 before 503
    assert after == [] and _vcs(lib, 'rev-parse', 'HEAD') == head_before
    assert not (lib / '.git' / 'anthive-upload.lock').exists()
    assert not lu.uploads_enabled(tmp_path / 'missing')


def test_endpoint_basic_mode_admin_list(tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    client, _ = _serve_app(tmp_path, monkeypatch, lib, {'auth': {'mode': 'basic', 'admins': ['bob']}})
    ok = client.post('/papers/upload', data={'metadata': json.dumps(NEW)}, headers={'Remote-User': 'bob'})
    assert ok.status_code == 201
    no = client.post('/papers/upload', data={'metadata': json.dumps({**NEW, 'doi': '10.1000/te'})},
                     headers={'Remote-User': 'carol'})
    assert no.status_code == 403


# ── MCP gate ─────────────────────────────────────────────────────────────────

def test_mcp_upload_tools_gate(monkeypatch):
    tree = ast.parse(MCP_PATH.read_text())
    wanted = {'_is_admin', '_upload_groups', '_may_upload'}
    body = [n for n in tree.body if getattr(n, 'name', None) in wanted
            or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'UPLOAD_TOOLS'
                                                  for t in n.targets))]
    cfg = {'library_upload_groups': ['admins', 'curators']}
    namespace: Dict[str, Any] = {'Optional': Optional, '_get_anthive_config': lambda: cfg}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MCP_PATH), 'exec', dont_inherit=True), namespace)
    assert namespace['UPLOAD_TOOLS'] == frozenset({'paper_upload_requirements', 'check_paper', 'upload_paper'})
    may = namespace['_may_upload']
    assert may({'user': 'a', 'groups': ['curators']}) and may({'user': 'r', 'groups': ['admins']})
    assert not may({'user': 'u', 'groups': ['users']}) and not may(None)
    source = MCP_PATH.read_text()
    for tool in ('paper_upload_requirements', 'check_paper', 'upload_paper'):
        assert f"async def {tool}(" in source


def _anthive_mcp_class(cfg):
    """AnthiveMCP from ant-mcp on a stub FastMCP base."""
    import asyncio  # noqa: F401
    import contextvars
    from types import SimpleNamespace
    tree = ast.parse(MCP_PATH.read_text())
    names = {'_is_admin', '_upload_groups', '_may_upload', 'AnthiveMCP'}
    assigns = {'UPLOAD_TOOLS', 'UPLOADS_DISABLED_MESSAGE', 'ADMIN_ONLY_TOOLS'}
    body = [n for n in tree.body if getattr(n, 'name', None) in names
            or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in assigns for t in n.targets))]

    class StubFastMCP:
        tool_names = ('search_literature', 'check_paper', 'upload_paper', 'paper_upload_requirements')

        def get_context(self):
            raise LookupError

        async def list_tools(self):
            return [SimpleNamespace(name=name) for name in self.tool_names]

        async def call_tool(self, name, arguments):
            return f'ran {name}'

    namespace: Dict[str, Any] = {
        'Optional': Optional, '_get_anthive_config': lambda: cfg, 'FastMCP': StubFastMCP,
        '_current_identity': contextvars.ContextVar('identity', default=None), 'frozenset': frozenset,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MCP_PATH), 'exec', dont_inherit=True), namespace)
    return namespace['AnthiveMCP'], namespace


def test_mcp_hides_upload_tools_when_uploads_disabled():
    import asyncio
    cls, namespace = _anthive_mcp_class({'library_upload_groups': ['admins']})
    server = cls()
    server.require_identity = False

    async def tool_names():
        return {tool.name for tool in await server.list_tools()}

    assert {'check_paper', 'upload_paper'} <= asyncio.run(tool_names())

    async def disabled(identity=None):
        return False
    server.uploads_probe = disabled
    names = asyncio.run(tool_names())
    assert 'check_paper' not in names and 'upload_paper' not in names
    assert 'paper_upload_requirements' not in names and 'search_literature' in names
    with pytest.raises(PermissionError, match='switched off'):
        asyncio.run(server.call_tool('upload_paper', {}))
    assert asyncio.run(server.call_tool('search_literature', {})) == 'ran search_literature'


def test_mcp_upload_tools_only_for_uploaders():
    import asyncio
    cls, namespace = _anthive_mcp_class({'library_upload_groups': ['admins']})
    server = cls()
    server.require_identity = True
    upload_tools = {'paper_upload_requirements', 'check_paper', 'upload_paper'}

    async def tool_names(identity):
        token = namespace['_current_identity'].set(identity)
        try:
            return {tool.name for tool in await server.list_tools()}
        finally:
            namespace['_current_identity'].reset(token)

    assert not upload_tools & asyncio.run(tool_names({'user': 'alice', 'groups': ['users']}))
    assert not upload_tools & asyncio.run(tool_names(None))
    assert upload_tools <= asyncio.run(tool_names({'user': 'root', 'groups': ['admins']}))


def test_mcp_text_tells_users_where_text_goes():
    source = MCP_PATH.read_text()
    assert source.count('LLM provider') >= 3          # instructions, guide, tool descriptions
    assert 'DATA LEAVES THE SERVER' in source
    assert 'anthive_llm' not in source and 'summary_only' not in source


# ── two-way sync against a fake S3 ───────────────────────────────────────────

class FakeS3:
    def __init__(self):
        self.objects: Dict[str, Dict[str, Any]] = {}

    def _etag(self, data: bytes) -> str:
        return hashlib.md5(data).hexdigest()

    def put(self, key, data, metadata=None):
        self.objects[key] = {'data': data, 'meta': dict(metadata or {}), 'etag': self._etag(data)}

    def get_paginator(self, name):
        outer = self

        class Pager:
            def paginate(self, Bucket, Prefix):
                return [{'Contents': [{'Key': k, 'Size': len(v['data']), 'ETag': f'"{v["etag"]}"'}
                                      for k, v in sorted(outer.objects.items()) if k.startswith(Prefix)]}]
        return Pager()

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        obj = self.objects[Key]
        return {'Metadata': obj['meta'], 'ETag': f'"{obj["etag"]}"', 'ContentLength': len(obj['data'])}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.put(key, Path(path).read_bytes(), (ExtraArgs or {}).get('Metadata'))

    def download_file(self, bucket, key, path):
        Path(path).write_bytes(self.objects[key]['data'])

    def copy_object(self, Bucket, Key, CopySource, MetadataDirective='COPY', Metadata=None, **kw):
        src = self.objects[CopySource['Key']]
        meta = Metadata if MetadataDirective == 'REPLACE' else src['meta']
        self.objects[Key] = {'data': src['data'], 'meta': dict(meta or {}),
                             'etag': self._etag(src['data'] + (b'm' if MetadataDirective == 'REPLACE' else b''))}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


KEY = '_library/papers/'


def site(tmp_path, name):
    lib = tmp_path / name
    (lib / 'papers' / 'reading').mkdir(parents=True)
    return lib


def run(s3, lib, **kw):
    return ls.sync_library_pdfs(s3, 'b', '', lib, today='20261002', say=lambda m: None, **kw)


def kinds(report):
    return sorted((a.kind, a.rel) for a in report.actions if a.kind not in ('record', 'forget'))


def test_sync_new_local_then_new_remote_and_in_sync(tmp_path):
    s3 = FakeS3()
    a, b = site(tmp_path, 'a'), site(tmp_path, 'b')
    (a / 'papers' / 'reading' / 'p1.pdf').write_bytes(PDF + b'1')
    assert kinds(run(s3, a)) == [('upload', 'reading/p1.pdf')]
    assert s3.objects[KEY + 'reading/p1.pdf']['meta']['sha256'] == hashlib.sha256(PDF + b'1').hexdigest()
    report = run(s3, b)
    assert kinds(report) == [('download', 'reading/p1.pdf')]
    assert (b / 'papers' / 'reading' / 'p1.pdf').read_bytes() == PDF + b'1'
    assert report.changed_local == [b / 'papers' / 'reading' / 'p1.pdf']
    assert kinds(run(s3, a)) == [] and kinds(run(s3, b)) == []


def test_sync_changed_local_only_and_remote_only(tmp_path):
    s3 = FakeS3()
    a, b = site(tmp_path, 'a'), site(tmp_path, 'b')
    (a / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'v1')
    run(s3, a), run(s3, b)
    (a / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'v2-from-a')
    assert kinds(run(s3, a)) == [('upload', 'reading/p.pdf')]
    assert kinds(run(s3, b)) == [('download', 'reading/p.pdf')]
    assert (b / 'papers' / 'reading' / 'p.pdf').read_bytes() == PDF + b'v2-from-a'


def test_sync_conflict_keeps_local_and_saves_remote(tmp_path):
    s3 = FakeS3()
    a, b = site(tmp_path, 'a'), site(tmp_path, 'b')
    (a / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'base')
    run(s3, a), run(s3, b)
    (a / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'edit-a')
    run(s3, a)
    (b / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'edit-b')
    report = run(s3, b)
    assert kinds(report) == [('conflict', 'reading/p.pdf')]
    assert (b / 'papers' / 'reading' / 'p.pdf').read_bytes() == PDF + b'edit-b'
    assert (b / 'papers' / 'reading' / 'p.conflict-20261002.pdf').read_bytes() == PDF + b'edit-a'
    assert kinds(run(s3, b)) == [('conflict', 'reading/p.pdf')]          # stays until resolved
    assert kinds(run(s3, b, prefer='local')) == [('resolve_local', 'reading/p.pdf')]
    assert s3.objects[KEY + 'reading/p.pdf']['data'] == PDF + b'edit-b'
    assert kinds(run(s3, b)) == []
    assert kinds(run(s3, a)) == [('download', 'reading/p.pdf')]


def test_sync_deletes_report_or_propagate(tmp_path):
    s3 = FakeS3()
    a, b = site(tmp_path, 'a'), site(tmp_path, 'b')
    (a / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'x')
    run(s3, a), run(s3, b)
    (a / 'papers' / 'reading' / 'p.pdf').unlink()
    assert kinds(run(s3, a)) == [('report', 'reading/p.pdf')]
    assert KEY + 'reading/p.pdf' in s3.objects
    assert kinds(run(s3, a, propagate_deletes=True)) == [('delete_remote', 'reading/p.pdf')]
    assert KEY + 'reading/p.pdf' not in s3.objects
    assert kinds(run(s3, b)) == [('report', 'reading/p.pdf')]             # deleted remotely, kept locally
    assert (b / 'papers' / 'reading' / 'p.pdf').exists()
    assert kinds(run(s3, b, propagate_deletes=True)) == [('delete_local', 'reading/p.pdf')]
    assert not (b / 'papers' / 'reading' / 'p.pdf').exists()


def test_sync_move_local_side_and_remote_side(tmp_path):
    s3 = FakeS3()
    a, b, c = site(tmp_path, 'a'), site(tmp_path, 'b'), site(tmp_path, 'c')
    (a / 'papers' / 'lab').mkdir()
    (a / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'm')
    run(s3, a), run(s3, b), run(s3, c)
    os.replace(a / 'papers' / 'reading' / 'p.pdf', a / 'papers' / 'lab' / 'p.pdf')
    # without --propagate-deletes the S3 copy is made, the old key stays
    assert kinds(run(s3, a)) == [('move_remote', 'lab/p.pdf')]
    assert KEY + 'lab/p.pdf' in s3.objects and KEY + 'reading/p.pdf' in s3.objects
    assert kinds(run(s3, a)) == [('report', 'reading/p.pdf')]
    assert kinds(run(s3, b)) == [('download', 'lab/p.pdf')]          # old one kept too
    # with it, the old key goes, and a site that has not seen either half moves locally
    run(s3, a, propagate_deletes=True)
    assert KEY + 'reading/p.pdf' not in s3.objects
    assert kinds(run(s3, c)) == [('move_local', 'lab/p.pdf')]
    assert (c / 'papers' / 'lab' / 'p.pdf').exists() and not (c / 'papers' / 'reading' / 'p.pdf').exists()


def test_sync_first_contact_identical_and_legacy_metadata(tmp_path):
    s3 = FakeS3()
    a = site(tmp_path, 'a')
    (a / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'same')
    s3.put(KEY + 'reading/p.pdf', PDF + b'same', {'original-mtime': '1'})       # legacy: no sha256
    assert kinds(run(s3, a)) == []
    assert s3.objects[KEY + 'reading/p.pdf']['meta']['sha256'] == hashlib.sha256(PDF + b'same').hexdigest()
    b = site(tmp_path, 'b')
    (b / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'different!')
    assert kinds(run(s3, b)) == [('conflict', 'reading/p.pdf')]


def test_sync_dry_run_and_read_only_site(tmp_path):
    s3 = FakeS3()
    a = site(tmp_path, 'a')
    (a / 'papers' / 'reading' / 'p.pdf').write_bytes(PDF + b'd')
    assert kinds(run(s3, a, dry_run=True)) == [('upload', 'reading/p.pdf')]
    assert s3.objects == {} and not ls.manifest_path(a).exists()
    report = run(s3, a, allow_remote_writes=False)
    assert kinds(report) == [('report', 'reading/p.pdf')] and s3.objects == {}


def test_sync_conflict_copies_are_never_synced(tmp_path):
    s3 = FakeS3()
    a = site(tmp_path, 'a')
    (a / 'papers' / 'reading' / 'p.conflict-20261001.pdf').write_bytes(PDF)
    assert kinds(run(s3, a)) == []


def test_push_one_pdf_records_manifest(tmp_path):
    s3 = FakeS3()
    a = site(tmp_path, 'a')
    pdf = a / 'papers' / 'reading' / 'up.pdf'
    pdf.write_bytes(PDF + b'up')
    assert ls.push_one_pdf(s3, 'b', '', a, pdf) == 'reading/up.pdf'
    assert kinds(run(s3, a)) == []


def test_git_sync_without_remote_and_not_a_repo(tmp_path):
    lib = make_library(tmp_path)
    assert ls.git_sync(lib, push=True)['status'] == 'skipped'
    assert 'no remote' in ls.git_sync(lib, push=True)['detail']
    assert ls.git_sync(tmp_path, push=False)['status'] == 'skipped'


def test_git_sync_pull_and_push(tmp_path):
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(origin)], check=True)
    lib = make_library(tmp_path)
    _vcs(lib, 'remote', 'add', 'origin', str(origin))
    _vcs(lib, 'push', '-q', '-u', 'origin', 'main')
    other = tmp_path / 'other'
    subprocess.run(['git', 'clone', '-q', str(origin), str(other)], check=True)
    _vcs(other, 'config', 'user.email', 'o@example.org')
    _vcs(other, 'config', 'user.name', 'Other')
    (other / 'papers' / 'reading').mkdir(parents=True, exist_ok=True)
    (other / 'papers' / 'reading' / 'x.md').write_text('---\nentrytype: misc\ntitle: x\nnote: n\nyear: 2020\n---\n')
    _vcs(other, 'add', '-A')
    _vcs(other, 'commit', '-q', '-m', 'other site')
    _vcs(other, 'push', '-q')
    lu.upload_paper(settings(lib), NEW, fetch=no_fetch)        # a local commit
    result = ls.git_sync(lib, push=True)
    assert result['status'] == 'ok', result
    assert (lib / 'papers' / 'reading' / 'x.md').exists()
    assert 'Add smith2024microglia' in _vcs(origin, 'log', '--format=%s')


# ── ant-stack rendering ──────────────────────────────────────────────────────

def _load_ant_stack():
    import importlib.machinery
    import importlib.util
    path = REPO_ROOT / 'bin' / 'ops' / 'ant-stack'
    loader = importlib.machinery.SourceFileLoader('ant_stack_upload_test', str(path))
    spec = importlib.util.spec_from_loader('ant_stack_upload_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_ant_stack_mounts_and_keys(tmp_path):
    stack = _load_ant_stack()
    import yaml
    for upload in (False, True):
        site_file = tmp_path / f'site-{upload}.yaml'
        lib, idx, store, state = (tmp_path / n for n in ('lib', 'idx', 'store', f'state{upload}'))
        for d in (lib, idx, store):
            d.mkdir(exist_ok=True)
        site_file.write_text(yaml.safe_dump({
            'site': {'name': 't', 'public_url': 'https://anthive.example.org'},
            'data': {'h5adstore': str(store), 'library': str(lib), 'library_index': str(idx)},
            'state_dir': str(state), 'services': {'paper_upload': upload},
            'library': {'upload_groups': ['admins', 'curators'], 'git_push': True},
        }))
        site_cfg = stack.load_site(site_file)
        compose = stack.render_compose_site(site_cfg) if hasattr(stack, 'render_compose_site') else None
        if compose is None:
            fn = next(getattr(stack, n) for n in dir(stack) if n.startswith('render_compose'))
            compose = fn(site_cfg)
        mounts = {m['target']: m.get('read_only', False) for m in compose['services']['api']['volumes']
                  if isinstance(m, dict) and m.get('target') in (str(lib), str(idx))}
        assert mounts == {str(lib): not upload, str(idx): not upload}
        cfg = stack.render_anthive_yaml(site_cfg)
        assert cfg['library_upload_groups'] == ['admins', 'curators'] and cfg['library_git_push'] is True
        assert cfg['library_index_publisher'] is False and cfg['mcp']['max_upload_mb'] == 25


def test_endpoint_md_record_with_unquoted_date(tmp_path, monkeypatch):
    """YAML turns `date: 2026-10-03` into a date object; dry run and upload
    must still answer with JSON (a 500 came after the commit in 2.41.0 rc)."""
    lib = make_library(tmp_path)
    client, after = _serve_app(tmp_path, monkeypatch, lib)
    text = ("---\nentrytype: report\ntype: project proposal\ntitle: A dated proposal\n"
            "author:\n- Doe, Jane\ndate: 2026-10-03\ninstitution: KU Leuven\n---\n")
    dry = client.post('/papers/upload', data={'metadata': text, 'dry_run': 'true'}, headers=ADMIN)
    assert dry.status_code == 200, dry.text
    created = client.post('/papers/upload', data={'metadata': text}, headers=ADMIN)
    assert created.status_code == 201, created.text
    paper = parse_sidecar(lib / created.json()['path'])
    assert str(paper.fields['date']) == '2026-10-03'
