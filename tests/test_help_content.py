"""Tests for lib/help_content, the /help REST endpoints and the MCP explain tool.

The REST handlers and the MCP helpers live in single-file uv scripts, so (as in
tests/test_facet_core.py) the wanted nodes are cut out of the script source
with ``ast`` and executed against a stub FastAPI app.
"""
import ast
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

import help_content

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
MCP_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-mcp'
SKILL_PATH = REPO_ROOT / '.claude' / 'skills' / 'anthive' / 'SKILL.md'

REQUIRED_TOPICS = (
    'what', 'pages', 'dataset-versions', 'dataset-caveats', 'differential-expression', 'genes', 'module-scores',
    'per-cell-per-donor', 'facet-effects', 'facet-replicate-unit', 'facet-scopes', 'donor-groups', 'facet-counting',
    'facet-howto', 'papers', 'library-search',
)


# ── content integrity ────────────────────────────────────────────────────────

def test_required_topics_exist():
    ids = {topic.id for topic in help_content.topics()}
    assert set(REQUIRED_TOPICS) <= ids


def test_every_topic_file_is_valid_and_ids_are_unique():
    paths = sorted((help_content.CONTENT_DIR / 'topics').glob('*.yaml'))
    ids = [yaml.safe_load(path.read_text())['id'] for path in paths]
    assert ids == [path.stem for path in paths]
    assert len(help_content.topics()) == len(paths)


def test_summaries_are_short_plain_sentences():
    for topic in help_content.topics():
        assert 20 < len(topic.summary) < 400, topic.id
        assert '<' not in topic.summary and '\n' not in topic.summary, topic.id
        assert len(re.findall(r'[.!?](?:\s|$)', topic.summary)) <= 2, topic.id


def test_no_em_dashes_anywhere_in_the_content():
    for path in help_content.CONTENT_DIR.rglob('*.yaml'):
        assert '—' not in path.read_text(encoding='utf-8'), path.name


def test_every_non_section_topic_is_a_child_of_exactly_one_topic():
    parents: Dict[str, List[str]] = {}
    for topic in help_content.topics():
        for child in topic.children:
            parents.setdefault(child, []).append(topic.id)
    assert all(len(owners) == 1 for owners in parents.values())
    assert 'facet-replicate-unit' in parents and parents['facet-scopes'] == ['facet-effects']


def test_glossary_is_complete_and_slugs_are_anchor_safe():
    slugs = [term.slug for term in help_content.glossary()]
    assert len(slugs) == len(set(slugs)) > 20
    assert all(re.fullmatch(r'[a-z0-9-]+', slug) for slug in slugs)
    assert {'pseudobulk', 'replicate-unit', 'r-value', 'scope', 'fdr', 'k-of-m'} <= set(slugs)


def test_body_links_to_topics_and_glossary_resolve():
    ids = {topic.id for topic in help_content.topics()}
    slugs = {term.slug for term in help_content.glossary()}
    page_anchors = {'quickstart', 'mcp', 'mcp-clients', 'mcp-offers', 'skills', 'servers', 'ai-practice', 'ai', 'code',
                    'account', 'glossary', 'skills-claude-apps'}
    for topic in help_content.topics():
        for target in re.findall(r'href="#([a-z0-9-]+)"', topic.body_html):
            if target.startswith('glossary-'):
                assert target[len('glossary-'):] in slugs, (topic.id, target)
            else:
                assert target in ids | page_anchors, (topic.id, target)


def test_hint_topic_ids_used_by_the_api_exist():
    source = SERVE_PATH.read_text()
    hinted = set(re.findall(r"'([a-z-]+)'", re.search(r"_FACET_HELP_TOPICS = \(([^)]*)\)", source).group(1)))
    hinted |= set(re.findall(r"'((?:dataset|facet|per)[a-z-]+)'", source.split('def database_context')[1].split('@app')[0]))
    assert hinted and all(help_content.get_topic(topic_id) for topic_id in hinted), hinted


