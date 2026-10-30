"""The web UI's dataset journal (REST 2.49): the Journal tab of the dataset page (greyed preview, hidden when the
journal is off), the entries fragment /journal/list (escaping of user text, events lighter, replies nested,
version flag, link types, download counts and names, the rights that show the action buttons), the site-wide
Open reports page /journal/open and its menu entry. ant-serve is faked in process, as in
tests/test_web_browser.py."""
from __future__ import annotations

import re

import pytest
import requests

from tests.test_web_browser import _fake_api_get, browser

STATUS_ON = {
    'enabled': True, 'notes': True,
    'note_categories': ['comment', 'question', 'finding', 'link', 'used-in', 'data-error', 'annotation-concern',
                        'data-request'],
    'event_categories': ['downloaded', 'used-in-analysis', 'artefact-made', 'deposited', 'version-published'],
    'actionable_categories': ['data-error', 'annotation-concern', 'data-request'],
    'statuses': ['open', 'acknowledged', 'fixed', 'declined'],
    'link_types': ['eln', 'doi', 'artefact', 'protocol', 'manuscript', 'other'],
    'ref_keys': ['column', 'value', 'gene', 'genes', 'cell_type', 'evidence'],
    'target_types': ['dataset', 'collection', 'geneset', 'facet', 'artefact'],
    'max_body_chars': 10000, 'download_retention_days': 730,
    'signed_in': True, 'is_admin': False, 'may_triage': False, 'help_topics': ['journal'],
}

EVIL_BODY = 'Sex of donor 3 looks **swapped**. <script>alert(1)</script><img src=x onerror=alert(2)> [x](javascript:alert(3))'

REPORT = {
    'id': 7, 'target_type': 'dataset', 'target_id': 'GroupA/ds1', 'dataset_version': '1.0.0', 'author': 'ana',
    'created': '2026-10-29T10:15:00Z', 'kind': 'note', 'category': 'data-error', 'body': EVIL_BODY,
    'links': [{'url': 'https://eln.example/exp/42', 'label': 'ELN <b>42</b>', 'type': 'eln'},
              {'url': 'javascript:alert(4)', 'label': 'bad', 'type': 'other'}],
    'refs': {'column': 'ant_sex', 'gene': 'XIST', 'evidence': '<i>violin</i>'},
    'parent_id': None, 'edited': None, 'may_edit': True,
    'status': 'open', 'status_by': None, 'status_at': None, 'status_note': None, 'fixed_in_version': None,
    'changelog_ref': None,
    'replies': [{'id': 8, 'target_type': 'dataset', 'target_id': 'GroupA/ds1', 'dataset_version': '1.1.0',
                 'author': 'ben', 'created': '2026-10-29T11:00:00Z', 'kind': 'note', 'category': 'comment',
                 'body': 'Confirmed with XIST.', 'links': [], 'refs': {}, 'parent_id': 7, 'edited': None,
                 'may_edit': False}],
}
EVENT = {'id': 5, 'target_type': 'dataset', 'target_id': 'GroupA/ds1', 'dataset_version': '1.1.0',
         'author': 'anthive', 'created': '2026-10-20T09:00:00Z', 'kind': 'event', 'category': 'artefact-made',
         'body': 'Artefact GA1/ds1/volcano@v2 made from version 1.1.0.', 'links': [], 'refs': {}, 'parent_id': None,
         'edited': None, 'may_edit': False, 'about': {'type': 'artefact', 'id': 'GA1/ds1/volcano'}, 'replies': []}


def _fake(path, journal=True, status=None, names=False, list_error=None):
    status = {**STATUS_ON, **(status or {})}
    if path == '/journal/status':
        if not journal:
            return {**status, 'enabled': False}
        return status
    if path.startswith('/databases/GroupA/ds1/journal/downloads'):
        out = {'target_type': 'dataset', 'target_id': 'GroupA/ds1', 'n_downloads': 12, 'n_people': 4,
               'n_anonymous': 0, 'last_download': '2026-10-28T08:00:00Z', 'retention_days': 730,
               'names_visible': names}
        if names:
            out['rows'] = [{'user': 'carla', 'ts': '2026-10-28T08:00:00Z', 'what': 'h5ad', 'name': 'ds1.h5ad',
                            'dataset_version': '1.1.0'}]
        return out
    if path.startswith('/databases/GroupA/ds1/journal?'):
        if list_error:
            error = requests.HTTPError(f'{list_error}')
            error.response = type('R', (), {'status_code': list_error})()
            raise error
        entries = [REPORT, EVENT]
        if 'kind=event' in path:
            entries = [EVENT]
        return {'target_type': 'dataset', 'target_id': 'GroupA/ds1', 'entries': entries,
                'downloads': {'n_downloads': 12, 'n_people': 4, 'n_anonymous': 0,
                              'last_download': '2026-10-28T08:00:00Z'},
                'next_before_id': 5 if 'before_id' not in path else None}
    if path.startswith('/journal/open'):
        return {'issues': [{**REPORT, 'replies': None}], 'counts': {'data-error': 1}}
    if path.startswith('/artefacts?'):
        return {'artefacts': [], 'next_cursor': None}
    return _fake_api_get(None, path)


