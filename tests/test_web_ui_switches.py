"""The ui switches of a site (site.yaml / anthive.yaml ``ui:``) in the web UI.

A public read-only mirror can switch off facets, caveats, the API pages and
MCP. Each test renders the real templates through a FastAPI TestClient with
the in-process fake API of ``test_web_browser`` and checks three things for
the part that is off: it is gone from the pages (tabs, menus, links, help),
its own pages answer 404, and the defaults leave everything on.
"""

# pyright: reportMissingImports=false

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import help_content  # noqa: E402
from tests.test_web_browser import _fake_api_get, browser  # noqa: E402

ALL_ON = {'facets': True, 'caveats': True, 'api': True, 'mcp': True, 'jupyter': True}
SIGNED_IN = {'Remote-User': 'mf', 'Remote-Name': 'Mark'}


@pytest.fixture
def make_client(monkeypatch):
    """A client factory: make_client(facets=False, ...) switches parts off."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(browser, '_api_get', _fake_api_get)
    monkeypatch.setattr(browser, '_facets_visible_cfg', [[]])

    def build(**off):
        monkeypatch.setattr(browser, '_ui_cfg', {**ALL_ON, **off})
        monkeypatch.setattr(browser, '_caveats_mode_cfg', ['all'])
        app = FastAPI()
        app.state.api_url = 'http://testapi'
        app.state.reports = []
        app.state.genesets_available = True
        app.include_router(browser.router)
        browser.install_error_pages(app)
        return TestClient(app)
    return build


def _visible(html: str) -> str:
    """The page without scripts, styles and data URIs (the logo's base64 can spell anything)."""
    html = re.sub(r'<(script|style)\b.*?</\1>', '', html, flags=re.S)
    return re.sub(r'data:[a-z/+]+;base64,[A-Za-z0-9+/=]+', '', html)


def _with_caveat_in_listing(api_url, path, **kwargs):
    """The fake API, with a caveat on GroupA/ds1 in /databases too (the collection page marker)."""
    payload = _fake_api_get(api_url, path, **kwargs)
    if path == '/databases':
        payload = {'databases': [dict(db, caveats=[{'level': 'high', 'message': 'broken X_pca'}])
                                 if db.get('id') == 'GroupA/ds1' else db for db in payload['databases']]}
    return payload


def _main_nav(html: str) -> str:
    match = re.search(r'<nav aria-label="Main">(.*?)</nav>', html, re.S)
    assert match is not None
    return match.group(1)


def _tab_bar(html: str) -> str:
    match = re.search(r'<div class="tab-bar" id="ds-tab-bar" role="tablist">(.*?)\n</div>', html, re.S)
    assert match is not None
    return match.group(1)


# ── defaults: nothing changes ────────────────────────────────────────────────

def test_ui_defaults_leave_every_part_on(make_client):
    client = make_client()
    assert browser._ui() == ALL_ON
    home = client.get('/', headers=SIGNED_IN).text
    assert 'href="/facets"' in _main_nav(home)
    assert 'id="topbar-api-link"' not in home and 'id="api-menu-link"' in home
    assert 'id="mcp-menu-link"' in home
    assert 'href="/api/changelog"' in home
    page = client.get('/browse/GroupA/ds1').text
    bar = _tab_bar(page)
    assert 'id="tab-group-facet"' in bar and 'id="tab-btn-caveats"' in bar
    assert 'id="tab-caveats"' in page and 'id="ds-open-notebook"' in page
    assert client.get('/facets').status_code == 200
    assert client.get('/api-guide').status_code == 200
    assert client.get('/account/mcp', headers=SIGNED_IN).status_code == 200
    assert 'window.antUi = {facets: true, caveats: true, api: true, mcp: true}' in home


def test_ui_settings_come_from_config(monkeypatch):
    import config
    monkeypatch.delenv('ANTHIVE_UI_FACETS', raising=False)
    assert config.get_ui_settings({}) == ALL_ON
    assert config.get_ui_settings({'ui': {'facets': False, 'mcp': 'off'}}) == {**ALL_ON, 'facets': False, 'mcp': False}
    monkeypatch.setenv('ANTHIVE_UI_FACETS', 'true')
    assert config.get_ui_settings({'ui': {'facets': False}})['facets'] is True
    with pytest.raises(config.ConfigError):
        config.get_ui_settings({'ui': {'caveats': 'maybe'}})
    monkeypatch.setenv('ANTHIVE_CONFIG_STRICT', '1')
    with pytest.raises(config.ConfigError, match='unknown'):
        config.get_ui_settings({'ui': {'facet': False}})


# ── facets ───────────────────────────────────────────────────────────────────

def test_facets_off_hides_tabs_menu_and_pages(make_client):
    client = make_client(facets=False)
    home = client.get('/').text
    assert '/facets' not in _main_nav(home)
    page = client.get('/browse/GroupA/ds1').text
    bar = _tab_bar(page)
    assert 'tab-group-facet' not in bar and 'data-tab="facets"' not in bar and 'data-tab="facet-gene"' not in bar
    assert 'id="tab-facets"' not in page and 'id="tab-facet-gene"' not in page and 'id="tab-facet-gs"' not in page
    assert 'id="ds-facets-pointer"' not in page and 'href="/facets/' not in page
    assert 'id="bio-replicate-unit"' not in page
    assert 'facet replication' not in page
    assert 'class="facet-pill"' not in page          # ant_* columns show their plain name
    for path in ('/facets', '/facets/ant_disease', '/facets/ant_disease/effects-table',
                 '/browse/GroupA/ds1/plot/donor?facet=ant_sex&cell_type=microglia&gene=APOE'):
        response = client.get(path, headers={'Accept': 'text/html'})
        assert response.status_code == 404, path
    # the plot pages themselves stay
    assert client.get('/browse/GroupA/ds1/plot/scatter').status_code == 200


def test_facets_off_skips_facet_api_calls(make_client, monkeypatch):
    seen = []

    def recording(api_url, path, **kwargs):
        seen.append(path)
        return _fake_api_get(api_url, path, **kwargs)
    client = make_client(facets=False)
    monkeypatch.setattr(browser, '_api_get', recording)
    assert client.get('/browse/GroupA/ds1').status_code == 200
    assert not [path for path in seen if 'facet' in path]


def test_facets_off_gene_and_geneset_pages_have_no_facet_de(make_client):
    client = make_client(facets=False)
    gene = client.get('/genes/APOE').text
    assert 'data-vtab="shifts"' not in gene and 'id="vt-shifts"' not in gene
    assert '/facet-effects' not in gene and 'id="gene-facet-effects"' not in gene
    assert 'data-vtab="expression"' in gene
    geneset = client.get('/genesets/StudyX/Exp1/gs_up').text
    assert 'data-vtab="shifts"' not in geneset and 'id="vt-shifts"' not in geneset
    assert '/facet-effects' not in geneset
    assert 'data-vtab="genes"' in geneset
    assert 'window.antUi = {facets: false' in geneset


def test_facets_off_help_has_no_facet_sections(make_client):
    client = make_client(facets=False)
    help_page = client.get('/help').text
    for anchor in ('facet-effects', 'facet-counting', 'facet-howto', 'donor-variability', 'facets-columns',
                   'facet-scopes'):
        assert f'id="{anchor}"' not in help_page, anchor
    assert 'href="#facet-effects"' not in help_page and 'href="/facets"' not in help_page
    assert 'Genes and gene sets' in help_page and 'Genes, gene sets and facets' not in help_page
    assert 'id="glossary-facet"' not in help_page and 'id="glossary-r-value"' not in help_page
    assert 'id="glossary-rank-among-gene-sets"' in help_page
    assert '<tr data-ui="facets">' not in help_page


def test_facets_off_glossary_links_still_resolve(make_client):
    """Every glossary slug a remaining page links to exists on the help page of the same site."""
    client = make_client(facets=False)
    help_page = client.get('/help').text
    defined = set(re.findall(r'id="glossary-([a-z0-9-]+)"', help_page))
    for path in ('/browse/GroupA/ds1', '/genes/APOE', '/genesets/StudyX/Exp1/gs_up', '/donor-groups',
                 '/browse/GroupA/ds1/plot/scatter', '/help'):
        html = _visible(client.get(path).text)   # links drawn by scripts belong to the facet views
        linked = set(re.findall(r'glossary-([a-z0-9-]+)"', html)) | set(re.findall(r'data-glossary="([a-z0-9-]+)"', html))
        missing = linked - defined
        assert not missing, (path, missing)


# ── caveats ──────────────────────────────────────────────────────────────────

def test_caveats_off_hides_tab_panel_and_markers(make_client, monkeypatch):
    client = make_client(caveats=False)
    page = client.get('/browse/GroupA/ds1').text
    assert 'data-tab="caveats"' not in _tab_bar(page)
    assert 'id="tab-caveats"' not in page and 'caveats-card' not in _visible(page)
    assert 'aria-label="Known caveats for this dataset"' not in page
    monkeypatch.setattr(browser, '_api_get', _with_caveat_in_listing)
    folder = client.get('/browse/GroupA').text
    assert 'class="cv-mark' not in folder and '= caveats' not in folder
    plot = client.get('/browse/GroupA/ds1/plot/scatter').text
    assert 'id="cv-toggle"' not in plot and 'id="cv-panel"' not in plot
    help_page = client.get('/help').text
    assert 'id="dataset-caveats"' not in help_page and '<tr data-ui="caveats">' not in help_page
    assert 'and so is Caveats' not in help_page


def test_caveats_on_still_shows_them(make_client, monkeypatch):
    client = make_client()
    page = client.get('/browse/GroupA/ds1').text
    assert 'id="tab-btn-caveats"' in page and 'aria-label="Known caveats for this dataset"' in page
    monkeypatch.setattr(browser, '_api_get', _with_caveat_in_listing)
    assert 'class="cv-mark' in client.get('/browse/GroupA').text
    assert 'id="cv-toggle"' in client.get('/browse/GroupA/ds1/plot/scatter').text


# ── api ──────────────────────────────────────────────────────────────────────

def test_api_off_hides_api_page_links_and_notebooks(make_client):
    client = make_client(api=False)
    home = client.get('/', headers=SIGNED_IN).text
    assert 'id="topbar-api-link"' not in home and 'id="api-menu-link"' not in home
    assert 'href="/api-guide"' not in home and 'href="/api/changelog"' not in home
    assert 'id="stack-version"' in home                  # the version stays, without the link
    assert client.get('/api-guide', headers={'Accept': 'text/html'}).status_code == 404
    page = client.get('/browse/GroupA/ds1').text
    assert 'id="ds-open-notebook"' not in page
    plot = client.get('/browse/GroupA/ds1/plot/scatter').text
    assert 'id="open-notebook"' not in plot
    assert "getElementById('open-notebook')?.addEventListener" in plot   # the script tolerates the missing button
    about = client.get('/about').text
    assert 'href="/api/docs"' not in about and 'href="/jupyter/"' not in about
    help_page = client.get('/help').text
    assert 'id="code"' not in help_page and 'href="/api-guide"' not in help_page
    assert 'curl ' not in help_page and '/api/docs' not in help_page and 'href="/jupyter' not in help_page


# ── mcp ──────────────────────────────────────────────────────────────────────

def test_mcp_off_hides_account_page_menu_and_help(make_client):
    client = make_client(mcp=False)
    home = client.get('/', headers=SIGNED_IN).text
    assert 'id="mcp-menu-link"' not in home and 'href="/account/mcp"' not in home
    assert client.get('/account/mcp', headers={**SIGNED_IN, 'Accept': 'text/html'}).status_code == 404
    assert client.get('/admin/mcp-links', headers=SIGNED_IN, follow_redirects=False).status_code == 404
    help_page = client.get('/help').text
    for anchor in ('mcp', 'mcp-clients', 'mcp-offers'):
        assert f'id="{anchor}"' not in help_page, anchor
    assert 'MCP' not in _visible(help_page)
    assert '/account/mcp' not in help_page
    guide = client.get('/api-guide').text
    assert 'id="api-guide-mcp-about"' not in guide and '/account/mcp' not in guide


def test_all_off_public_mirror(make_client):
    client = make_client(facets=False, caveats=False, api=False, mcp=False)
    help_page = client.get('/help').text
    assert 'id="ai"' not in help_page and 'id="code"' not in help_page
    assert 'You explore it in the browser.' in help_page
    assert 'MCP' not in _visible(help_page)
    page = client.get('/browse/GroupA/ds1').text
    bar = _tab_bar(page)
    assert 'facet' not in bar.lower() and 'caveat' not in bar.lower()
    about = client.get('/about').text
    assert 'MCP' not in _visible(about) and 'notebooks' not in _visible(about)


# ── help content markers ─────────────────────────────────────────────────────

def test_strip_ui_removes_marked_elements_with_nesting():
    html = ('<p>keep</p><p data-ui="facets">drop <span>x</span></p>'
            '<ul><li data-ui="api">a <ul><li>b</li></ul></li><li>c</li></ul><span data-ui="mcp">m</span>')
    out = help_content.strip_ui(html, {'facets', 'api'})
    assert out == '<p>keep</p><ul><li>c</li></ul><span data-ui="mcp">m</span>'
    assert help_content.strip_ui(html, ()) == html


def test_help_topics_of_hidden_parts_render_empty():
    assert help_content.topic_html('facet-effects', hidden={'facets'}) == ''
    pages = help_content.topic_html('pages', hidden={'facets', 'caveats'})
    assert 'id="facets-columns"' not in pages and 'id="dataset-caveats"' not in pages
    assert 'id="dataset-page"' in pages
    full = help_content.topic_html('pages')
    assert 'id="facets-columns"' in full and 'id="dataset-caveats"' in full


def test_help_on_a_site_without_login_does_not_ask_to_sign_in(make_client, monkeypatch):
    client = make_client(facets=False, caveats=False, api=False, mcp=False)
    monkeypatch.setattr(browser, '_api_guide_site_settings', lambda: {'mode': 'none', 'public_base_url': '', 'admins': set(),
                                                                      'upload_groups': ['admins'], 'max_pdf_mb': 25})
    help_page = client.get('/help').text
    assert 'Sign in and look around' not in help_page and 'Signing in and out' not in help_page
    assert '<strong>Look around.</strong>' in help_page and 'No account needed' in help_page
    login_site = make_client()
    monkeypatch.setattr(browser, '_api_guide_site_settings', lambda: {'mode': 'authelia', 'public_base_url': '', 'admins': set(),
                                                                      'upload_groups': ['admins'], 'max_pdf_mb': 25})
    assert 'Sign in and look around' in login_site.get('/help').text


# ── jupyter ──────────────────────────────────────────────────────────────────

def test_jupyter_off_hides_notebook_buttons_and_links_but_keeps_the_api(make_client):
    client = make_client(jupyter=False)
    page = client.get('/browse/GroupA/ds1').text
    assert 'id="ds-open-notebook"' not in page
    plot = client.get('/browse/GroupA/ds1/plot/scatter').text
    assert 'id="open-notebook"' not in plot
    about = client.get('/about').text
    assert 'href="/jupyter/"' not in about and 'href="/api/docs"' in about
    help_page = client.get('/help').text
    assert 'href="/jupyter' not in help_page and '/api/docs' in help_page
    api_guide = client.get('/api-guide').text
    assert 'href="/jupyter' not in api_guide
    on = make_client()
    assert 'href="/jupyter/"' in on.get('/about').text and 'id="ds-open-notebook"' in on.get('/browse/GroupA/ds1').text


def _with_a_note_caveat(api_url, path, **kwargs):
    """ds1's info with a low-level note next to its contamination caveat (a known defect)."""
    payload = _fake_api_get(api_url, path, **kwargs)
    if path.endswith('GroupA/ds1/info'):
        payload = dict(payload, caveats=list(payload.get('caveats') or []) + [
            {'level': 'low', 'kind': 'note', 'message': 'donor ages rounded to five years'}])
    return payload


def test_caveats_serious_shows_only_high_and_known_defects(make_client, monkeypatch):
    client = make_client()
    monkeypatch.setattr(browser, '_api_get', _with_a_note_caveat)
    monkeypatch.setattr(browser, '_caveats_mode_cfg', ['serious'])
    page = client.get('/browse/GroupA/ds1').text
    assert 'id="tab-btn-caveats"' in page                         # the tab stays
    assert 'co-express astrocyte markers' in page                 # contamination: a known defect
    assert 'donor ages rounded' not in page                        # a note is hidden
    monkeypatch.setattr(browser, '_caveats_mode_cfg', ['all'])
    assert 'donor ages rounded' in client.get('/browse/GroupA/ds1').text


def test_caveats_mode_from_the_config(monkeypatch):
    import config
    import metadata
    monkeypatch.delenv('ANTHIVE_UI_CAVEATS', raising=False)
    assert config.get_caveats_mode({'ui': {'caveats': 'serious'}}) == 'serious'
    assert config.get_ui_settings({'ui': {'caveats': 'serious'}})['caveats'] is True
    assert config.get_caveats_mode({'ui': {'caveats': False}}) == 'off'
    assert config.get_caveats_mode({}) == 'all'
    monkeypatch.setenv('ANTHIVE_UI_CAVEATS', 'serious')
    assert config.get_caveats_mode({'ui': {'caveats': True}}) == 'serious'
    high = {'level': 'high', 'kind': 'note'}
    defect = {'level': 'low', 'kind': 'wrong_var_names'}
    note = {'level': 'medium', 'kind': 'small_n'}
    assert metadata.caveats_for_mode([high, defect, note], 'serious') == [high, defect]
    assert metadata.caveats_for_mode([high, note], 'off') == []