def test_loader_rejects_unknown_references(tmp_path, monkeypatch):
    topics_dir = tmp_path / 'topics'
    topics_dir.mkdir()
    (tmp_path / 'glossary.yaml').write_text('terms: []\n')
    (topics_dir / 'a.yaml').write_text('id: a\ntitle: A\nsummary: S.\nbody_html: "<p>x</p>"\nrelated: [nope]\n')
    monkeypatch.setattr(help_content, 'CONTENT_DIR', tmp_path)
    help_content.reload()
    try:
        with pytest.raises(ValueError, match='unknown topic'):
            help_content.topics()
        (topics_dir / 'a.yaml').write_text('id: b\ntitle: A\nsummary: S.\nbody_html: "<p>x</p>"\n')
        help_content.reload()
        with pytest.raises(ValueError, match='file name'):
            help_content.topics()
        (topics_dir / 'a.yaml').write_text('id: a\ntitle: A\nbody_html: "<p>x</p>"\n')
        help_content.reload()
        with pytest.raises(ValueError, match='missing'):
            help_content.topics()
    finally:
        monkeypatch.undo()
        help_content.reload()


# ── html to markdown ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('fragment, expected', [
    ('<p>One <strong>two</strong> <em>three</em> <code>four</code>.</p>', 'One **two** *three* `four`.'),
    ('<p>See <a href="/x">the page</a>.</p>', 'See [the page](/x).'),
    ('<ul><li>a</li><li>b</li></ul>', '- a\n- b'),
    ('<ol><li>a</li><li>b</li></ol>', '1. a\n2. b'),
    ('<ul><li>a<ul><li>b</li></ul></li></ul>', '- a\n  - b'),
    ('<h3 id="x">Head</h3><p>Body</p>', '### Head\n\nBody'),
    ('<p>a<br>b</p>', 'a\nb'),
    ('<p>1 &lt; 2 &amp; 3</p>', '1 < 2 & 3'),
    ('<pre>x  =  1\ny</pre>', '```\nx  =  1\ny\n```'),
    ('<dl><dt>Term</dt><dd>Def</dd></dl>', '**Term**\nDef'),
    ('<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>',
     '| A | B |\n| --- | --- |\n| 1 | 2 |'),
    ('<p><a name="x"></a>text</p>', 'text'),
    ('<div><span>plain</span></div>', 'plain'),
])
def test_html_to_markdown(fragment, expected):
    assert help_content.html_to_markdown(fragment) == expected


def test_html_to_text_strips_markup():
    assert help_content.html_to_text('<p>A <strong>b</strong> <a href="/x">c</a> <code>d</code></p>') == 'A b c d'


def test_markdown_of_real_topics_has_no_html_or_template_leftovers():
    for topic in help_content.topics():
        markdown = help_content.topic_markdown(topic.id)
        assert markdown and '<' not in re.sub(r'`[^`]*`', '', markdown).replace('< ', '').replace('<-', ''), topic.id
        assert '{{' not in markdown and '{%' not in markdown, topic.id
    assert 'https://docs.example.org/doc/DESIGN' in help_content.topic_markdown('facet-limits', docs_url='https://docs.example.org/doc')


def test_docs_placeholder_is_filled_in_html_and_markdown():
    html = help_content.topic_html('facet-counting', 'https://docs.example.org/doc/')
    assert 'https://docs.example.org/doc/DESIGN_facet_testing.md' in html and '{{ docs }}' not in html
    assert help_content.fill('{{ docs }}/A.md', 'https://x/y/') == 'https://x/y/A.md'


# ── rendering ────────────────────────────────────────────────────────────────

def test_topic_html_renders_children_as_h3_with_the_anchor_id():
    html = help_content.topic_html('facet-effects')
    assert '<h3 class="subsection-head" id="facet-replicate-unit">Replicate unit and analysis level</h3>' in html
    assert html.index('id="facet-tested"') < html.index('id="facet-scopes"')
    assert 'id="facets-columns">Facets (<code>ant_*</code> columns)</h3>' in help_content.topic_html('pages')


def test_topic_html_unknown_topic_raises():
    with pytest.raises(KeyError):
        help_content.topic_html('nope')
    with pytest.raises(KeyError):
        help_content.topic_markdown('nope')
    with pytest.raises(KeyError):
        help_content.term_markdown('nope')


