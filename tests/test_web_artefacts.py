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
        first = VERSIONS['versions'][0]
        if first['state'] == 'deleted':
            raise requests.HTTPError('410 deleted')
        return {'ref': 'GA1/ds1/volcano@v1', 'version': 1, 'state': first['state'], 'latest': 2,
                'purge': ({key: first[key] for key in ('purged', 'purged_by', 'purge_reason')}
                          if first['state'] == 'purged' else None), 'manifest': MANIFEST, 'files': FILES}
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
    assert 'purged by ana on 2026-10-17: wrong axis' in html and '<b>wrong' not in html
    assert 'class=" art-purged" data-state="purged"' in html                  # greyed out
    assert 'href="?version=1"' in html                                       # a purged version stays viewable
    assert 'fixed the axis' in html and 'Ana Lyst' in html and 'python 3.12' in html


def test_depositor_text_is_sanitised_on_the_artefact_page(make_client):
    html = make_client().get('/artefacts/GroupA/ds1/volcano').text
    body = html[html.index('class="art-page"'):]
    assert '<script>alert' not in body and 'onerror=alert' not in body.replace('onerror=alert(1)&gt;', '')
    assert not re.search(r'href="\s*javascript:', body) and not re.search(r'href="[^"]*evil', body)
    assert 'evil.example' not in body
    assert '<a href="https://doi.org/10.1/x" rel="nofollow noopener noreferrer">paper</a>' in body
    assert 'img src=x' not in body.replace('&lt;img src=x', '')


def test_purge_controls_never_on_the_latest_and_only_for_the_owner_and_admins(make_client, monkeypatch):
    monkeypatch.setitem(VERSIONS['versions'][0], 'state', 'live')
    client = make_client()
    assert 'btn-link art-purge-open' not in client.get('/artefacts/GroupA/ds1/volcano').text
    assert 'btn-link art-purge-open' not in client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'otto'}).text
    owner = client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'ana'}).text
    assert 'class="btn-link art-purge-open" data-version="1"' in owner and 'id="art-purge-form"' in owner
    assert 'data-version="2"' not in owner                                    # the latest version: no button
    assert 'btn-link art-delete-open' not in owner and 'btn-link art-unpurge' not in owner
    admin = client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'root', 'Remote-Groups': 'admins'}).text
    assert 'btn-link art-purge-open' in admin and 'btn-link art-delete-open' not in admin       # live: purge it first


def test_a_purged_version_offers_unpurge_and_for_admins_a_permanent_delete(make_client):
    client = make_client()
    owner = client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'ana'}).text
    assert 'class="btn-link art-unpurge" data-version="1"' in owner and 'btn-link art-delete-open' not in owner
    assert 'btn-link art-purge-open' not in owner
    admin = client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'root', 'Remote-Groups': 'admins'}).text
    assert 'class="btn-link art-delete-open" data-version="1"' in admin and 'id="art-delete-form"' in admin
    assert 'name="confirm"' in admin and "'/' + action" in admin
    assert 'id="art-purge-help"' in admin                                     # help behind the (i)
    assert 'btn-link art-unpurge' not in client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'otto'}).text


def test_a_purged_version_page_shows_the_mark_and_keeps_the_files(make_client):
    html = make_client().get('/artefacts/GroupA/ds1/volcano?version=1').text
    assert 'This version was purged on 2026-10-17 by ana: wrong axis. Its files stay available.' in html
    assert 'href="/api/artefacts/GroupA/ds1/volcano/v2/files/volcano.png" download' in html