@pytest.fixture(autouse=True)
def _ui_on(monkeypatch):
    monkeypatch.setattr(browser, "_facets_visible_cfg", [[]])
    monkeypatch.setattr(browser, "_ui_cfg", {"facets": True, "caveats": True, "api": True, "mcp": True})
    monkeypatch.setattr(browser, "_caveats_mode_cfg", ["all"])
    monkeypatch.setattr(browser, "_home_collection_cfg", [""])


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


SIGNED_IN = {'Remote-User': 'ana'}


def test_the_dataset_page_has_a_greyed_journal_tab(make_client):
    html = make_client().get('/browse/GroupA/ds1', headers=SIGNED_IN).text
    button = re.search(r'<button[^>]*data-tab="journal"[^>]*>.*?</button>', html, re.S).group(0)
    assert 'vw-preview' in button and '>Journal<' in button
    panel = html[html.index('id="tab-journal"'):html.index('id="tab-downloads"')]
    assert 'class="review-banner"' in panel and 'data-view="journal"' in panel
    assert re.search(r'<div class="doc-box" id="jr-help"[^>]*hidden', panel)          # help closed by default
    assert 'id="jr-new"' in panel and 'id="jr-use"' in panel and 'id="jr-filters"' in panel
    assert '<option value="data-error">data error</option>' in panel
    assert 'href="/help#journal"' in panel
    # the tab sits between Artefacts and Downloads
    assert html.index('data-tab="artefacts"') < html.index('data-tab="journal"') < html.index('data-tab="downloads"')
    assert '—' not in panel and 'honest' not in panel.lower()


def test_the_journal_tab_is_hidden_when_the_journal_is_off(make_client):
    html = make_client(journal=False).get('/browse/GroupA/ds1', headers=SIGNED_IN).text
    assert 'data-tab="journal"' not in html and 'id="tab-journal"' not in html


def test_the_journal_tab_is_hidden_without_notes(make_client):
    html = make_client(status={'notes': False}).get('/browse/GroupA/ds1').text
    assert 'data-tab="journal"' not in html


def test_the_journal_tab_is_hidden_when_the_api_has_no_journal(make_client, monkeypatch):
    client = make_client()
    monkeypatch.setattr(browser, '_api_get', lambda api_url, path, **kwargs: _fake_api_get(None, path))
    assert 'data-tab="journal"' not in client.get('/browse/GroupA/ds1').text


def test_the_fragment_escapes_user_text_and_renders_markdown(make_client):
    response = make_client().get('/journal/list?db_id=GroupA/ds1&v=1.1.0', headers=SIGNED_IN)
    html = response.text
    assert response.status_code == 200
    assert '<script>alert' not in html and '<img' not in html and 'href="javascript' not in html
    assert 'javascript:alert(4)' not in html                                       # unsafe link dropped
    assert '<strong>swapped</strong>' in html                                      # safe markdown
    assert 'ELN &lt;b&gt;42&lt;/b&gt;' in html or 'ELN 42' in html                 # labels are plain text
    assert '<span class="jr-ltype">ELN</span><a href="https://eln.example/exp/42"' in html
    assert 'rel="nofollow noopener noreferrer"' in html
    assert '&lt;i&gt;violin&lt;/i&gt;' in html                                     # refs autoescaped
    assert '<a href="/genes/XIST">XIST</a>' in html


def test_the_fragment_nests_replies_flags_old_versions_and_lightens_events(make_client):
    html = make_client().get('/journal/list?db_id=GroupA/ds1&v=1.1.0', headers=SIGNED_IN).text
    report = html[html.index('id="journal-7"'):html.index('id="journal-5"')]
    assert 'class="jr-replies"' in report and 'id="journal-8"' in report           # reply nested
    assert 'jr-ver jr-ver-old' in report and 'version 1.0.0, older than the current version' in report
    assert 'jr-status jr-st-open' in report
    reply = report[report.index('id="journal-8"'):]
    assert 'jr-ver-old' not in reply and 'data-act="reply"' not in reply           # no reply to a reply
    event = re.search(r'<li class="jr-entry jr-event"[^>]*id="journal-5"', html)
    assert event and 'href="/artefacts/GA1/ds1/volcano"' in html
    assert 'artefact made' in html


