"""The biologist front door (2026-10-26): "/" with one search box, browse by disease and cell type, the search
results page, the collection list at /collections, and the dataset page's reading view with Technical details.

Rendered through the real templates with the in-process fake API of ``test_web_browser``; GET /search is
answered by ``lib/catalog_search`` over the fake catalogue, the way ant-serve answers it.
"""

# pyright: reportMissingImports=false

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import catalog_search  # noqa: E402
from tests.test_web_browser import _fake_api_get, browser  # noqa: E402

CALLS = []


def _fake_with_search(api_url, path, **kwargs):
    CALLS.append(path)
    if path.startswith('/search?'):
        query = parse_qs(urlparse(path).query)['q'][0]
        databases = _fake_api_get(api_url, '/databases')['databases']
        index = catalog_search.GeneIndex([(db['id'], 'TREM2') for db in databases])
        artefacts = [{'ref': f'GroupA/ds1/umap-{n}', 'artefact_id': f'umap-{n}', 'kind': 'figure', 'group_id': 'GroupA',
                      'scope': 'ds1', 'db_id': 'GroupA/ds1', 'title': f'Microglia UMAP {n}', 'description': 'clusters',
                      'withdrawn': n == 0} for n in range(7)]
        facets = [{'column': 'ant_sex', 'tag': 'sex', 'label': 'Sex', 'summary': 'Biological sex of the donor',
                   'definition': '', 'n_datasets': 2}]
        return catalog_search.search(query, databases, gene_index=index, genesets=[], labels={},
                                     artefacts=artefacts, facets=facets, help_hidden=[],
                                     limit=int(parse_qs(urlparse(path).query).get('limit', ['50'])[0]))
    return _fake_api_get(api_url, path, **kwargs)


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(browser, '_api_get', _fake_with_search)
    monkeypatch.setattr(browser, '_ui_cfg', {'facets': True, 'caveats': True, 'api': True, 'mcp': True, 'jupyter': True})
    monkeypatch.setattr(browser, '_caveats_mode_cfg', ['all'])
    monkeypatch.setattr(browser, '_facets_visible_cfg', [[]])
    CALLS.clear()
    browser._FRONT_DOOR_CACHE.clear()
    app = FastAPI()
    app.state.api_url = 'http://testapi'
    app.state.reports = []
    app.state.genesets_available = True
    app.include_router(browser.router)
    browser.install_error_pages(app)
    return TestClient(app)


def test_front_door_has_search_browse_examples_and_a_closed_what_is_here(client):
    html = client.get('/').text
    assert '<title>Find data · anthive</title>' in html
    assert 'class="review-banner"' in html and 'data-view="front-door"' in html     # preview until approved
    form = html.split('class="fd-search"')[1].split('</form>')[0]
    assert 'action="/search"' in html and 'name="q"' in form
    for link in ('href="/diseases"', 'href="/cell-types"', 'href="/collections"'):
        assert link in html
    examples = html.split('id="fd-examples"')[1].split('</p>')[0]
    assert examples.count('href="/search?q=') == 2 and 'TREM2' in examples and 'APOE4' in examples
    # one visible line, the paragraph hidden behind its toggle
    assert 'data-help-for="fd-what"' in html and re.search(r'<div class="doc-box" id="fd-what"[^>]*hidden', html)
    assert '3 datasets in 2 collections' in html
    assert 'A cell count is not a sample size' in html
    assert '—' not in html.split('id="front-door"')[1].split('</main>')[0]


def test_front_door_counts_only_what_the_api_returns_for_the_visitor(client, monkeypatch):
    def public_only(api_url, path, **kwargs):
        result = _fake_api_get(api_url, path, **kwargs)
        if path == '/databases':      # ant-serve leaves internal datasets out for an anonymous visitor
            result = {'databases': [db for db in result['databases'] if db.get('access') == 'public']}
        return result
    monkeypatch.setattr(browser, '_api_get', public_only)
    # one visible collection: the front page jumps into it, as the landing page always did; ?all=1 stays
    assert client.get('/', follow_redirects=False).headers['location'] == '/browse/GroupA'
    html = client.get('/?all=1').text
    assert '2 datasets in 1 collections' in html


