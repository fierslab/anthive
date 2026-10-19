"""The web UI's artefact views (A6): the dataset page's Artefacts tab, the collection list, analysis
collections without datasets, the artefact page (versions, made_from, files, purge controls) and the
sanitising of depositor text. ant-serve is faked in process, as in tests/test_web_browser.py."""
from __future__ import annotations

import re

import pytest
import requests

from tests.test_web_browser import _fake_api_get, browser

EVIL_TITLE = '<script>alert("t")</script>Volcano **plot**'
EVIL_DESCRIPTION = ('Shows the DE result. <img src=x onerror=alert(1)> [click](javascript:alert(2)) '
                    '[paper](https://doi.org/10.1/x)\n\n![t](https://evil.example/t.png)')

SUMMARY = {'ref': 'GA1/ds1/volcano', 'artefact_id': 'volcano', 'kind': 'figure', 'group_id': 'GroupA',
           'scope': 'ds1', 'db_id': 'GroupA/ds1', 'title': EVIL_TITLE, 'latest': 2, 'versions_live': 2,
           'owner': 'ana', 'updated': '2026-10-18', 'size_bytes': 2048, 'main_file': 'volcano.png',
           'preview': None, 'access': 'inherit', 'path': '/artefacts/GroupA/ds1/volcano'}
GROUP_ARTEFACT = {**SUMMARY, 'ref': 'GA1/_group/overview', 'artefact_id': 'overview', 'kind': 'report',
                  'scope': '_group', 'db_id': None, 'title': 'Collection overview', 'access': 'internal'}
MANIFEST = {'title': EVIL_TITLE, 'description': EVIL_DESCRIPTION, 'change_note': 'fixed the axis',
            'made_from': [{'db_id': 'GroupA/ds1', 'dataset_version': '1.1.0'},
                          {'db_id': None, 'dataset_version': None, 'hidden': 'made from a dataset you cannot see'}],
            'declared': {'author': {'name': 'Ana Lyst'}, 'created': '2026-10-12',
                         'code': {'repository': 'javascript:alert(3)', 'commit': 'abc123'},
                         'software': [{'name': 'python', 'version': '3.12'}]},
            'recorded': {'uploaded_by': 'ana', 'published': '2026-10-18T10:00:00Z'}}
FILES = [{'name': 'volcano.png', 'size': 2048, 'role': 'main', 'download': '/artefacts/GroupA/ds1/volcano/v2/files/volcano.png',
          'preview': '/artefacts/GroupA/ds1/volcano/v2/preview/volcano.png.webp'},
         {'name': 'report.html', 'size': 100, 'role': 'supplement',
          'download': '/artefacts/GroupA/ds1/volcano/v2/files/report.html', 'preview': None}]
DETAIL = {**SUMMARY, 'created': '2026-10-01', 'versions': [], 'manifest': MANIFEST, 'files': FILES}
VERSIONS = {'ref': 'GA1/ds1/volcano', 'latest': 2, 'versions': [
    {'version': 1, 'state': 'purged', 'published': '2026-10-02', 'purged': '2026-10-17', 'purged_by': 'ana',
     'purge_reason': '<b>wrong</b> axis'},
    {'version': 2, 'state': 'live', 'published': '2026-10-18'}]}


def _fake(path, artefacts_listing=True, analysis=False):
    if path.startswith('/artefacts?'):
        if 'group_id=GroupA' in path:
            return {'artefacts': [SUMMARY, GROUP_ARTEFACT], 'next_cursor': None}
        if 'db_id=GroupA%2Fds1' in path and artefacts_listing:
            return {'artefacts': [SUMMARY], 'next_cursor': None}
        if 'group_id=AN6' in path:
            return {'artefacts': [{**GROUP_ARTEFACT, 'group_id': 'AN6'}], 'next_cursor': None}
        return {'artefacts': [], 'next_cursor': None}
    if path == '/artefacts/GroupA/ds1/volcano':
        return DETAIL
    if path == '/artefacts/GroupA/ds1/volcano/versions':
        return VERSIONS
    if path == '/artefacts/GroupA/ds1/volcano/v1':
        raise requests.HTTPError('410 purged')
    if path == '/groups/AN6':
        return {'collection_kind': 'analysis', 'group': 'Atlas2026', 'group_id': 'AN6',
                'title': 'Atlas <i>analyses</i>', 'authors': ['Mark Fiers'], 'year': 2026, 'access': 'internal',
                'description': '# Atlas2026\n\nShared analyses. <script>x()</script>', 'datasets': []}
    if path == '/groups/DS9':
        return {'collection_kind': 'datasets', 'group_id': 'DS9', 'datasets': []}
    if path.startswith('/groups/'):
        raise requests.HTTPError('404')
    return _fake_api_get(None, path)


@pytest.fixture
def make_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def build(**options):
        monkeypatch.setattr(browser, '_api_get', lambda api_url, path, **kwargs: _fake(path, **options))
        app = FastAPI()
        app.state.api_url = 'http://testapi'
        app.state.reports = []
        app.state.genesets_available = True
        app.include_router(browser.router)
        return TestClient(app)
    return build