def test_the_fragment_shows_download_counts_without_names_for_ordinary_users(make_client):
    html = make_client().get('/journal/list?db_id=GroupA/ds1&v=1.1.0', headers=SIGNED_IN).text
    assert '12 downloads by 4 signed-in people, the last on 2026-10-28.' in html
    assert 'id="jr-dl-names"' not in html and 'carla' not in html


def test_the_fragment_shows_names_and_status_controls_to_the_owner(make_client):
    html = make_client(names=True).get('/journal/list?db_id=GroupA/ds1&v=1.1.0', headers=SIGNED_IN).text
    assert 'id="jr-dl-names" hidden' in html and 'carla' in html and 'class="table-wrap"' in html
    assert 'data-act="status"' in html                                             # owner may set statuses


def test_action_buttons_follow_the_rights(make_client):
    plain = make_client().get('/journal/list?db_id=GroupA/ds1&v=1.1.0', headers=SIGNED_IN).text
    assert 'data-act="edit"' in plain and 'data-act="reply"' in plain              # may_edit on own note
    assert 'data-act="status"' not in plain and 'data-act="hide"' not in plain
    admin = make_client(status={'is_admin': True, 'may_triage': True}).get(
        '/journal/list?db_id=GroupA/ds1&v=1.1.0', headers={'Remote-User': 'root', 'Remote-Groups': 'admins'}).text
    assert 'data-act="status"' in admin and 'data-act="hide"' in admin


def test_the_fragment_carries_raw_fields_as_json_only(make_client):
    html = make_client().get('/journal/list?db_id=GroupA/ds1&v=1.1.0', headers=SIGNED_IN).text
    block = re.search(r'<script type="application/json" class="jr-data">(.*?)</script>', html, re.S).group(1)
    assert '<' not in block                                                        # tojson escapes < > &
    assert '"7"' in block and '"8"' in block


def test_the_fragment_answers_sign_in_and_paging(make_client):
    html = make_client(list_error=401).get('/journal/list?db_id=GroupA/ds1').text
    assert 'Sign in to read and write the journal.' in html
    first = make_client().get('/journal/list?db_id=GroupA/ds1', headers=SIGNED_IN).text
    assert 'class="jr-older" data-before="5"' in first
    more = make_client().get('/journal/list?db_id=GroupA/ds1&before_id=5', headers=SIGNED_IN).text
    assert 'data-append="1"' in more and 'jr-downloads' not in more and 'jr-older' not in more


def test_the_fragment_passes_only_known_filters(make_client, monkeypatch):
    seen = []
    client = make_client()

    def spy(api_url, path, **kwargs):
        seen.append(path)
        return _fake(path)
    monkeypatch.setattr(browser, '_api_get', spy)
    client.get('/journal/list?db_id=GroupA/ds1&category=data-error,evil&kind=event&status=open', headers=SIGNED_IN)
    listing = [p for p in seen if p.startswith('/databases/GroupA/ds1/journal?')][0]
    assert 'category=data-error' in listing and 'evil' not in listing
    assert 'kind=event' in listing and 'status=open' in listing


def test_the_open_reports_page_lists_reports_with_their_target(make_client):
    response = make_client().get('/journal/open', headers=SIGNED_IN)
    html = response.text
    assert response.status_code == 200
    assert '<title>Open reports · anthive</title>' in html
    assert 'class="review-banner"' in html and 'data-view="journal-open"' in html
    assert 'href="/browse/GroupA/ds1#journal">Dataset One</a>' in html
    assert '<script>alert' not in html and 'data-act=' not in html
    assert 'href="/journal/open?category=data-error"' in html


def test_the_open_reports_page_is_404_when_the_journal_is_off(make_client):
    assert make_client(journal=False).get('/journal/open').status_code == 404


def test_the_menu_has_a_hidden_preview_open_reports_entry(make_client):
    html = make_client().get('/collections', headers=SIGNED_IN).text
    link = re.search(r'<a href="/journal/open" id="journal-open-menu-link"[^>]*>.*?</a>', html, re.S).group(0)
    assert 'display:none' in link and 'vw-preview' in link and 'preview' in link
    assert "fetch('/api/journal/status'" in html
    anonymous = make_client().get('/collections').text
    assert 'id="journal-open-menu-link"' not in anonymous


@pytest.mark.parametrize('entry, current, older', [
    ('1.0.0', '1.1.0', True), ('1.1.0', '1.1.0', False), ('1.2', '1.1.9', False), ('v1.0', '1.0.1', True),
    (None, '1.0.0', False), ('draft', '1.0.0', True)])
def test_version_older(entry, current, older):
    assert browser._journal_version_older(entry, current) is older
