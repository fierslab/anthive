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
PAPERS = []       # library record hits the fake /search returns (set by a test)
FULLTEXT = []     # full-text hits the fake /search returns (set by a test)


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
        kinds = [k for k in parse_qs(urlparse(path).query).get('kind', [''])[0].split(',') if k]
        return catalog_search.search(query, databases, gene_index=index, genesets=[], labels={},
                                     artefacts=artefacts, facets=facets, help_hidden=[],
                                     papers=lambda words: list(PAPERS), paper_fulltext=lambda groups: list(FULLTEXT),
                                     limit=int(parse_qs(urlparse(path).query).get('limit', ['50'])[0]), kinds=kinds)
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
    PAPERS.clear()
    FULLTEXT.clear()
    monkeypatch.setattr(browser, '_home_collection_cfg', [''])
    browser._HOME_COLLECTION_WARNED.clear()
    app = FastAPI()
    app.state.api_url = 'http://testapi'
    app.state.reports = []
    app.state.genesets_available = True
    app.include_router(browser.router)
    browser.install_error_pages(app)
    return TestClient(app)


def test_front_door_is_minimal_logo_search_browse_line_and_hidden_examples(client):
    html = client.get('/').text
    page = html.split('id="front-door"')[1].split('</main>')[0]
    assert '<title>Find data · anthive</title>' in html
    # approved (2026-10-27): no review banner, no preview marks on the page itself
    assert 'class="review-banner"' not in html and 'data-view="front-door"' not in html
    assert 'exp-badge' not in page and 'Preview.' not in page
    # the logo above the box, no tagline
    logo = page.split('id="fd-logo"')[1].split('</h1>')[0]
    assert '<img src="data:image/png;base64,' in logo and 'alt="anthive"' in logo
    assert 'Search datasets, genes' not in page and '<h1>Search' not in html
    form = page.split('class="fd-search"')[1].split('</form>')[0]
    assert 'action="/search"' in page and 'name="q"' in form
    # no numbers, no recently updated list, no "What is here"
    for gone in ('id="fd-stats"', 'fd-tile', 'id="fd-recent"', 'Recently updated', 'id="fd-what-here"',
                 'data-help-for="fd-what"', 'What is here'):
        assert gone not in html, gone
    # one small browse line under the box
    browse = page.split('id="fd-browse"')[1].split('</nav>')[0]
    for link in ('href="/diseases"', 'href="/cell-types"', 'href="/collections"'):
        assert link in browse
    assert page.index('class="fd-search"') < page.index('id="fd-browse"')
    assert '—' not in page


def test_front_door_examples_are_hidden_until_a_minute_without_activity(client):
    html = client.get('/').text
    examples = re.search(r'<p class="fd-try" id="fd-examples"([^>]*)>(.*?)</p>', html, re.S)
    assert examples, 'example paragraph missing'
    attributes, body = examples.groups()
    assert ' hidden' in attributes and 'data-delay-ms="60000"' in attributes
    assert body.count('href="/search?q=') == 2 and 'TREM2' in body and 'APOE4' in body
    assert browser.FRONT_DOOR_EXAMPLES_DELAY_MS == 60_000
    script = html.split("getElementById('fd-examples')")[1].split('</script>')[0]
    for event in ("'keydown'", "'input'", "'pointermove'", "'scroll'", "'touchstart'"):
        assert event in script, event
    assert "addEventListener('focus', restart)" in script            # focus in the search box counts
    assert 'clearTimeout(timer)' in script and 'setTimeout(show, delay)' in script
    assert 'if (shown) return;' in script                           # once shown they stay
    assert 'prefers-reduced-motion: reduce' in script \
        and 'prefers-reduced-motion: reduce' in html.split('@keyframes fd-fade-in')[1]


def test_home_always_shows_the_front_door(client, monkeypatch):
    monkeypatch.setattr(browser, '_home_collection_cfg', ['GroupA'])
    response = client.get('/home', follow_redirects=False)
    assert response.status_code == 200 and 'id="front-door"' in response.text
    # an empty search goes back to the front door, not to the configured landing
    assert client.get('/search', params={'q': ' '}, follow_redirects=False).headers['location'] == '/home'