def test_old_landing_links_go_to_the_collection_list(client):
    for qs in ('q=microglia', 'disease=normal', 'view=table', 'org=human|mouse'):
        response = client.get('/?' + qs, follow_redirects=False)
        assert response.status_code == 302 and unquote(response.headers['location']) == '/collections?' + qs
    assert client.get('/?all=1', follow_redirects=False).status_code == 200
    collections = client.get('/collections').text
    assert 'Group A Study' in collections and '<title>Datasets · anthive</title>' in collections
    assert client.get('/datasets', follow_redirects=False).headers['location'] == '/collections'


def test_nav_has_home_as_preview_a_search_field_and_datasets_on_the_collection_list(client):
    page = client.get('/help').text
    nav = page.split('<nav aria-label="Main">')[1].split('</nav>')[0]
    home = nav.split('id="nav-home"')[1].split('</a>')[0]
    assert 'vw-preview' in nav.split('href="/"')[1].split('</a>')[0] and 'preview' in home
    assert 'vw-preview' not in nav.split('href="/collections"')[1].split('</a>')[0]
    form = page.split('id="topbar-search"')[1].split('</form>')[0]
    assert 'action="/search"' in page.split('id="topbar-search"')[0].rsplit('<form', 1)[1] + form
    assert 'name="q"' in form and 'id="topbar-search-toggle"' in form and 'aria-expanded="false"' in form
    # outside the phone menu panel, so the icon stays in the bar
    assert page.index('id="topbar-search"') < page.index('id="topbar-menu"')
    for path in ('/', '/collections', '/browse/GroupA/ds1', '/genes', '/diseases'):
        assert 'id="topbar-search-input"' in client.get(path).text, path


def test_search_field_keeps_the_query_on_the_results_page(client):
    html = client.get('/search', params={'q': 'microglia'}).text
    assert re.search(r'id="topbar-search-input" value="microglia"', html)


def test_search_groups_top_few_and_show_all(client):
    html = client.get('/search', params={'q': 'microglia'}).text
    artefacts = html.split('id="search-artefacts"')[1].split('</section>')[0]
    assert artefacts.count('<li>') == 5 and 'show all 7 artefacts' in artefacts
    assert 'href="/search?q=microglia&amp;kind=artefacts"' in artefacts
    assert 'href="/artefacts/GroupA/ds1/umap-1"' in artefacts and 'withdrawn' in artefacts
    assert 'id="search-collections"' in html and 'href="/browse/GroupA"' in html
    assert 'id="search-facets"' not in html                       # no facet matches microglia: group hidden
    one = client.get('/search', params={'q': 'microglia', 'kind': 'artefacts'}).text
    assert one.split('id="search-artefacts"')[1].split('</section>')[0].count('<li>') == 7
    assert 'id="search-datasets"' not in one and '<title>Artefacts · Search · anthive</title>' in one


def test_search_finds_help_and_facets_and_respects_ui_facets(client, monkeypatch):
    html = client.get('/search', params={'q': 'sex'}).text
    assert 'href="/facets/ant_sex"' in html.split('id="search-facets"')[1].split('</section>')[0]
    assert 'href="/help#' in html.split('id="search-help"')[1].split('</section>')[0]
    monkeypatch.setattr(browser, '_ui_cfg', {'facets': False, 'caveats': True, 'api': True, 'mcp': True, 'jupyter': True})
    assert 'id="search-facets"' not in client.get('/search', params={'q': 'sex'}).text


