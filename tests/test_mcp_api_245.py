"""ant-mcp against REST API 2.45: the tools added or changed in the 2.45 sync.

The tools are closures inside ``main()`` of the single-file uv script ``bin/api/ant-mcp``, so they are cut out with
``ast`` (as in ``test_donor_variability_api.py``) and run against canned API answers through a fake ``_get``. Covered:
the per-geom plot routes (and that every parameter the tool sends is declared by that ant-serve route), the facet
overview, ``get_facet_tests`` up and down counts, the ``db_id`` filter and absolute link of ``facet_donor_values``,
the dataset mode of ``donor_variability``, gene set ranks, the stored (author or custom) DE tools, and the
server instructions and guide that point the model at ``explain()``.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import time
import contextvars
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-mcp'
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
MCP_SOURCE = MCP_PATH.read_text()
MCP_TREE = ast.parse(MCP_SOURCE)

MODULE_NAMES = ('PLOT_COMMON_PARAMS', 'PLOT_GEOM_PARAMS', 'de_method_level', 'de_level_note', 'DE_TECHNICAL_TERM',
                'DE_RULES', 'ANTHIVE_GUIDE', 'WARMING_UP_MESSAGE', 'ServerWarmingUp')
NESTED_NAMES = ('_fmt_number', 'plot', 'facet_overview', 'get_facet_tests', 'facet_donor_values', 'donor_variability',
                '_dataset_donor_variability', '_rank_phrase', '_geneset_rank_lines', 'get_geneset', 'list_module_scores',
                '_dataset_path', '_de_studies_by_dataset', '_de_study_line', 'list_de_studies', 'de_study', 'gene_de',
                'facet_effects')


def _module_level() -> List[ast.stmt]:
    nodes = []
    for node in MCP_TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in MODULE_NAMES:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in MODULE_NAMES for t in node.targets):
            nodes.append(node)
    return nodes


def _nested() -> Dict[str, ast.AST]:
    tree = ast.parse(MCP_SOURCE)  # fresh: the decorators are stripped below
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    found = {}
    for node in ast.walk(main):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NESTED_NAMES:
            node.decorator_list = []
            found[node.name] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == '_de_scan_state':
            found['_de_scan_state'] = node
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('_DE_SCAN_TTL', '_FACET_TOOL_RULES')
                                                  for t in node.targets):
            found[node.targets[0].id] = node
    return found


class StatusError(Exception):
    """Stands in for httpx.HTTPStatusError."""

    def __init__(self, status: int = 404, text: str = 'not found'):
        super().__init__(text)
        self.response = type('Response', (), {'status_code': status, 'text': text})()


class FakeHttpx:
    HTTPStatusError = StatusError
    HTTPError = StatusError


def _load(responses: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    """Namespace with the tools, plus the list of (path, params) the tools asked ant-serve for."""
    calls: List[Tuple[str, Dict[str, Any]]] = []

    async def fake_get(path: str, **params):
        calls.append((path, params))
        if path not in responses:
            raise StatusError(404, f"no canned answer for {path}")
        answer = responses[path]
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    async def fake_get_plot(path: str, **params):
        calls.append((path, params))
        return b'PNG', {'x-plot-caption': 'caption'}

    namespace: Dict[str, Any] = {
        'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional, 'Tuple': Tuple, 're': re, 'json': json,
        'asyncio': asyncio, 'time': time, 'quote': quote, 'urlencode': urlencode, 'httpx': FakeHttpx,
        '_get': fake_get, '_get_plot': fake_get_plot, '_make_plot_response': lambda data, fmt, caption: f'url:{fmt}:{caption}',
        '_current_identity': contextvars.ContextVar('identity', default=None),
        'public_api_url': 'https://anthive.example.org/api',
    }
    nested = _nested()
    body = _module_level() + [nested[name] for name in ('_FACET_TOOL_RULES', '_DE_SCAN_TTL', '_de_scan_state')]
    body += [nested[name] for name in NESTED_NAMES]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(MCP_PATH), 'exec'), namespace)
    return namespace, calls


def _run(coroutine):
    return asyncio.run(coroutine)


# ── plot: per-geom routes ─────────────────────────────────────────────────────

def _serve_route_params() -> Dict[str, set]:
    """Query parameter names of every /databases/{db_id}/plot/{geom} route in ant-serve (CommonPlotParams included)."""
    tree = ast.parse(SERVE_PATH.read_text())
    common = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'CommonPlotParams':
            init = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
            for arg in init.args.args[1:]:
                common.add('format' if arg.arg == 'output_format' else arg.arg)
    routes = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant)
                    and str(decorator.args[0].value).startswith('/databases/{db_id:path}/plot/')):
                geom = decorator.args[0].value.rsplit('/', 1)[-1]
                routes[geom] = {a.arg for a in node.args.args} | common
    return routes


def test_plot_tool_parameters_are_declared_by_each_per_geom_route():
    namespace, _ = _load({})
    routes = _serve_route_params()
    for geom, params in namespace['PLOT_GEOM_PARAMS'].items():
        assert geom in routes, geom
        missing = (params | namespace['PLOT_COMMON_PARAMS']) - routes[geom]
        assert not missing, (geom, missing)


def test_plot_tool_calls_the_per_geom_route_with_only_its_parameters():
    namespace, calls = _load({})
    assert _run(namespace['plot']('G24/MVL', 'violin', x='Gender', y='APOE', subset=['Gender:Female'])) == 'url:png:caption'
    path, params = calls[-1]
    assert path == '/databases/G24/MVL/plot/violin'
    assert params['x'] == 'Gender' and params['y'] == 'APOE' and params['subset'] == ['Gender:Female']
    assert not {'bins', 'gridsize', 'window', 'point_size', 'ci', 'geom'} & set(params)
    _run(namespace['plot']('G24/MVL', 'hexbin', x='APOE', y='TREM2', gridsize=40))
    assert calls[-1][0].endswith('/plot/hexbin') and calls[-1][1]['gridsize'] == 40 and 'point_size' not in calls[-1][1]
    text = _run(namespace['plot']('G24/MVL', 'bogus'))
    assert text.startswith("Unknown geom 'bogus'") and len(calls) == 2
    assert "'/plot'" not in MCP_SOURCE and "/plot'," not in MCP_SOURCE


# ── facet overview and facet tests ────────────────────────────────────────────

OVERVIEW = {
    'facet': 'braak', 'scope': 'human_postmortem', 'scopes': ['human_postmortem'],
    'headline': {'celltype_group': 'microglia', 'cohorts': 5, 'n_genes_in_family': 19480, 'n_replicated': 114,
                 'n_effect_gate': 111, 'n_strict': 2, 'strict_genes': ['PTPRG', 'MEIS1'], 'effect_gate_unit': '10% over three stages'},
    'cell_types': [{'celltype_group': 'microglia', 'n_cohorts': 5, 'n_genes_in_family': 19480, 'n_replicated': 114,
                    'n_effect_gate': 111, 'n_strict': 2, 'n_assigned': 111,
                    'genes': [{'gene': 'PTPRG', 'r_value': 5.8e-18, 'direction': 'up', 'median_logFC': 0.47, 'k': 4, 'm': 5,
                               'effect_gate_passed': True, 'strict': True, 'also_in': ['astrocyte'],
                               'detection': {'pct_median': 1.3}, 'low_detection': True, 'gene_flags': ['long_gene']}]}],
    'quiet_groups': [{'celltype_group': 'endothelial cell', 'n_cohorts': 2, 'n_genes_in_family': 9110, 'n_replicated': 0,
                      'n_effect_gate': 0, 'n_strict': 0, 'low_power': True}],
    'n_genes_unique': 112,
}


def test_facet_overview_prints_headline_groups_and_quiet_groups():
    namespace, calls = _load({'/facets/braak/effects/overview': OVERVIEW})
    text = _run(namespace['facet_overview']('braak', n=3))
    assert calls[-1] == ('/facets/braak/effects/overview', {'n': 3, 'scope': None, 'exclude_secondary': 'true'})
    assert '114 of 19480 testable genes replicate' in text and 'PTPRG, MEIS1' in text
    assert 'PTPRG | 5.80e-18 | up | 0.47 | 4 of 5 | passed | strict; also in astrocyte; low detection (1.3 percent' in text
    assert 'endothelial cell (2 cohorts, 9110 genes, LOW POWER' in text
    doc = ast.get_docstring(_nested()['facet_overview'])
    assert 'EXPLORATORY' in doc and "explain('facet-overview')" in doc and "explain('r-value')" in doc


FACET_TESTS = {
    'status': 'tested', 'n_units': 1, 'n_units_tested': 1, 'runs': {},
    'units': [{'facet': 'sex', 'cell_type': 'microglial cell', 'celltype_source': 'ant_cell_type', 'tested': True,
               'contrast': 'm_vs_f', 'level_counts': {'f': 10, 'm': 8}, 'n_donors': 18, 'covariates': ['age'],
               'genes': {'n_tested': 4000, 'n_q05': 9, 'n_up': 7, 'n_down': 2},
               'modules': [{'method': 'pseudobulk', 'source': 'gene_list', 'n_modules': 101, 'n_q05_param': 2,
                            'n_q05_perm': 1, 'n_q05': 2, 'n_up': 1, 'n_down': 1},
                           {'method': 'cell_mean', 'source': 'ucell', 'n_modules': 101, 'n_q05_param': 0,
                            'n_q05_perm': 0, 'n_q05': None, 'n_up': None, 'n_down': None}]}],
}


def test_get_facet_tests_summary_prints_up_and_down_counts():
    namespace, _ = _load({'/databases/G24/MMG/facet-tests': FACET_TESTS})
    text = _run(namespace['get_facet_tests']('G24/MMG'))
    assert 'genes q<0.05: 9 of 4000 (7 up, 2 down)' in text
    assert 'pseudobulk/gene_list 2|1 of 101, 2 by the replication rule (1 up, 1 down)' in text
    assert 'cell_mean/ucell 0|0 of 101;' not in text and 'cell_mean/ucell 0|0 of 101' in text
    assert "explain('fdr')" in ast.get_docstring(_nested()['get_facet_tests'])


# ── donor values: db_id and absolute link ─────────────────────────────────────

DONOR_VALUES = {
    'facet': 'braak', 'scope': 'human_postmortem', 'subject': {'gene': 'PTPRG'}, 'cell_type': 'microglia',
    'scale': 'log2 CPM', 'adjusted': False, 'note': 'values are unadjusted', 'anonymisation': 'sequential labels',
    'x_label': 'Braak stage',
    'units': [{'cohort_display': 'Lee2026', 'counted': True, 'db_id': 'L26/PSY', 'cell_type': 'microglial cell',
               'n_donors': 2, 'replicate_unit': 'donor', 'model': {'estimate': 0.4, 'unit': 'per Braak stage'},
               'covariates': ['sex'], 'available': True, 'facet_kind': 'numeric',
               'donors': [{'label': 'D01', 'facet_value': 0, 'value': 7.1, 'n_cells': 12}]}],
}


def test_facet_donor_values_filters_by_dataset_and_prints_an_absolute_link():
    namespace, calls = _load({'/facets/braak/donor-values': DONOR_VALUES})
    text = _run(namespace['facet_donor_values']('braak', 'microglia', gene='PTPRG', db_id='L26/PSY'))
    assert calls[-1][1]['db_id'] == 'L26/PSY'
    link = next(line for line in text.splitlines() if line.startswith('plot: '))[6:].split(' ')[0]
    parts = urlsplit(link)
    assert parts.scheme == 'https' and parts.netloc == 'anthive.example.org'
    assert parts.path == '/api/facets/braak/donor-plot.png'
    query = parse_qs(parts.query)
    assert query['db_id'] == ['L26/PSY'] and query['gene'] == ['PTPRG'] and 'include_uncounted' not in query
    assert 'D01 | 0 | 7.1 | 12' in text


# ── donor variability: one dataset ────────────────────────────────────────────

DV_SUMMARY = {
    'db_id': 'G24/MMG', 'status': 'analysed', 'reason': None, 'facet_scope': 'human_postmortem',
    'replicate_unit': 'donor', 'replicate_column': 'ant_donor',
    'run': {'tool': 'ant-donor-variability/1.0', 'timestamp_utc': 't', 'seed': 1, 'counts_layer': 'UMIs', 'n_replicates_all': 89},
    'units': [{'cell_type': 'microglial cell', 'celltype_group': 'microglia', 'status': 'ok', 'n_replicates': 87,
               'median_cells_per_replicate': 364.0, 'rung_deepest': 'D4', 'n_genes': 12064, 'n_modules': 366,
               'median_reliability': 0.68, 'counted_in_cross_cohort': True, 'terms_omitted': {'disease': 'known for 69%'},
               'rungs': [{'name': 'D0', 'computed': True}, {'name': 'D1', 'computed': True}, {'name': 'D2', 'computed': False}]}],
}


def _dv_rows(params):
    base = dict(DV_SUMMARY, unit={'cell_type': 'microglial cell', 'n_replicates': 87}, rung=params.get('rung'),
                n_matching=1, n_returned=1)
    if params.get('feature_type') is None:
        return DV_SUMMARY
    if params.get('feature') == 'ABSENT':
        return dict(base, n_matching=0, n_returned=0, rows=[])
    if params['feature_type'] == 'module':
        row = {'module': 'S/E/m', 'method': 'pseudobulk', 'source': 'gene_list', 'reliability': 0.97, 'sd_donor': 0.64,
               'fold_range_donor': 2.4, 'rungs': {'D1': {'sd': 0.43, 'share_design': 0.55}}}
    else:
        row = {'gene': 'ARL17B', 'reliability': 0.98, 'sd_donor': 1.6, 'fold_range_donor': 9.2,
               'rungs': {'D1': {'sd': 1.67, 'share_design': 0.0, 'excess_z': 4.1}}}
    return dict(base, rows=[row])


def test_donor_variability_for_one_dataset():
    namespace, calls = _load({'/databases/G24/MMG/donor-variability': _dv_rows})
    text = _run(namespace['donor_variability'](db_id='G24/MMG', n=3))
    assert 'Donor variability in G24/MMG' in text and '87 replicates' in text and 'rungs D0, D1 (deepest D4)' in text
    assert 'left out of the design: disease: known for 69%' in text
    assert 'ARL17B | reliability 0.98' in text and 'excess z 4.1' in text
    assert calls[-1][1]['feature_type'] == 'gene' and calls[-1][1]['sort'] == 'excess' and calls[-1][1]['limit'] == 3
    text = _run(namespace['donor_variability'](db_id='G24/MMG', feature_type='module'))
    assert 'S/E/m (pseudobulk/gene_list)' in text and calls[-1][1]['sort'] == 'sd'
    text = _run(namespace['donor_variability'](db_id='G24/MMG', gene='ABSENT'))
    assert 'ABSENT is not among the analysed genes' in text and calls[-1][1]['feature'] == 'ABSENT'


# ── gene set ranks ────────────────────────────────────────────────────────────

GENESET = {'geneset_id': 'S/E/m', 'name': 'm', 'format': 'grp', 'n_genes': 3, 'genes': ['A', 'B', 'C'],
           'column_name': 'S::E::m', 'scored_in': ['G24/MMG']}
RANK = {'geneset_id': 'S/E/m', 'min_cells': 20, 'min_peers': 10, 'not_available': [],
        'datasets': [{'db_id': 'G24/MMG',
                      'geneset': {'available': True, 'rank': 0.83, 'n_modules_compared': 159, 'n_cells': 40000,
                                  'mean': 0.33, 'std': 0.08,
                                  'cell_types': [{'cell_type': 'microglia', 'rank': 0.83, 'n_modules_compared': 159,
                                                  'n_cells': 37264}]},
                      'ucell': {'available': False, 'reason': 'too few peers', 'rank': None}}]}


def test_get_geneset_adds_the_rank_in_one_dataset():
    namespace, calls = _load({'/genesets/S/E/m': GENESET, '/genesets/S/E/m/rank': RANK})
    text = _run(namespace['get_geneset']('S/E/m'))
    assert 'Rank among gene sets' not in text and len(calls) == 1
    text = _run(namespace['get_geneset']('S/E/m', db_id='G24/MMG'))
    assert calls[-1] == ('/genesets/S/E/m/rank', {'db_id': 'G24/MMG'})
    assert 'a higher mean score than 83% of 159 other gene sets' in text
    assert 'UCell, secondary: not available (too few peers)' in text and 'within this dataset only' in text
    assert 'scores sidecar' in text


def test_list_module_scores_lists_sidecar_scores_with_their_query():
    ranks = {'available': True, 'modules': [
        {'geneset_id': 'S/E/low', 'column_name': 'S::E::low', 'available': True, 'rank': 0.1, 'n_modules_compared': 2, 'std': 0.1},
        {'geneset_id': 'S/E/high', 'column_name': 'S::E::high', 'available': True, 'rank': 0.9, 'n_modules_compared': 2, 'std': 0.2}]}
    stats = {'n': 3, 'min': 0.0, 'max': 1.0, 'mean': 0.5, 'std': 0.1, 'median': 0.5}
    listing = {'count': 3, 'count_by_storage': {'scores_sidecar': 2, 'duckdb': 1}, 'scored_genesets': [
        {'geneset_id': 'L/E/old', 'column_name': 'L::E::old', 'storage': 'duckdb', 'geneset_known': True, 'stats': stats,
         'query_sql': 'SELECT cell_name, "L::E::old" FROM obsnum'},
        {'geneset_id': 'S/E/high', 'column_name': 'S::E::high', 'storage': 'scores_sidecar', 'geneset_known': True,
         'stats': stats, 'ucell_column_name': 'ucell__S/E__high',
         'query_sql': "SELECT cell_name, score AS \"S::E::high\" FROM score_sidecar WHERE source = 'geneset' "
                      "AND catalog = 'S/E' AND module = 'high'"},
        {'geneset_id': 'S/E/low', 'column_name': 'S::E::low', 'storage': 'scores_sidecar', 'geneset_known': False,
         'stats': stats, 'query_sql': 'x'}]}
    namespace, calls = _load({'/databases/X/Y/module_scores': listing, '/databases/X/Y/module-ranks': ranks})
    text = _run(namespace['list_module_scores']('X/Y'))
    assert len(calls) == 1
    assert '3 pre-computed module score(s) (1 as obsnum columns, 2 in the scores sidecar)' in text
    assert "FROM score_sidecar WHERE source = 'geneset' AND catalog = 'S/E' AND module = 'high'" in text
    assert 'FROM obsnum' in text and "UCell score also stored: 'ucell__S/E__high'" in text
    assert 'SELECT ... FROM obsnum does not see them' in text
    text = _run(namespace['list_module_scores']('X/Y', with_ranks=True))
    assert text.index('S/E/high') < text.index('S/E/low')


def test_list_module_scores_empty_dataset():
    namespace, _ = _load({'/databases/X/Y/module_scores': {'count': 0, 'scored_genesets': []}})
    assert 'no pre-computed module scores' in _run(namespace['list_module_scores']('X/Y'))


# ── stored DE (author or custom) ──────────────────────────────────────────────

def test_de_method_level_matches_the_web_ui_rule():
    namespace, _ = _load({})
    level = namespace['de_method_level']
    assert level('deseq2_pseudobulk') == 'donor or sample (pseudobulk)' and level('edgeR') == 'donor or sample (pseudobulk)'
    assert level('glmm') == 'cell' and level('wilcoxon') == 'cell' and level('lmem') == 'cell'
    assert level('something') == '' and level(None) == ''
    assert 'DESCRIPTIVE' in namespace['de_level_note']('glmm') and 'pseudoreplicated' in namespace['de_level_note']('glmm')
    browser = (REPO_ROOT / 'web' / 'browser.py').read_text()
    assert "text in ('glmm', 'lmem', 'wilcoxon', 'mast', 't-test', 'ttest', 'logreg', 'seurat', 'scanpy')" in browser
    assert "text in ('glmm', 'lmem', 'wilcoxon', 'mast', 't-test', 'ttest', 'logreg', 'seurat', 'scanpy')" in MCP_SOURCE


STUDIES = {'studies': [
    {'study_id': 'cells', 'method': 'glmm', 'target': 'gene', 'reference_type': 'vsgrandmean', 'n_terms': 2, 'n_contrasts': 3},
    {'study_id': 'pb', 'method': 'deseq2_pseudobulk', 'target': 'gene', 'reference_type': 'vsreference', 'n_terms': 1,
     'n_contrasts': 1, 'covariates': ['sex']}]}
SUMMARY = {'study_id': 'cells', 'name': 'cells', 'method': 'glmm', 'target': 'gene', 'reference_type': 'vsgrandmean',
           'covariates': None, 'subset_filter': {'celltype': ['MG']}, 'cluster_obs_column': None, 'padj_max': 0.05,
           'contrasts': [{'term': 'Strain', 'contrast': 'NLGF - HuHu', 'n_genes': 100, 'n_sig': 40, 'n_up': 10,
                          'n_down': 30, 'efftype': 'log2fc'}]}
ROWS = {'rows': [{'gene_name': 'CD9', 'log2fc': 1.95, 'effsize': 1.95, 'efftype': 'log2fc', 'estimate': 1.35,
                  'ci_low': 1.1, 'ci_high': 1.6, 'padj': 1e-24, 'pvalue': 1e-28}], 'n_rows': 1}


def _de_responses():
    return {
        '/databases': {'databases': [{'id': 'S25/V6A'}, {'id': 'SF9/CMR'}, {'id': 'NO/DE'}]},
        '/databases/S25/V6A/de': {'studies': STUDIES['studies'][:1]},
        '/databases/SF9/CMR/de': {'studies': STUDIES['studies'][1:]},
        '/databases/NO/DE/de': {'studies': []},
        '/databases/S25/V6A/de/cells/summary': SUMMARY,
        '/databases/S25/V6A/de/cells/terms/Strain/contrasts/NLGF%20-%20HuHu': ROWS,
        '/databases/S25/V6A/de/by-gene/Apoe': {'rows': [
            {'study_id': 'cells', 'method': 'glmm', 'term': 'Strain', 'contrast': 'NLGF - HuHu', 'log2fc': 1.0,
             'effsize': 1.0, 'efftype': 'log2fc', 'padj': 0.01},
            {'study_id': 'cells', 'method': 'glmm', 'term': 'batch_group', 'contrast': 'b1', 'log2fc': 0.1, 'padj': 0.5},
            {'study_id': 'cells', 'method': 'glmm', 'term': 'intercept', 'contrast': 'x', 'log2fc': 5, 'padj': 0}]},
        '/databases/SF9/CMR/de/by-gene/Apoe': {'rows': [
            {'study_id': 'pb', 'method': 'deseq2_pseudobulk', 'term': 'genotype', 'contrast': 'K_vs_W', 'log2fc': 2.87,
             'effsize': 2.87, 'efftype': 'log2fc', 'padj': 6.8e-12}]},
    }


def test_list_de_studies_scans_every_dataset_once_and_labels_the_level():
    namespace, calls = _load(_de_responses())
    text = _run(namespace['list_de_studies']())
    assert text.startswith('AUTHOR OR CUSTOM DE') and '2 DE studies in 2 dataset(s)' in text
    assert 'S25/V6A | cells | gene | method glmm [cell level, DESCRIPTIVE]' in text
    assert 'SF9/CMR | pb | gene | method deseq2_pseudobulk [pseudobulk per donor or sample]' in text and 'covariates sex' in text
    n_calls = len(calls)
    _run(namespace['list_de_studies']())
    assert len(calls) == n_calls, 'the scan is cached'
    text = _run(namespace['list_de_studies']('NO/DE'))
    assert text == 'NO/DE: no stored DE studies.'


def test_de_study_summary_and_one_contrast():
    namespace, calls = _load(_de_responses())
    text = _run(namespace['de_study']('S25/V6A', 'cells'))
    assert 'cell level, DESCRIPTIVE' in text and 'Strain | NLGF - HuHu | 100 | 40 (10 up, 30 down) | log2fc' in text
    assert '{"celltype": ["MG"]}' in text and calls[-1][1] == {'padj_max': 0.05}
    text = _run(namespace['de_study']('S25/V6A', 'cells', term='Strain', contrast='NLGF - HuHu', sort='effsize', n=5))
    assert calls[-1][0] == '/databases/S25/V6A/de/cells/terms/Strain/contrasts/NLGF%20-%20HuHu'
    assert calls[-1][1] == {'sort': 'effsize', 'direction': 'both', 'limit': 5, 'padj_max': 0.05}
    assert 'CD9 | 1.95 | 1.95 (log2fc) | 1.35 [1.1, 1.6] | 1.00e-24' in text
    _run(namespace['de_study']('S25/V6A', 'cells', term='Strain', contrast='NLGF - HuHu'))
    assert calls[-1][1]['padj_max'] is None


def test_gene_de_marks_cell_level_rows_and_hides_technical_terms():
    namespace, _ = _load(_de_responses())
    text = _run(namespace['gene_de']('Apoe'))
    assert 'S25/V6A | cells (glmm) | cell, DESCRIPTIVE | Strain | NLGF - HuHu' in text
    assert 'SF9/CMR | pb (deseq2_pseudobulk) | donor or sample (pseudobulk) | genotype | K_vs_W | 2.87' in text
    assert 'intercept' not in text and 'batch_group' not in text and '1 rows of technical terms' in text
    text = _run(namespace['gene_de']('Apoe', include_technical_terms=True, padj_max=0.05))
    assert 'batch_group' not in text, 'padj_max still applies'
    assert 'Strain' in text


# ── texts the model reads ─────────────────────────────────────────────────────

def _tool_docs() -> Dict[str, str]:
    docs = {}
    for node in ast.walk(MCP_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == 'tool':
                    docs[node.name] = ast.get_docstring(node) or ''
    return docs


def test_new_tools_are_registered_and_point_at_explain():
    docs = _tool_docs()
    for name in ('facet_overview', 'list_de_studies', 'de_study', 'gene_de'):
        assert name in docs, name
    for name in ('list_de_studies', 'de_study', 'gene_de'):
        doc = ' '.join(docs[name].split())
        assert 'AUTHOR OR CUSTOM DE' in doc or 'author or custom DE' in doc, name
        assert "explain('differential-expression')" in doc and "explain('per-cell-per-donor')" in doc, name
        assert 'DESCRIPTIVE' in doc and 'pseudoreplicated' in doc, name
    for name in ('facet_effects', 'facet_top_genes', 'facet_overview', 'module_facet_effects', 'facet_top_modules',
                 'get_facet_tests', 'facet_donor_values', 'get_geneset', 'list_module_scores', 'plot', 'facet_units'):
        assert 'explain(' in docs[name], name
    for name in ('facet_overview', 'list_de_studies', 'de_study', 'gene_de', 'facet_donor_values', 'donor_variability',
                 'get_geneset', 'list_module_scores', 'get_facet_tests'):
        assert '—' not in docs[name] and 'honest' not in docs[name].lower(), name


def test_instructions_and_guide_cover_the_new_tools_and_the_numbers():
    instructions = MCP_SOURCE[MCP_SOURCE.index("instructions=("):MCP_SOURCE.index("This MCP server is reachable at")]
    for needed in ('facet_overview', 'list_de_studies', 'de_study', 'gene_de', 'with_ranks', 'explain("fdr")',
                   'explain("r-value")', 'explain("per-cell-per-donor")', 'explain("differential-expression")'):
        assert needed in instructions, needed
    assert '—' not in instructions
    namespace, _ = _load({})
    guide = namespace['ANTHIVE_GUIDE']
    for needed in ('facet_overview', 'gene_de', 'r-value', 'q value', 'padj', 'per-cell-per-donor', 'rank-among-gene-sets'):
        assert needed in guide, needed
    assert '—' not in guide


def test_help_page_lists_the_new_mcp_tools():
    help_html = (REPO_ROOT / 'web' / 'templates' / 'help.html').read_text()
    section = help_html[help_html.index('id="mcp-offers"'):help_html.index('</table>', help_html.index('id="mcp-offers"'))]
    for name in ('facet_overview', 'facet_effects', 'module_facet_effects', 'facet_donor_values', 'donor_variability',
                 'list_de_studies', 'de_study', 'gene_de', 'donor_groups'):
        assert f'<code>{name}</code>' in section, name
    registered = set(_tool_docs())
    for first_cell in re.findall(r'<tr><td>(.*?)</td>', section):
        for name in re.findall(r'<code>([a-z_]+)</code>', first_cell):
            assert name in registered, name


@pytest.mark.parametrize('topic', ['facet-overview', 'facet-replication', 'r-value', 'k-of-m', 'fdr', 'per-cell-per-donor',
                                   'differential-expression', 'rank-among-gene-sets', 'module-replication', 'donor-plots',
                                   'donor-variability', 'facet-replicate-unit', 'facet-tested', 'module-scores',
                                   'facet-effects', 'facet-scopes'])
def test_every_explain_target_named_in_the_mcp_exists(topic):
    import sys  # noqa: PLC0415
    sys.path.insert(0, str(REPO_ROOT / 'lib'))
    import help_content  # noqa: PLC0415
    assert help_content.get_topic(topic) is not None or help_content.get_term(topic) is not None, topic


def test_every_explain_call_in_the_mcp_is_tested():
    named = set(re.findall(r"""explain\(['"]([a-z0-9-]+)['"]\)""", MCP_SOURCE))
    tested = set(test_every_explain_target_named_in_the_mcp_exists.pytestmark[0].args[1])
    assert named <= tested, named - tested


