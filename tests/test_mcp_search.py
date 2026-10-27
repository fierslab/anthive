"""ant-mcp ``search`` (GET /search) and ``catalog_stats`` (GET /stats), API 2.48.

The tools are closures inside ``main()``; they are cut out with ``ast`` and run against canned API answers, as
in tests/test_mcp_catalogue_tools.py. Covered: the parameters sent are the ones the route declares, kind is
checked before any call, output is grouped by kind with paper passages as plain text, and the tools are listed
in the server instructions, the guide and the help page.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from html import unescape as html_unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tests.test_mcp_api_245 import FakeHttpx, StatusError

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-mcp'
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
TOOLS = ('search', 'catalog_stats')
HELPERS = ('_search_text',)

SEARCH = {
    'query': 'TREM2 microglia', 'words': ['trem2', 'microglia'], 'gene_words': ['trem2'],
    'genes': [{'gene': 'TREM2', 'n_datasets': 120}],
    'datasets': [{'db_id': 'S20/SIE', 'title': 'Sierksma microglia', 'collection': 'Sierksma2020',
                  'organism': ['mouse'], 'cell_types': ['microglia'], 'diseases': [], 'n_cells': 12000,
                  'designation': 'primary', 'matched_in': ['cell type', 'authors']}],
    'n_datasets': 1, 'datasets_basis': 'words',
    'collections': [], 'n_collections': 0, 'genesets': None, 'n_genesets': 0,
    'papers': [{'citekey': 'zhou2020human', 'citation': 'Zhou 2020', 'title': 'Human and mouse snRNA-seq',
                'matched_in': ['title', 'fulltext'], 'n_passages': 4,
                'passages': [{'page_start': 11, 'page_end': 12, 'section': 'results',
                              'snippet': 'loss of <mark>TREM2</mark> in <mark>microglia</mark> &amp; more'}]}],
    'n_papers': 1, 'artefacts': None, 'facets': None, 'help': [], 'n_help': 0, 'suggestions': [],
}
STATS = {'n_datasets': 153, 'n_primary': 127, 'n_human': 130, 'n_mouse': 19, 'n_microglia': 74,
         'n_single_nucleus': 84, 'n_single_cell': 41, 'organisms': {'human': 130, 'mouse': 19},
         'n_collections': 63, 'n_cells': 17028665, 'n_cells_primary': 14206574, 'n_genes': 24765,
         'n_genesets': 181, 'n_papers': None, 'n_artefacts': 2,
         'recently_updated': [{'db_id': 'Z20/ALL', 'title': 'Zhou 2020', 'updated': '2026-10-20'}]}


def _load(responses: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    calls: List[Tuple[str, Dict[str, Any]]] = []

    async def fake_get(path: str, **params):
        calls.append((path, {k: v for k, v in params.items() if v is not None}))
        answer = responses.get(path)
        if answer is None:
            raise StatusError(404, f'no canned answer for {path}')
        return answer

    tree = ast.parse(MCP_PATH.read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    body: List[ast.stmt] = []
    for node in ast.walk(main):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in TOOLS:
            node.decorator_list = []
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in HELPERS + ('_count_text',):
            body.insert(0, node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_SEARCH_KINDS'
                                                  for t in node.targets):
            body.insert(0, node)
    module_level = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_count_text']
    namespace: Dict[str, Any] = {'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional, 're': re,
                                 'json': json, 'httpx': FakeHttpx, '_get': fake_get,
                                 'html_unescape': html_unescape}
    exec(compile(ast.fix_missing_locations(ast.Module(body=module_level + body, type_ignores=[])),
                 str(MCP_PATH), 'exec'), namespace)
    assert set(TOOLS) <= set(namespace)
    return namespace, calls


def _route_params(path: str) -> set:
    tree = ast.parse(SERVE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and decorator.args and \
                        isinstance(decorator.args[0], ast.Constant) and decorator.args[0].value == path:
                    return {arg.arg for arg in node.args.args} - {'request'}
    raise AssertionError(f'no route {path}')


def _run(coroutine):
    return asyncio.run(coroutine)


def test_search_sends_declared_parameters_and_groups_the_answer():
    namespace, calls = _load({'/search': SEARCH})
    text = _run(namespace['search']('TREM2 microglia', kind='datasets, papers', limit=500))
    assert calls == [('/search', {'q': 'TREM2 microglia', 'limit': 50, 'kind': 'datasets,papers'})]
    assert set(calls[0][1]) <= _route_params('/search')
    assert 'GENES (1)' in text and 'TREM2: measured in 120 dataset(s)' in text
    assert 'DATASETS (1)' in text and 'S20/SIE' in text and 'found in: cell type, authors' in text
    assert 'PAPERS (1;' in text and 'zhou2020human' in text and 'found in: title, fulltext' in text
    assert 'results, p.11-12: loss of TREM2 in microglia & more' in text      # plain text, no markup
    assert '<mark>' not in text and "citekeys=['zhou2020human']" in text      # more passages than shown
    assert 'GENE SETS' not in text and 'ARTEFACTS' not in text                # null and empty groups left out


def test_search_refuses_unknown_kind_before_calling():
    namespace, calls = _load({'/search': SEARCH})
    assert 'Unknown kind planets' in _run(namespace['search']('x', kind='planets'))
    assert calls == []


def test_search_with_nothing_found():
    empty = {'query': 'zzz', 'words': ['zzz'], 'datasets': [], 'n_datasets': 0, 'papers': [], 'n_papers': 0,
             'suggestions': []}
    namespace, _ = _load({'/search': empty})
    assert 'Nothing found' in _run(namespace['search']('zzz'))


def test_catalog_stats_text():
    namespace, calls = _load({'/stats': STATS})
    text = _run(namespace['catalog_stats']())
    assert calls == [('/stats', {})]
    assert 'Datasets: 153 (127 primary' in text and 'Papers: not on this site' in text
    assert 'Z20/ALL' in text and 'donor' not in text.lower()


def test_new_tools_are_listed_where_tools_are_listed():
    source = MCP_PATH.read_text()
    assert "'  search           start here" in source and "'  catalog_stats    " in source
    assert '1. Find data: `search`' in source
    help_html = (REPO_ROOT / 'web' / 'templates' / 'help.html').read_text()
    for tool in TOOLS:
        assert f'<code>{tool}</code>' in help_html, tool