def _tags(html):
    return {tag.lower() for tag in re.findall(r'<([A-Za-z][A-Za-z0-9]*)', html)}


def test_the_dataset_page_has_a_preview_artefacts_tab(make_client):
    html = make_client().get('/browse/GroupA/ds1').text
    button = re.search(r'<button[^>]*data-tab="artefacts"[^>]*>.*?</button>', html, re.S).group(0)
    assert 'vw-preview' in button and 'tab-empty' not in button and '<span class="tab-count">1</span>' in button
    panel = html[html.index('id="tab-artefacts"'):html.index('id="tab-downloads"')]
    assert 'class="review-banner"' in panel and 'href="/artefacts/GroupA/ds1/volcano"' in panel
    assert '<script>alert' not in html and 'Volcano plot' in panel
    assert 'class="doc-box"' in panel and 'hidden' in panel                     # help closed by default


def test_without_artefacts_the_tab_is_empty_and_downloads_is_unchanged(make_client):
    html = make_client(artefacts_listing=False).get('/browse/GroupA/ds1').text
    button = re.search(r'<button[^>]*data-tab="artefacts"[^>]*>', html).group(0)
    assert 'tab-empty' in button
    assert 'No artefacts for this dataset.' in html
    downloads = html[html.index('id="tab-downloads"'):]
    assert '/artefacts/' not in downloads[:3000]


def test_the_collection_page_lists_its_artefacts(make_client):
    html = make_client().get('/browse/GroupA').text
    section = html[html.index('id="coll-artefacts"'):]
    assert 'href="/artefacts/GroupA/ds1/volcano"' in section and 'the collection' in section
    assert 'internal</span>' in section and 'id="datasets-table"' in html


def test_an_analysis_collection_without_datasets_renders(make_client):
    response = make_client().get('/browse/AN6')
    assert response.status_code == 200
    html = response.text
    assert 'id="datasets-table"' not in html and 'id="ds-filter"' not in html
    assert 'Analysis collection by Mark Fiers, 2026' in html and 'Atlas analyses' in html
    assert '<script>x()' not in html and '&lt;script&gt;' in html               # depositor text, escaped
    assert 'class="review-banner"' in html and 'id="coll-artefacts"' in html


def test_other_collections_without_datasets_stay_404(make_client):
    client = make_client()
    assert client.get('/browse/DS9').status_code == 404
    assert client.get('/browse/NOPE').status_code == 404


def test_the_artefact_page_shows_versions_made_from_and_files(make_client):
    html = make_client().get('/artefacts/GroupA/ds1/volcano').text
    assert '<title>Volcano plot · anthive</title>' in html or 'Volcano plot · anthive' in html
    assert 'class="review-banner"' in html
    assert 'href="/browse/GroupA/ds1"' in html and 'made from a dataset you cannot see' in html
    assert 'href="/api/artefacts/GroupA/ds1/volcano/v2/files/volcano.png" download' in html
    assert 'href="/api/artefacts/GroupA/ds1/volcano/v2/files/report.html" download' in html   # active type: a download
    assert 'src="/api/artefacts/GroupA/ds1/volcano/v2/preview/volcano.png.webp"' in html
    assert 'purged on 2026-10-17 by ana: wrong axis' in html and '<b>wrong' not in html
    assert 'fixed the axis' in html and 'Ana Lyst' in html and 'python 3.12' in html


def test_depositor_text_is_sanitised_on_the_artefact_page(make_client):
    html = make_client().get('/artefacts/GroupA/ds1/volcano').text
    body = html[html.index('class="art-page"'):]
    assert '<script>alert' not in body and 'onerror=alert' not in body.replace('onerror=alert(1)&gt;', '')
    assert not re.search(r'href="\s*javascript:', body) and not re.search(r'href="[^"]*evil', body)
    assert 'evil.example' not in body
    assert '<a href="https://doi.org/10.1/x" rel="nofollow noopener noreferrer">paper</a>' in body
    assert 'img src=x' not in body.replace('&lt;img src=x', '')


def test_the_purge_form_is_only_for_the_owner_and_admins(make_client):
    client = make_client()
    assert 'art-purge-open' not in client.get('/artefacts/GroupA/ds1/volcano').text
    assert 'art-purge-open' not in client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'otto'}).text
    owner = client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'ana'}).text
    assert 'art-purge-open' in owner and 'id="art-purge-form"' in owner
    admin = client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'root', 'Remote-Groups': 'admins'}).text
    assert 'art-purge-open' in admin


def test_a_purged_version_shows_its_tombstone(make_client):
    html = make_client().get('/artefacts/GroupA/ds1/volcano?version=1').text
    assert 'Version v1 was purged on 2026-10-17 by ana' in html


def test_unknown_or_malformed_artefacts_are_404(make_client):
    client = make_client()
    assert client.get('/artefacts/GroupA/ds1/missing').status_code == 404
    assert client.get('/artefacts/GroupA/ds1/..%2F..').status_code == 404
    assert client.get('/artefacts/GroupA/ds1/volcano?version=7').status_code == 404


def test_the_artefacts_view_is_registered_and_not_approved():
    assert 'artefacts' in browser.VIEW_REGISTRY and not browser.view_approved('artefacts')