# ── 2.46.1: a 503 warming_up becomes a plain message (and one short retry) ──

def _warmup_helpers() -> Dict[str, Any]:
    names = ('WARMING_UP_MESSAGE', 'WARMING_UP_RETRY_MAX_SECONDS', 'ServerWarmingUp', 'api_request')
    nodes = [node for node in ast.parse(MCP_SOURCE).body
             if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names)
             or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets))]
    sleeps: List[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    namespace: Dict[str, Any] = {'asyncio': type('A', (), {'sleep': staticmethod(fake_sleep)})}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MCP_PATH), 'exec'), namespace)
    namespace['sleeps'] = sleeps
    return namespace


class _FakeResponse:
    def __init__(self, status: int, body: Any):
        self.status_code, self._body, self.headers = status, body, {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise StatusError(self.status_code, 'error')


class _FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    async def request(self, method, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_mcp_turns_warming_up_into_a_plain_message():
    helpers = _warmup_helpers()
    warming = {'status': 'warming_up', 'retry_after': 30}
    client = _FakeClient([_FakeResponse(503, warming)])
    with pytest.raises(helpers['ServerWarmingUp']) as excinfo:
        _run(helpers['api_request'](client, 'GET', 'http://api/genes/TREM2/facet-effects'))
    assert 'warming up' in str(excinfo.value) and 'try again in a minute' in str(excinfo.value)
    assert client.calls == 1 and helpers['sleeps'] == []                 # 30 s is too long to wait in a tool call
    short = {'status': 'warming_up', 'retry_after': 2}
    client = _FakeClient([_FakeResponse(503, short), _FakeResponse(200, {'ok': True})])
    response = _run(helpers['api_request'](client, 'GET', 'http://api/x'))
    assert response.json() == {'ok': True} and helpers['sleeps'] == [2.0]
    client = _FakeClient([_FakeResponse(503, {'detail': 'Geneset store is not configured'})])
    with pytest.raises(StatusError):                                      # other 503s stay HTTP errors
        _run(helpers['api_request'](client, 'GET', 'http://api/genesets'))
    assert 'await api_request(_http_client' in MCP_SOURCE and '_http_client.get(url' not in MCP_SOURCE


def test_facet_effects_reports_a_warming_facet_and_keeps_the_others():
    namespace, _ = _load({})
    card = {'facet': {'contrast': 'm vs f', 'effect_unit': 'log2FC'}, 'scope': 'human_postmortem',
            'scopes': ['human_postmortem'], 'units': []}

    async def fake_get(path: str, **params):
        if path == '/facets/sex/effects':
            raise namespace['ServerWarmingUp'](namespace['WARMING_UP_MESSAGE'])
        if path == '/facets/disease/effects':
            raise StatusError(500, 'boom')
        return card
    namespace['_get'] = fake_get
    text = _run(namespace['facet_effects']('TREM2'))
    assert 'TREM2 / sex: the server is still warming up' in text and 'try this facet again in a minute' in text
    assert 'disease: error 500' in text
    for name in ('graft_sex', 'host_sex', 'apoe4_carrier', 'braak', 'age', 'app_nlgf'):
        assert f'## TREM2 / {name} [human_postmortem]' in text, name


def test_deposit_tool_docstrings_name_every_state():
    import sys  # noqa: PLC0415
    sys.path.insert(0, str(REPO_ROOT / 'lib'))
    from dataset_deposit import RECORD_STATES  # noqa: PLC0415
    docs = _tool_docs()
    states = set(RECORD_STATES) | {'seen', 'stable', 'checking', 'importing', 'live', 'held', 'failed',
                                   'needs_decision', 'rejected', 'superseded'}
    for tool in ('my_deposits', 'deposit_status'):
        for state in states:
            assert re.search(rf'\b{state}\b', docs[tool]), (tool, state)


def test_instructions_mention_warm_up_and_internal_datasets():
    instructions = MCP_SOURCE[MCP_SOURCE.index("instructions=("):MCP_SOURCE.index("This MCP server is reachable at")]
    assert 'warming up, try again in a' in instructions and 'minute' in instructions
    assert 'access=internal' in instructions and 'signed-in' in instructions