def test_glossary_html_has_the_anchor_per_term():
    html = help_content.glossary_html()
    for term in help_content.glossary():
        assert f'<dt id="glossary-{term.slug}">' in html


def test_topic_links_include_children_and_skip_self():
    related, terms = help_content.topic_links('facet-counting')
    assert 'facet-counting' not in related and not set(related) & set(help_content.get_topic('facet-counting').children)
    assert 'r-value' in terms and len(terms) == len(set(terms))
    with pytest.raises(KeyError):
        help_content.topic_links('nope')


def test_topic_markdown_children_switch():
    with_children = help_content.topic_markdown('facet-effects')
    without_children = help_content.topic_markdown('facet-effects', include_children=False)
    assert '### Scopes: human post-mortem and xenograft' in with_children and '### Scopes: human post-mortem and xenograft' not in without_children
    assert len(with_children) > len(without_children)


# ── lookup ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('query, kind, ident', [
    ('facet-counting', 'topic', 'facet-counting'),
    ('replicate unit', 'topic', 'facet-replicate-unit'),
    ('caveats', 'topic', 'dataset-caveats'),
    ('versions', 'topic', 'dataset-versions'),
    ('scopes', 'topic', 'facet-scopes'),
    ('Donor Groups', 'topic', 'donor-groups'),
    ('pseudobulk', 'term', 'pseudobulk'),
    ('r value', 'term', 'r-value'),
    ('pseudobulc', 'term', 'pseudobulk'),
])
def test_find_best_hit(query, kind, ident):
    hits = help_content.find(query)
    assert hits and any(hit['kind'] == kind and hit['id'] == ident for hit in hits[:3])


def test_find_nothing_and_blank():
    assert help_content.find('') == [] and help_content.find('   ') == []
    assert help_content.find('xyzzy quux') == []


def test_find_respects_limit_and_orders_by_score():
    hits = help_content.find('replication', limit=2)
    assert len(hits) <= 2
    assert hits == sorted(hits, key=lambda hit: -hit['score'])


def test_index_lists_topics_and_glossary():
    index = help_content.index()
    assert {topic['id'] for topic in index['topics']} >= set(REQUIRED_TOPICS)
    assert all(set(topic) == {'id', 'title', 'summary'} for topic in index['topics'])
    assert {'slug', 'term'} == set(index['glossary'][0])


# ── REST endpoints (cut out of ant-serve) ────────────────────────────────────

_SERVE_NAMES = {'_help_response', '_help_docs_url', 'help_index', 'help_search', 'help_glossary',
                'help_glossary_term', 'help_topic', '_with_help'}
_SERVE_ASSIGNS = {'_HELP_CACHE_HEADERS', '_FACET_HELP_TOPICS'}