def test_search_results_grouped_by_kind(client):
    html = client.get('/search', params={'q': "TREM2 expression in microglia across Alzheimer's datasets"}).text
    assert '<title>Search · anthive</title>' in html and 'data-view="search"' in html
    genes = html.split('id="search-genes"')[1].split('</section>')[0]
    assert 'href="/genes/TREM2"' in genes
    datasets = html.split('id="search-datasets"')[1].split('</section>')[0]
    assert 'href="/browse/GroupA/ds1"' in datasets
    assert 'href="/browse/GroupA/ds2"' not in datasets        # astrocytes, controls only
    assert 'found in' in datasets
    assert any(call.startswith('/search?') for call in CALLS)
    assert 'id="search-papers"' not in html                     # no library on this site


def test_search_with_nothing_left_and_an_empty_query(client):
    assert client.get('/search', params={'q': ' '}, follow_redirects=False).headers['location'] == '/'
    html = client.get('/search', params={'q': 'in the'}).text
    assert 'No search words left' in html


def test_search_on_an_older_api_says_so(client, monkeypatch):
    def old_api(api_url, path, **kwargs):
        if path.startswith('/search?'):
            raise requests.HTTPError('404')
        return _fake_api_get(api_url, path, **kwargs)
    monkeypatch.setattr(browser, '_api_get', old_api)
    html = client.get('/search', params={'q': 'microglia'}).text
    assert 'id="search-error"' in html and 'API 2.48' in html


def test_browse_by_disease_and_cell_type(client):
    html = client.get('/diseases').text
    assert 'data-view="browse-disease"' in html and '<title>Browse by disease · anthive</title>' in html
    table = html.split('id="browse-counts"')[1].split('</table>')[0]
    assert 'href="/diseases/alzheimer%20disease"' in table and 'control donors' in table
    assert table.index('alzheimer disease') < table.index('control donors')
    assert 'id="browse-coverage"' in html and 'datasets record a disease' in html
    one = client.get('/diseases/alzheimer%20disease').text
    assert 'href="/browse/GroupA/ds1"' in one and 'href="/browse/GroupA/ds2"' not in one
    cells = client.get('/cell-types').text
    assert 'data-view="browse-celltype"' in cells and 'href="/cell-types/microglia"' in cells
    missing = client.get('/cell-types/no-such-type')
    assert missing.status_code == 404 and 'Browse by cell type' in missing.text


@pytest.mark.parametrize('key', ['front-door', 'search', 'browse-disease', 'browse-celltype'])
def test_new_views_are_registered_and_not_approved(key):
    assert key in browser.VIEW_REGISTRY and not browser.view_approved(key)


# ── dataset page: reading view and Technical details ──────────────────────────

def test_dataset_reading_view_tabs_and_closed_technical_details(client):
    html = client.get('/browse/GroupA/ds1').text
    bar = html.split('id="ds-tab-bar"')[1].split('</div>')[0]
    assert 'data-tab="num"' not in bar and 'data-tab="cat"' not in bar
    tech_row = html.split('class="tech-row"')[1].split('</div>\n</div>')[0]
    assert 'id="tech-toggle" aria-expanded="false" aria-controls="ds-tech-bar"' in tech_row
    assert re.search(r'id="ds-tech-bar"[^>]*hidden', tech_row)
    assert re.findall(r'data-tab="([^"]+)"', tech_row) == ['num', 'cat', 'facets']
    # old deep links still reach the panels: the JS opens the bar for a technical tab
    assert 'if (isTechTab(name)) setTech(true);' in html
    assert "if (tabPart === 'technical') { setTech(true); return; }" in html
    assert "name === 'facets-de'" in html
    for panel in ('tab-num', 'tab-cat', 'tab-facets', 'tab-facet-gene', 'tab-de'):
        assert f'id="{panel}"' in html, panel


