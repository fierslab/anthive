"""ant-mcp: reanalyses (is_secondary: true) are visible in listings by default, statistics still leave them out.

Mark, 2.47.0: listing and discovery show every dataset, a reanalysis marked "(reanalysis of <db_id>)";
``search_datasets(exclude_secondary=true)`` still gives primary datasets only. The facet tests, replication
counts, donor variability and other counting tools keep ``exclude_secondary=true`` by default (a reanalysis
reuses the same cells and would count them twice).
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

MCP_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-mcp'
MCP_TREE = ast.parse(MCP_PATH.read_text())

DATABASES = [
    {'id': 'P26/PRI', 'title': 'Primary', 'is_secondary': False, 'designation': 'primary', 'organism': 'human'},
    {'id': 'R26/REA', 'title': 'Reanalysis', 'is_secondary': True, 'designation': 'reanalysis',
     'reanalysis_of': ['P26/PRI'], 'organism': 'human'},
]


def _tools() -> Dict[str, ast.AST]:
    main = next(n for n in MCP_TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    return {node.name: node for node in ast.walk(main)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == 'tool'
                    for d in node.decorator_list)}


def _search_datasets():
    tree = ast.parse(MCP_PATH.read_text())
    helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name in ('_count_text', '_reanalysis_mark', '_lineage_line')]
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    tool = next(n for n in ast.walk(main) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'search_datasets')
    tool.decorator_list = []

    async def fake_get(path: str, **params):
        assert path == '/databases'
        return {'databases': DATABASES}

    namespace: Dict[str, Any] = {'Optional': Optional, 'List': List, 'Dict': Dict, 'Any': Any, 'json': json,
                                 '_get': fake_get}
    exec(compile(ast.fix_missing_locations(ast.Module(body=helpers + [tool], type_ignores=[])), str(MCP_PATH), 'exec'),
         namespace)
    return namespace['search_datasets']


def test_reanalyses_are_listed_and_marked_by_default():
    reply = asyncio.run(_search_datasets()())
    assert 'Found 2 dataset(s)' in reply and 'left out' not in reply
    assert 'db_id:      R26/REA (reanalysis of P26/PRI)' in reply
    assert 'db_id:      P26/PRI\n' in reply


def test_primary_only_on_request():
    reply = asyncio.run(_search_datasets()(exclude_secondary=True))
    assert 'Found 1 dataset(s) (1 reanalyses left out' in reply and 'R26/REA' not in reply


def test_listing_default_false_statistics_default_true():
    tools = _tools()

    def default(name):
        node = tools[name]
        names = [a.arg for a in node.args.args]
        defaults = dict(zip(names[len(names) - len(node.args.defaults):], node.args.defaults))
        return defaults['exclude_secondary'].value

    assert default('search_datasets') is False
    counting = [name for name, node in tools.items()
                if name != 'search_datasets' and 'exclude_secondary' in [a.arg for a in node.args.args]]
    assert counting, 'the counting tools take exclude_secondary'
    for name in counting:
        assert default(name) is True, name
    doc = ast.get_docstring(tools['search_datasets'])
    assert '(reanalysis of' in doc and 'count them twice' in doc


def test_missing_counts_do_not_break_the_listing():
    # '?' with the ',' format raised ValueError when a catalog entry had no n_cells
    reply = asyncio.run(_search_datasets()())
    assert 'cells:      ?    genes: ?' in reply