@pytest.fixture
def serve_client():
    from fastapi import FastAPI, HTTPException, Query, Request, Response
    from fastapi.testclient import TestClient

    tree = ast.parse(SERVE_PATH.read_text())
    body = [node for node in tree.body if getattr(node, 'name', None) in _SERVE_NAMES
            or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in _SERVE_ASSIGNS
                                                     for target in node.targets))]
    app = FastAPI()
    namespace: Dict[str, Any] = {
        'app': app, 'Request': Request, 'Query': Query, 'Response': Response, 'HTTPException': HTTPException,
        'json': json, 'hashlib': hashlib, 'help_content': help_content, 'Dict': Dict, 'Any': Any, 'Optional': Optional,
        'get_docs_base_url': lambda cfg: 'https://docs.example.org/doc', 'get_config': lambda: {},
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return TestClient(app), namespace


def test_help_index_endpoint(serve_client):
    client, _ = serve_client
    response = client.get('/help')
    assert response.status_code == 200
    data = response.json()
    assert {topic['id'] for topic in data['topics']} >= set(REQUIRED_TOPICS)
    assert data['glossary'][0]['slug']
    assert 'public' in response.headers['cache-control'] and response.headers['etag']


def test_help_endpoints_are_cacheable_with_etag(serve_client):
    client, _ = serve_client
    first = client.get('/help')
    second = client.get('/help', headers={'If-None-Match': first.headers['etag']})
    assert second.status_code == 304 and second.content == b''
    other = client.get('/help', headers={'If-None-Match': '"stale"'})
    assert other.status_code == 200


def test_help_topic_endpoint(serve_client):
    client, _ = serve_client
    data = client.get('/help/facet-counting').json()
    assert data['id'] == 'facet-counting' and data['title'] and data['summary']
    assert 'r-value' in data['body'] and '<p>' not in data['body']
    assert 'facet-howto' in data['related'] and data['terms'] and data['anchor'] == '/help#facet-counting'
    parent = client.get('/help/facet-effects').json()
    assert '### Scopes: human post-mortem and xenograft' in parent['body'] and 'facet-scopes' in parent['children']
    assert 'https://docs.example.org/doc/DESIGN_facet_testing.md' in client.get('/help/facet-limits').json()['body']
    bare = client.get('/help/facet-effects', params={'include_children': 'false'}).json()
    assert '### Scopes: human post-mortem and xenograft' not in bare['body']


def test_help_topic_unknown_is_404_with_close_matches(serve_client):
    client, _ = serve_client
    response = client.get('/help/facet-countin')
    assert response.status_code == 404 and 'facet-counting' in response.json()['detail']


def test_help_glossary_endpoints(serve_client):
    client, _ = serve_client
    everything = client.get('/help/glossary').json()
    assert everything['format'] == 'markdown' and len(everything['terms']) == len(help_content.glossary())
    assert all('<' not in term['definition'] for term in everything['terms'][:5])
    as_html = client.get('/help/glossary', params={'format': 'html'}).json()
    html_by_slug = {term['slug']: term['definition'] for term in as_html['terms']}
    assert html_by_slug['replicate-unit'] == help_content.get_term('replicate-unit').definition_html
    one = client.get('/help/glossary/replicate-unit').json()
    assert one['term'] == 'Replicate unit' and 'Cells are never replicates' in one['definition']
    assert one['anchor'] == '/help#glossary-replicate-unit'
    missing = client.get('/help/glossary/pseudobulc')
    assert missing.status_code == 404 and 'pseudobulk' in missing.json()['detail']
    assert client.get('/help/glossary', params={'format': 'pdf'}).status_code == 422


def test_help_glossary_route_is_not_swallowed_by_the_topic_route(serve_client):
    client, _ = serve_client
    assert 'terms' in client.get('/help/glossary').json()
    assert 'matches' in client.get('/help/search', params={'q': 'scope'}).json()


def test_help_search_endpoint(serve_client):
    client, _ = serve_client
    data = client.get('/help/search', params={'q': 'replicate unit'}).json()
    assert data['query'] == 'replicate unit' and data['matches'][0]['score'] == 1.0
    assert client.get('/help/search', params={'q': 'xyzzy quux'}).json()['matches'] == []
    assert client.get('/help/search').status_code == 422


def test_with_help_adds_topics_to_dicts_only(serve_client):
    _, namespace = serve_client
    with_help = namespace['_with_help']
    assert with_help({'a': 1}, 'x', 'y') == {'a': 1, 'help_topics': ['x', 'y']}
    assert with_help([1], 'x') == [1]
    assert all(help_content.get_topic(topic_id) for topic_id in namespace['_FACET_HELP_TOPICS'])


def test_facet_and_context_endpoints_carry_help_topics():
    source = SERVE_PATH.read_text()
    assert source.count('_with_help(') >= 7
    assert "*_FACET_HELP_TOPICS" in source and "'dataset-caveats'" in source


# ── MCP (cut out of ant-mcp) ─────────────────────────────────────────────────

@pytest.fixture
def mcp_helpers():
    tree = ast.parse(MCP_PATH.read_text())
    wanted = {'format_help_index', 'format_help_topic', 'explain_via_api'}
    body = [node for node in tree.body if getattr(node, 'name', None) in wanted]
    namespace: Dict[str, Any] = {'Any': Any, 'Dict': Dict}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MCP_PATH), 'exec', dont_inherit=True), namespace)
    return namespace