def test_dataset_data_tab_shows_human_labels_for_standard_columns(client):
    html = client.get('/browse/GroupA/ds1').text
    data_tab = html.split('id="tab-data"')[1].split('</section>')[0]
    card = data_tab.split('id="ds-annotations"')[1].split('</table>')[0]
    rows = re.findall(r'<tr class="ann-row" data-col="([^"]+)"><td class="colname"[^>]*>([^<]+)<span class="ann-raw">', card)
    assert rows, card
    for column, label in rows:
        assert column.startswith('ant_') and not label.startswith('ant_'), (column, label)
    assert 'ant_donor_uid' not in [column for column, _ in rows]
    assert '.ds-tech-names .ann-raw' in html            # stored names appear only with Technical details open


def test_dataset_without_facets_ui_keeps_de_as_a_plain_tab(client, monkeypatch):
    monkeypatch.setattr(browser, '_ui_cfg', {'facets': False, 'caveats': True, 'api': True, 'mcp': True, 'jupyter': True})
    html = client.get('/browse/GroupA/ds1').text
    bar = html.split('id="ds-tab-bar"')[1].split('</div>')[0]
    assert 'tab-group-facet' not in bar and 'Facets &amp; DE' not in bar
    tech = html.split('id="ds-tech-bar"')[1].split('</div>')[0]
    assert 'data-tab="facets"' not in tech


# ── front page numbers (2026-10-26, round 3) ──────────────────────────────────

def _tiles(html):
    block = html.split('id="fd-stats"')[1].split('</div>')[0]
    return dict(re.findall(r'id="fd-tile-([a-z]+)"[^>]*>\s*<span class="fd-n">([^<]+)', block)), block


def test_front_door_shows_live_numbers_under_the_search_box(client):
    html = client.get('/').text
    assert html.index('class="fd-search"') < html.index('id="fd-stats"') < html.index('id="fd-browse-disease"')
    tiles, block = _tiles(html)
    assert tiles['datasets'] == '3' and tiles['collections'] == '2'
    assert 'genes' not in tiles and 'papers' not in tiles      # no gene index or library behind the fake API
    assert 'href="/collections"' in block and 'id="fd-tile-genesets"' not in block   # older API: no count
    assert 'primary datasets only' in block.split('id="fd-tile-cells"')[1].split('</a>')[0]
    assert 'id="fd-tile-species"' in block and 'human / mouse' in block


def test_front_door_numbers_come_from_api_stats_and_are_cached_per_access_level(client, monkeypatch):
    calls = []

    def with_stats(api_url, path, **kwargs):
        calls.append(path)
        if path == '/stats':
            return {'n_datasets': 3, 'n_collections': 2, 'n_cells_primary': 1500, 'n_genes': 27000,
                    'n_genesets': 128, 'n_papers': 346, 'n_artefacts': 2, 'n_human': 3, 'n_mouse': 0,
                    'n_single_nucleus': 2, 'n_single_cell': 1,
                    'recently_updated': [{'db_id': 'GroupA/ds1', 'group_id': 'GroupA', 'file_id': 'ds1',
                                          'title': 'Dataset One', 'collection': 'Group A Study',
                                          'updated': '2026-10-05'}]}
        return _fake_api_get(api_url, path, **kwargs)
    monkeypatch.setattr(browser, '_api_get', with_stats)
    monkeypatch.setattr(browser, '_library_available', lambda request: True)
    html = client.get('/').text
    tiles, _ = _tiles(html)
    assert tiles['genes'] == '27,000' and tiles['genesets'] == '128' and tiles['papers'] == '346'
    assert tiles['artefacts'] == '2' and tiles['cells'] == '1.5k'
    recent = html.split('id="fd-recent"')[1].split('</section>')[0]
    assert 'href="/browse/GroupA/ds1"' in recent and '2026-10-05' in recent
    client.get('/')
    assert calls.count('/stats') == 1                           # second visit served from the cache
    client.get('/', headers={'Remote-User': 'mf', 'Remote-Groups': 'admins'})
    assert calls.count('/stats') == 2                           # another access level asks again