def test_configured_home_collection_opens_its_page(client, monkeypatch):
    monkeypatch.setattr(browser, '_home_collection_cfg', ['GroupB'])
    response = client.get('/', follow_redirects=False)
    assert response.status_code == 302 and response.headers['location'] == '/browse/GroupB'
    # the folder name (collection.group) works as well as the group_id, case ignored
    monkeypatch.setattr(browser, '_home_collection_cfg', ['group a study'])
    assert client.get('/', follow_redirects=False).headers['location'] == '/browse/GroupA'
    # ?all=1 and /home still reach the front door; the collection list stays at /collections
    assert 'id="front-door"' in client.get('/?all=1').text
    assert 'Group A Study' in client.get('/collections').text
    # the logo goes to "/", the configured landing; the nav home link always to the front door
    page = client.get('/help').text
    assert '<a href="/" class="brand"' in page
    assert 'href="/home" id="nav-home"' in page.split('<nav aria-label="Main">')[1].split('</nav>')[0]


def test_unknown_or_invisible_home_collection_falls_back_to_the_front_door(client, monkeypatch, capsys):
    monkeypatch.setattr(browser, '_home_collection_cfg', ['NoSuchStudy'])
    assert client.get('/', follow_redirects=False).status_code == 200
    client.get('/')
    assert capsys.readouterr().out.count("web_home_collection 'NoSuchStudy'") == 1   # warned once

    def public_only(api_url, path, **kwargs):
        result = _fake_api_get(api_url, path, **kwargs)
        if path == '/databases':      # an anonymous visitor does not see the internal GroupB
            result = {'databases': [db for db in result['databases'] if db.get('access') == 'public']}
        return result
    monkeypatch.setattr(browser, '_api_get', public_only)
    monkeypatch.setattr(browser, '_home_collection_cfg', ['GroupB'])
    # GroupB invisible: the usual rule applies (one visible collection, so straight into it)
    assert client.get('/', follow_redirects=False).headers['location'] == '/browse/GroupA'


def test_front_door_counts_only_what_the_api_returns_for_the_visitor(client, monkeypatch):
    def public_only(api_url, path, **kwargs):
        result = _fake_api_get(api_url, path, **kwargs)
        if path == '/databases':      # ant-serve leaves internal datasets out for an anonymous visitor
            result = {'databases': [db for db in result['databases'] if db.get('access') == 'public']}
        return result
    monkeypatch.setattr(browser, '_api_get', public_only)
    # one visible collection: the front page jumps into it, as the landing page always did; ?all=1 stays
    assert client.get('/', follow_redirects=False).headers['location'] == '/browse/GroupA'
    assert 'id="front-door"' in client.get('/?all=1').text


def test_old_landing_links_go_to_the_collection_list(client):
    for qs in ('q=microglia', 'disease=normal', 'view=table', 'org=human|mouse'):
        response = client.get('/?' + qs, follow_redirects=False)
        assert response.status_code == 302 and unquote(response.headers['location']) == '/collections?' + qs
    assert client.get('/?all=1', follow_redirects=False).status_code == 200
    collections = client.get('/collections').text
    assert 'Group A Study' in collections and '<title>Datasets · anthive</title>' in collections
    assert client.get('/datasets', follow_redirects=False).headers['location'] == '/collections'


def test_nav_home_is_the_front_door_and_the_search_button_is_on_every_other_page(client):
    page = client.get('/help').text
    nav = page.split('<nav aria-label="Main">')[1].split('</nav>')[0]
    home = nav.split('id="nav-home"')[1].split('</a>')[0]
    assert 'href="/home" id="nav-home"' in nav and 'vw-preview' not in home and 'preview' not in home
    assert 'vw-preview' not in nav.split('href="/collections"')[1].split('</a>')[0]
    # the logo goes to "/" (the site's landing); About moved into the user menu
    assert '<a href="/" class="brand" id="topbar-brand"' in page
    assert 'href="/about" id="about-menu-link"' in page
    # round 6: a search button opens a small search box; not on the front door, which is the search page
    for path in ('/', '/home'):
        html = client.get(path).text
        assert 'id="topbar-search"' not in html and 'id="topbar-search-btn"' not in html, path
    for path in ('/collections', '/browse/GroupA/ds1', '/genes', '/diseases', '/search?q=microglia', '/help'):
        html = client.get(path).text
        box = html.split('<div class="topbar-search" id="topbar-search">')[1].split('</div>\n        </div>')[0]
        assert 'id="topbar-search-btn" aria-label="Search anthive" aria-haspopup="dialog" aria-expanded="false"' \
            ' aria-controls="topbar-search-pop"' in box, path
        assert '<div class="ts-pop" id="topbar-search-pop" role="dialog" aria-label="Search anthive" hidden>' in box
        # the same form as the front page: GET /search with q
        assert '<form class="fd-search" action="/search" method="get" role="search">' in box
        assert 'name="q"' in box, path
        # outside the phone menu panel, so it stays visible on phones
        assert html.index('id="topbar-search"') < html.index('id="topbar-menu"')
    # the old always-visible field and its phone toggle stay gone
    for gone in ('id="topbar-search-toggle"', 'id="topbar-search-input"', 'search-open'):
        assert gone not in client.get('/collections').text, gone