def test_a_deleted_version_shows_its_tombstone(make_client, monkeypatch):
    monkeypatch.setitem(VERSIONS, 'versions', [{
        'version': 1, 'state': 'deleted', 'published': '2026-10-02', 'purged': '2026-10-17', 'purged_by': 'ana',
        'purge_reason': 'wrong axis', 'deleted': '2026-10-19', 'deleted_by': 'root', 'delete_reason': 'gone'},
        VERSIONS['versions'][1]])
    client = make_client()
    html = client.get('/artefacts/GroupA/ds1/volcano?version=1').text
    assert 'Version v1 was deleted permanently on 2026-10-19 by root: gone. Its files are gone.' in html
    page = client.get('/artefacts/GroupA/ds1/volcano', headers={'Remote-User': 'root', 'Remote-Groups': 'admins'}).text
    assert 'deleted on 2026-10-19 by root: gone' in page and 'btn-link art-delete-open' not in page


def test_unknown_or_malformed_artefacts_are_404(make_client):
    client = make_client()
    assert client.get('/artefacts/GroupA/ds1/missing').status_code == 404
    assert client.get('/artefacts/GroupA/ds1/..%2F..').status_code == 404
    assert client.get('/artefacts/GroupA/ds1/volcano?version=7').status_code == 404


def test_the_artefacts_view_is_registered_and_not_approved():
    assert 'artefacts' in browser.VIEW_REGISTRY and not browser.view_approved('artefacts')


# ── security review round 3: every store body is rendered without raw HTML ────────────────────────────────

XSS_BODY = 'Our atlas.\n\n<img src=x onerror="fetch(\'/api/me\')">\n\n<script>alert(1)</script> [x](javascript:alert(2))'


def test_a_collection_description_with_markup_renders_as_text_once_it_has_datasets(make_client, monkeypatch):
    import copy
    from tests import test_web_browser
    databases = copy.deepcopy(test_web_browser._DATABASES)
    for db in databases:
        if (db.get('group_id') or db['id'].split('/')[0]) == 'GroupA':
            db.setdefault('collection', {})['description'] = XSS_BODY
    monkeypatch.setattr(test_web_browser, '_DATABASES', databases)
    html = make_client().get('/browse/GroupA').text
    about = html[html.index('About this collection'):html.index('About this collection') + 2000]
    assert '<img' not in about and '<script>alert' not in about and 'onerror="' not in about
    assert '&lt;img' in about and not re.search(r'href="\s*javascript:', about)


def test_a_dataset_description_with_markup_renders_as_text(make_client, monkeypatch):
    import copy
    from tests import test_web_browser
    info = copy.deepcopy(test_web_browser._INFO)
    info['GroupA/ds1']['markdown'] = '# Dataset One\n\n' + XSS_BODY + '\n\n![umap](plot:scatter?x=X_umap)'
    monkeypatch.setattr(test_web_browser, '_INFO', info)
    html = make_client().get('/browse/GroupA/ds1').text
    desc = html[html.index('ds-desc md-body'):html.index('ds-desc md-body') + 3000]
    assert '<script>alert' not in desc and 'onerror="' not in desc and '&lt;img' in desc
    assert 'class="md-plot-thumb"' in desc                     # plot thumbnails still work


def test_the_collection_deposit_escapes_markup_in_index_md():
    import artefact_deposit
    import artefacts as artefacts_lib
    block = {'title': 'Atlas analyses', 'authors': ['Ana Lyst'], 'access': 'internal', 'papers': {},
             'description': XSS_BODY}
    text = artefact_deposit.render_analysis_index_md('Atlas2026', 'A26', block, 'ana', '2026-10-19')
    _meta, body = artefacts_lib.split_front_matter(text)
    assert '<img' not in body and '<script' not in body and '&lt;img' in body


def test_plot_thumbnail_alt_text_is_escaped_once(make_client, monkeypatch):
    import copy
    from tests import test_web_browser
    info = copy.deepcopy(test_web_browser._INFO)
    info['GroupA/ds1']['markdown'] = '# Dataset One\n\n![UMAP & "clusters"](plot:scatter?x=X_umap)'
    monkeypatch.setattr(test_web_browser, '_INFO', info)
    html = make_client().get('/browse/GroupA/ds1').text
    assert 'alt="UMAP &amp; &quot;clusters&quot;"' in html and '&amp;amp;' not in html