def _fake_api(calls: List[str]):
    """A coroutine ``get`` answering the /help routes from lib/help_content."""
    async def get(path: str, **params: Any) -> dict:
        calls.append(path)
        if path == '/help':
            return help_content.index()
        if path == '/help/search':
            return {'matches': help_content.find(params['q'], limit=8)}
        if path.startswith('/help/glossary/'):
            slug = path.rsplit('/', 1)[1]
            return {'slug': slug, 'term': help_content.get_term(slug).term, 'definition': help_content.term_markdown(slug)}
        topic = help_content.get_topic(path.rsplit('/', 1)[1])
        return {'id': topic.id, 'title': topic.title, 'summary': topic.summary,
                'body': help_content.topic_markdown(topic.id), 'related': topic.related, 'terms': topic.terms}
    return get


def test_explain_returns_the_topic_text(mcp_helpers):
    calls: List[str] = []
    text = asyncio.run(mcp_helpers['explain_via_api'](_fake_api(calls), 'facet counting'))
    assert text.startswith('# Replication across cohorts and how to read it')
    assert 'Related topics:' in text and calls[:2] == ['/help/search', '/help/facet-counting']


def test_explain_returns_a_glossary_term(mcp_helpers):
    text = asyncio.run(mcp_helpers['explain_via_api'](_fake_api([]), 'pseudobulk'))
    assert text.startswith('# Pseudobulk (glossary)') and 'Other close matches' not in text.split('\n')[0]


def test_explain_lists_topics_when_unknown_or_empty(mcp_helpers):
    unknown = asyncio.run(mcp_helpers['explain_via_api'](_fake_api([]), 'xyzzy quux'))
    assert "No help topic or glossary term matches 'xyzzy quux'" in unknown and '`facet-counting`' in unknown
    empty = asyncio.run(mcp_helpers['explain_via_api'](_fake_api([]), ''))
    assert empty.startswith('# Anthive help topics') and 'Glossary terms' in empty
    assert asyncio.run(mcp_helpers['explain_via_api'](_fake_api([]), None)).startswith('# Anthive help topics')


def test_format_helpers(mcp_helpers):
    index = mcp_helpers['format_help_index']({'topics': [{'id': 'a', 'title': 'A', 'summary': 'S.'}], 'glossary': []})
    assert '- `a`: A. S.' in index and '(none)' in index
    topic = mcp_helpers['format_help_topic']({'title': 'T', 'summary': 'S.', 'body': 'B', 'related': ['x'], 'terms': ['y']})
    assert topic == '# T\n\nS.\n\nB\n\nRelated topics: `x`\nGlossary terms: `y`'
    assert mcp_helpers['format_help_topic']({'title': 'T', 'summary': 'S.', 'body': 'B'}) == '# T\n\nS.\n\nB'


def test_mcp_registers_explain_resources_and_instructions():
    source = MCP_PATH.read_text()
    tree = ast.parse(source)
    tools = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             for dec in node.decorator_list if isinstance(dec, ast.Call) and getattr(dec.func, 'attr', '') == 'tool'}
    assert 'explain' in tools
    assert "@mcp.resource('anthive://help'" in source and "@mcp.resource('anthive://help/{topic}'" in source
    instructions = source.split('instructions=(')[1].split("f'This MCP server is reachable")[0]
    assert 'EXPLAINING ANTHIVE' in instructions and 'call explain() and quote' in instructions
    assert '—' not in instructions.split('EXPLAINING ANTHIVE')[1].split('LITERATURE')[0]


# ── served skill ─────────────────────────────────────────────────────────────

def test_served_skill_points_at_the_help_endpoints():
    text = SKILL_PATH.read_text()
    assert text.startswith('---\nname: anthive\n')
    for needle in ('$API/help', '/help/search', 'explain(', 'help_topics', 'scope=', 'openapi.json',
                   'gitlab.kuleuven.be/bdslab/anthive/skills', 'github.com/badslab/labskills'):
        assert needle in text, needle
    assert '—' not in text and len(text.splitlines()) < 120