def test_search_button_script_closes_and_returns_focus():
    base = (Path(__file__).resolve().parent.parent / 'web' / 'templates' / 'base.html').read_text()
    script = base.split("var pop = document.getElementById('topbar-search-pop');")[1].split('})();')[0]
    assert "e.key === 'Escape' && isOpen()" in script and 'btn.focus()' in script
    assert '!pop.contains(e.target)' in script                  # a click outside closes it
    assert "e.key !== '/'" in script and "getElementById('cat-q')" in script and 'INPUT|TEXTAREA|SELECT' in script
    assert 'input.focus()' in script


def test_search_results_page_keeps_its_own_box_with_the_query(client):
    html = client.get('/search', params={'q': 'microglia'}).text
    box = html.split('id="search-results"')[1].split('class="fd-search"')[1].split('</form>')[0]
    assert 'name="q"' in box and 'value="microglia"' in box
    assert '<form class="fd-search" action="/search"' in html


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


def test_show_all_asks_the_api_for_that_kind_only(client):
    client.get('/search', params={'q': 'microglia', 'kind': 'artefacts'})
    call = next(call for call in CALLS if call.startswith('/search?'))
    assert parse_qs(urlparse(call).query)['kind'] == ['artefacts']
    CALLS.clear()
    client.get('/search', params={'q': 'microglia'})
    assert 'kind' not in parse_qs(urlparse(next(c for c in CALLS if c.startswith('/search?'))).query)


def test_search_shows_paper_passages_and_where_the_words_were_found(client, monkeypatch):
    monkeypatch.setattr(browser, '_library_available', lambda request: True)
    PAPERS.append({'citekey': 'smith2024', 'title': 'Microglia in plaques', 'authors': ['Smith, Anna'],
                   'citation': 'Smith 2024', 'journal': 'Nature'})
    FULLTEXT.extend([
        {'citekey': 'smith2024', 'summary': {}, 'score': 3.0, 'n_passages': 5,
         'passages': [{'page_start': 4, 'page_end': 5, 'section': 'results',
                       'snippet': 'reactive <mark>microglia</mark> &lt;b&gt;near&lt;/b&gt; plaques'}]},
        {'citekey': 'lee2020', 'summary': {'citekey': 'lee2020', 'title': 'Glia atlas', 'citation': 'Lee 2020'},
         'score': 1.0, 'n_passages': 1,
         'passages': [{'page_start': 2, 'page_end': 2, 'section': 'figure_legend',
                       'snippet': '<script>alert(1)</script><mark>microglia</mark> cluster'}]}])
    html = client.get('/search', params={'q': 'microglia'}).text
    papers = html.split('id="search-papers"')[1].split('</section>')[0]
    assert papers.index('smith2024') < papers.index('lee2020')
    assert 'found in title, full text' in papers and 'found in full text' in papers
    assert '<ul class="fd-passages"><li><span class="snip">reactive <mark>microglia</mark> &lt;b&gt;near&lt;/b&gt; plaques</span>' \
        ' <span class="where">results, pp. 4-5</span>' in papers
    assert 'figure legend, p. 2' in papers
    assert '<script>alert(1)</script>' not in papers and '&lt;script&gt;' in papers   # only <mark> survives
    assert '4 more matching passages in this paper' in papers


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
    assert client.get('/search', params={'q': ' '}, follow_redirects=False).headers['location'] == '/home'
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


@pytest.mark.parametrize('key', ['search', 'browse-disease', 'browse-celltype'])
def test_new_views_are_registered_and_not_approved(key):
    assert key in browser.VIEW_REGISTRY and not browser.view_approved(key)


def test_front_door_view_is_approved():
    assert browser.view_approved('front-door')


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
