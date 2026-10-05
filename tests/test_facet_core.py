"""Lineage in the API (L3), recurring-gene ranking and cell type groups.

* ``lib/celltype_groups.yaml``: every label maps to a known group, prefixes,
  unmapped fallback.
* ``lib/facet_results.py``: replication counted per cell type group, one
  representative source per cohort and group, Bonferroni over sibling strata,
  secondary datasets listed but not counted, and the recurring-gene ranking
  (k, then agreement, then median effect; never "2 of 2" over "2 of 4" for
  having fewer tests).
* ``bin/api/ant-serve``: lineage fields, ``/donor-groups``,
  ``exclude_secondary`` on ``/module-variability`` and ``/gene_stats_all``
  (endpoints extracted with ``ast`` and run on a fake registry, as in
  ``test_facet_results.py``).
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import pytest
import yaml

import celltype_groups
import facet_results as fr

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
MCP_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-mcp'


# ── cell type groups ─────────────────────────────────────────────────────────

def test_every_label_and_prefix_names_a_group():
    table = yaml.safe_load(celltype_groups.TABLE_PATH.read_text())
    groups = set(table['groups'])
    assert set(table['labels'].values()) <= groups
    assert set(table['prefixes'].values()) <= groups
    for name, entry in table['groups'].items():
        cl_id = (entry or {}).get('cl_id')
        assert cl_id is None or cl_id.startswith('CL:'), name


@pytest.mark.parametrize('label, group', [
    ('microglial cell', 'microglia'), ('Microglia', 'microglia'), ('MG', 'microglia'),
    ('Mic P2RY12', 'microglia'), ('Mic MKI67', 'microglia'), ('micro', 'microglia'),
    ('MICRO', 'microglia'), ('Z20/ALL/cluster_Micro', 'microglia'),
    ('PVM', 'perivascular macrophage'), ('CAMs', 'perivascular macrophage'),
    ('myeloid cell', 'myeloid (mixed)'), ('Phagocytes', 'myeloid (mixed)'),
    ('Olig_PLXDC2_SFRP1', 'oligodendrocyte'), ('OPC_ADM', 'oligodendrocyte precursor cell'),
    ('EN-L2-3-A', 'excitatory neuron'), ('IN-VIP', 'inhibitory neuron'),
    ('SOX6_AGTR1', 'dopaminergic neuron'), ('Endo_DCN_ABCC9', 'vascular (mixed)'),
    ('Endo_MET', 'endothelial cell'), ('MicroOligo', 'glia (mixed)'),
    ('vascular leptomeningeal cell', 'VLMC / fibroblast'), ('pericyte', 'mural cell'),
    ('all', 'unmapped'), ('LC5/VNF/cluster_Alz', 'unmapped'), ('something new', 'unmapped'),
    (None, 'unmapped'),
])
def test_group_of(label, group):
    assert celltype_groups.group_of(label) == group


def test_group_info():
    info = celltype_groups.group_info('microglia')
    assert info == {'group': 'microglia', 'cl_id': 'CL:0000129', 'cl_label': 'microglial cell', 'note': None}
    assert celltype_groups.group_info('vascular (mixed)')['cl_id'] is None


# ── facet results: groups, representatives, siblings, secondaries ────────────

GENES = ('G_A', 'G_B', 'G_C', 'G_D', 'G_E')


def _write_dataset(root: Path, db_id: str, strata: List[Tuple[str, str, Dict[str, Tuple[float, float]]]],
                   facet: str = 'braak') -> None:
    group, stem = db_id.split('/')
    folder = root / group
    folder.mkdir(parents=True, exist_ok=True)
    rows, units = [], []
    for cell_type, source, effects in strata:
        for gene in GENES:
            logfc, q_value = effects.get(gene, (0.01, 0.9))
            rows.append({'facet': facet, 'cell_type': cell_type, 'celltype_source': source, 'gene': gene,
                         'logFC': logfc, 'ci_low': logfc - 0.1, 'ci_high': logfc + 0.1,
                         'p_value': q_value / 2, 'q_value': q_value, 'n_donors': 20,
                         'descriptive_only': False, 'n_lt5': False})
        units.append({'facet': facet, 'cell_type': cell_type, 'celltype_source': source, 'status': 'ok',
                      'n_donors_analysed': 20, 'n_genes_tested': len(GENES), 'n_sig_q05': 1,
                      'level_counts': {'n_donors': 20}, 'covariates': ['sex'], 'covariates_omitted': []})
    pd.DataFrame(rows).to_parquet(folder / f'{stem}.facet_tests.parquet')
    (folder / f'{stem}.facet_run_manifest.json').write_text(json.dumps(
        {'tool': 'ant-facet-test/1.0', 'db_id': db_id, 'units': units, 'parameters': {}}))


@pytest.fixture
def cohorts(tmp_path: Path) -> Tuple[Path, List[Dict[str, Any]]]:
    """Seven cohorts with microglia under different labels (C6 secondary).

    G_A: significant up in C1 and C2; C3 to C5 up with larger effects (n.s.)
    G_B: significant up in C1 and C2; down (n.s.) in C3, C4, C5
    G_C: significant up in C6 (secondary) and C7
    G_D: down in C1's author substate only (the C1 constant is n.s.)
    G_E: q = 0.02 in each of C5's three sibling strata
    Genes not listed in a stratum get logFC 0.01, q 0.9.
    """
    root = tmp_path / '_facet_tests'
    up_sig, up_ns_big, down_ns = (0.3, 0.001), (0.9, 0.5), (-0.2, 0.6)
    # C1: constant microglia plus author substates (the constant is the representative)
    _write_dataset(root, 'C1/ds', [
        ('microglial cell', 'constant_all_cells', {'G_A': up_sig, 'G_B': up_sig, 'G_D': (0.2, 0.9)}),
        ('Mic P2RY12', 'cell_type_high_resolution', {'G_D': (-1.0, 0.0001)}),
    ])
    # C2: per-cell author label only
    _write_dataset(root, 'C2/ds', [('Microglia', 'cell_type', {'G_A': up_sig, 'G_B': up_sig})])
    _write_dataset(root, 'C3/ds', [('MG', 'class', {'G_A': up_ns_big, 'G_B': down_ns})])
    _write_dataset(root, 'C4/ds', [('micro', 'celltype', {'G_A': up_ns_big, 'G_B': down_ns})])
    # C5: three sibling author strata in one group, each q = 0.02 (Bonferroni x3 = 0.06)
    _write_dataset(root, 'C5/ds', [
        ('Mic TPT1', 'cell_type_high_resolution', {'G_A': up_ns_big, 'G_B': down_ns, 'G_E': (0.5, 0.02)}),
        ('Mic MKI67', 'cell_type_high_resolution', {'G_A': up_ns_big, 'G_B': down_ns, 'G_E': (0.5, 0.02)}),
        ('Mic DUSP1', 'cell_type_high_resolution', {'G_A': up_ns_big, 'G_B': down_ns, 'G_E': (0.5, 0.02)}),
    ])
    # C6: a secondary dataset, significant for G_C; must not count
    _write_dataset(root, 'C6/ds', [('microglial cell', 'ant_cell_type', {'G_C': up_sig})])
    _write_dataset(root, 'C7/ds', [('microglial cell', 'ant_cell_type', {'G_C': up_sig})])
    table = ['status\tcohort\tcell_class\trole\tdb_id\trule_from_audit_8_3']
    for index in range(1, 8):
        table.append(f'DRAFT\tCohort{index}\tmicroglia\tanchor\tC{index}/ds\tanchor')
    (root / fr.COHORT_TABLE_NAME).write_text('\n'.join(table) + '\n')
    datasets = [{'db_id': f'C{i}/ds', 'group_folder': f'C{i}', 'stem': 'ds', 'title': f'C{i}',
                 'is_secondary': i == 6} for i in range(1, 8)]
    return root, datasets


def test_replication_is_counted_per_group(cohorts):
    root, datasets = cohorts
    card = fr.FacetResults(root).gene_effects('braak', 'G_A', datasets)
    assert [r['celltype_group'] for r in card['replication']] == ['microglia']
    rep = card['replication'][0]
    assert rep['labels'] == ['MG', 'Mic DUSP1', 'Mic MKI67', 'Mic P2RY12', 'Mic TPT1', 'Microglia',
                             'micro', 'microglial cell']
    assert (rep['cohorts_sig_same_sign'], rep['cohorts_tested']) == (2, 6)
    raw_labels = {u['cell_type'] for u in card['units']}
    assert 'Mic P2RY12' in raw_labels and all(u['celltype_group'] == 'microglia' for u in card['units'])


def test_representative_source_wins_within_a_cohort(cohorts):
    root, datasets = cohorts
    card = fr.FacetResults(root).gene_effects('braak', 'G_D', datasets)
    # C1's substate is strongly down, but the constant (all microglia) stands for C1
    assert card['replication'][0]['cohorts_sig_same_sign'] == 0
    substate = next(u for u in card['units'] if u['cell_type'] == 'Mic P2RY12')
    assert substate['counted'] is False and 'more direct cell type source' in substate['not_counted_reason']


def test_sibling_strata_are_bonferroni_adjusted(cohorts):
    root, datasets = cohorts
    card = fr.FacetResults(root).gene_effects('braak', 'G_E', datasets)
    assert card['replication'][0]['cohorts_sig_same_sign'] == 0
    sibling = next(u for u in card['units'] if u['cell_type'] == 'Mic TPT1')
    assert sibling['n_sibling_strata'] == 3 and sibling['q_counted'] == pytest.approx(0.06)
    assert sibling['significant'] is True  # the raw q is still shown as is


def test_secondary_datasets_are_listed_but_not_counted(cohorts):
    root, datasets = cohorts
    card = fr.FacetResults(root).gene_effects('braak', 'G_C', datasets)
    rep = card['replication'][0]
    assert rep['cohorts_sig_same_sign'] == 1 and 'Cohort6' not in rep['cohorts_up']
    secondary = next(u for u in card['units'] if u['db_id'] == 'C6/ds')
    assert secondary['is_secondary'] and not secondary['counted']
    assert secondary['not_counted_reason'] == 'secondary dataset (reanalysis)'
    everything = fr.FacetResults(root).gene_effects('braak', 'G_C', datasets, exclude_secondary=False)
    assert everything['replication'][0]['cohorts_sig_same_sign'] == 2
    overview = fr.FacetResults(root).facet_overview('braak', datasets)
    row = next(r for r in overview['rows'] if r['db_id'] == 'C6/ds')
    assert row['is_secondary'] and row['counted'] is False and row['celltype_group'] == 'microglia'


def test_cell_type_filter_accepts_group_or_label(cohorts):
    root, datasets = cohorts
    by_group = fr.FacetResults(root).gene_effects('braak', 'G_A', datasets, cell_type='microglia')
    by_label = fr.FacetResults(root).gene_effects('braak', 'G_A', datasets, cell_type='MG')
    assert by_group['n_units'] > 1 and by_label['n_units'] == 1
    top = fr.FacetResults(root).recurring_genes('braak', datasets, cell_type='Mic P2RY12')
    assert top['celltype_group'] == 'microglia'


def test_recurring_gene_ranking(cohorts):
    root, datasets = cohorts
    out = fr.FacetResults(root).recurring_genes('braak', datasets)
    assert out['celltype_group'] == 'microglia' and out['cl_id'] == 'CL:0000129'
    rows = {r['gene']: r for r in out['rows']}
    # G_A: 2 significant of 6 tested, all six up -> agreement 1.0
    # G_B: 2 significant of 6 tested; C3, C4, C5 go down, C7 slightly up -> agreement 3/6
    assert (rows['G_A']['cohorts_sig_same_sign'], rows['G_A']['cohorts_tested']) == (2, 6)
    assert rows['G_A']['agreement'] == pytest.approx(1.0)
    assert rows['G_B']['agreement'] == pytest.approx(3 / 6)
    order = [r['gene'] for r in out['rows']]
    assert order.index('G_A') < order.index('G_B')
    assert 'never ranks a gene by itself' in out['ranking']


def test_two_of_two_does_not_outrank_two_of_four_for_fewer_tests():
    """Synthetic table straight into the ranking: X is 2 of 2, Y is 2 of 4
    with all four cohorts up and larger effects. The old rule (cohorts
    tested, fewer first) put X first; now Y wins on effect size."""
    reader = fr.FacetResults(Path('/nonexistent'))
    units = pd.DataFrame({
        'unit': [0, 1, 2, 3], 'db_id': ['a', 'b', 'c', 'd'], 'title': list('abcd'),
        'cell_type': ['microglial cell'] * 4, 'celltype_source': ['ant_cell_type'] * 4,
        'n_donors': [20] * 4, 'n_donors_ref': [None] * 4, 'n_donors_test': [None] * 4,
        'level_ref': [None] * 4, 'level_test': [None] * 4, 'descriptive_only': [False] * 4,
        'cohort': ['A', 'B', 'C', 'D'], 'cohort_label': ['A', 'B', 'C', 'D'], 'role': ['anchor'] * 4,
        'counted': [True] * 4, 'is_secondary': [False] * 4,
        'celltype_group': ['microglia'] * 4, 'summary_unit': [{}] * 4})
    rows = pd.DataFrame({
        'unit': [0, 1, 0, 1, 2, 3],
        'gene': ['X', 'X', 'Y', 'Y', 'Y', 'Y'],
        'logFC': [0.2, 0.2, 0.8, 0.8, 0.7, 0.6],
        'ci_low': [0.0] * 6, 'ci_high': [1.0] * 6,
        'q_value': [0.01, 0.01, 0.01, 0.01, 0.4, 0.5]})
    rows['gene_upper'] = rows['gene']
    reader.facet_gene_table = lambda facet, datasets: (rows, units)  # type: ignore[assignment]
    out = reader.recurring_genes('braak', [])
    assert [r['gene'] for r in out['rows']] == ['Y', 'X']
    y = out['rows'][0]
    assert (y['cohorts_sig_same_sign'], y['cohorts_tested'], y['cohorts_same_sign']) == (2, 4, 4)
    assert y['median_abs_logFC'] == pytest.approx(0.75)


# ── ant-serve: lineage, donor groups, exclude_secondary ──────────────────────

_SERVE_WANTED = {'_lineage_fields', '_secondary_db_ids', 'list_donor_groups', 'get_module_variability',
                 '_alias_canonical', 'get_gene_stats_all', '_dataset_donor_keys'}


class _Registry:
    def __init__(self):
        self.metadata_cache = {
            'G1/PRI': {'title': 'Primary', 'is_secondary': False, 'designation': 'primary', 'reanalysis_of': [],
                       'donor_group': 'pool1', 'has_donor_uid': True, 'n_cells': 100, 'default_layer': 'X',
                       'sha256': 'a', 'collection': {'group': 'G1'}},
            'G1/REA': {'title': 'Reanalysis', 'is_secondary': True, 'designation': 'reanalysis',
                       'reanalysis_of': ['G1/PRI'], 'donor_group': 'pool1', 'has_donor_uid': False,
                       'n_cells': 100, 'default_layer': 'X', 'sha256': 'b', 'collection': {'group': 'G1'}},
            'G2/ONE': {'title': 'Lonely', 'is_secondary': False, 'designation': 'unresolved',
                       'reanalysis_of': [], 'donor_group': None, 'n_cells': 50, 'default_layer': 'X',
                       'sha256': 'c', 'collection': {'group': 'G2'}},
        }
        self.databases = {db_id: f'/tmp/{db_id}.duckdb' for db_id in self.metadata_cache}
        self._db_extra = {db_id: {'group_id': db_id.split('/')[0]} for db_id in self.metadata_cache}

        class _Index:
            n_rows = 3

            @staticmethod
            def lookup(db_id, gene, case_sensitive):
                return {'gene': gene, 'mean': {'G1/PRI': 1.0, 'G1/REA': 5.0, 'G2/ONE': 2.0}[db_id]}

            @staticmethod
            def is_loaded():
                return True

        class _Cache:
            pass

        self.gene_stats_index = _Index()
        self.gene_stats_cache = _Cache()

    def list_databases(self):
        return [{'id': db_id, **meta} for db_id, meta in self.metadata_cache.items()]

    def get_database_path(self, db_id):
        return self.databases[db_id]


def _donor_duckdb(path: Path, rows: List[Tuple[str, Optional[str]]], with_uid: bool) -> None:
    """An obscat view with ant_donor (and ant_donor_uid) per cell; fake ids only."""
    import duckdb
    conn = duckdb.connect(str(path))
    columns = 'cell_name VARCHAR, ant_donor VARCHAR' + (', ant_donor_uid VARCHAR' if with_uid else '')
    conn.execute(f'CREATE TABLE t ({columns})')
    for index, (donor, uid) in enumerate(rows):
        values = [f'c{index}', donor] + ([uid] if with_uid else [])
        conn.execute(f'INSERT INTO t VALUES ({", ".join("?" for _ in values)})', values)
    conn.execute('CREATE VIEW obscat AS SELECT * FROM t')
    conn.close()


@pytest.fixture
def serve(tmp_path):
    from fastapi import FastAPI, HTTPException, Query, Response
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if name in _SERVE_WANTED:
            body.append(node)
    app = FastAPI()
    variability = pd.DataFrame({
        'dataset': ['G1/PRI', 'G1/REA', 'G2/ONE'], 'source': ['geneset'] * 3, 'catalog': ['c'] * 3,
        'module': ['m'] * 3, 'tag_column': ['ant_sex'] * 3, 'tag': ['sex'] * 3,
        'statistic': ['eta2'] * 3, 'value': [0.1, 0.9, 0.2], 'n_cells': [1] * 3, 'n_categories': [2] * 3})

    class _GeneStatsCache:
        @staticmethod
        def fingerprint(*args):
            return None

    registry = _Registry()
    # G1/PRI publishes a uid for D1 only; G1/REA (a reanalysis) has labels only.
    _donor_duckdb(tmp_path / 'pri.duckdb', [('D1', 'fakecoh:0001'), ('D1', 'fakecoh:0001'), ('D3', None)], True)
    _donor_duckdb(tmp_path / 'rea.duckdb', [('D1', None), ('D3', None), (' D3 ', None)], False)
    registry.databases = {'G1/PRI': str(tmp_path / 'pri.duckdb'), 'G1/REA': str(tmp_path / 'rea.duckdb'),
                          'G2/ONE': str(tmp_path / 'missing.duckdb')}

    import duckdb
    from contextlib import contextmanager
    from threading import Lock

    class _Db:
        def __init__(self, conn):
            self._conn = conn

    @contextmanager
    def _pooled_db(db_id):
        conn = duckdb.connect(registry.get_database_path(db_id), read_only=True)
        try:
            yield _Db(conn)
        finally:
            conn.close()

    namespace: Dict[str, Any] = {
        '_pooled_db': _pooled_db, '_DONOR_KEYS_CACHE': {}, '_DONOR_KEYS_LOCK': Lock(),
        'app': app, 'registry': registry, 'HTTPException': HTTPException, 'Query': Query,
        'Response': Response, 'JSONResponse': JSONResponse, 'log': logging.getLogger('test-core'),
        'Dict': Dict, 'List': List, 'Any': Any, 'Optional': Optional, 'Tuple': Tuple, 'Set': Set,
        'Path': Path, 'asyncio': asyncio, '_load_module_variability': lambda: variability,
        'BACKCOMPAT_LEGACY_NAMES': {}, 'GeneStatsCache': _GeneStatsCache,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return TestClient(app), namespace


def test_lineage_fields(serve):
    _, namespace = serve
    lineage = namespace['_lineage_fields']
    assert lineage({'is_secondary': True, 'reanalysis_of': 'V25/SAL', 'donor_group': 'voet2025'},
                   ['ant_donor_uid']) == {
        'designation': 'reanalysis', 'reanalysis_of': ['V25/SAL'], 'donor_group': 'voet2025',
        'has_donor_uid': True}
    assert lineage({'is_secondary': False}, [])['designation'] == 'primary'
    assert lineage({}, [])['designation'] == 'unresolved'
    assert namespace['_secondary_db_ids']() == {'G1/REA'}


def test_donor_groups_endpoint(serve):
    client, _ = serve
    data = client.get('/donor-groups').json()
    assert data['n_groups'] == 1 and data['n_ungrouped'] == 1
    group = data['groups'][0]
    assert group['donor_group'] == 'pool1' and group['n_datasets'] == 2
    assert (group['n_primary'], group['n_reanalysis'], group['n_unresolved']) == (1, 1, 0)
    assert group['n_cells'] == 100  # primaries only
    assert [d['db_id'] for d in group['datasets']] == ['G1/PRI', 'G1/REA']
    assert group['datasets'][1]['reanalysis_of'] == ['G1/PRI']
    assert group['has_donor_uid'] is False
    assert client.get('/donor-groups', params={'donor_group': 'nope'}).status_code == 404


def test_donor_groups_distinct_donor_counts(serve):
    client, _ = serve
    group = client.get('/donor-groups').json()['groups'][0]
    donors = group['donors']
    # PRI: fakecoh:0001 (uid), G1:D3 (fallback); REA: G1:D1, G1:D3 (fallback, ' D3 ' trimmed).
    # D1 counts twice: the reanalysis has no uid, which the fallback cannot see through.
    assert (donors['n_donors'], donors['n_donors_uid'], donors['n_donors_fallback']) == (3, 1, 2)
    assert donors['n_donors_primary'] == 2
    assert (donors['n_datasets_counted'], donors['n_datasets_without_donor']) == (2, 0)
    assert 'ant_donor_uid' in donors['basis']
    by_id = {d['db_id']: d for d in group['datasets']}
    assert (by_id['G1/PRI']['n_donors'], by_id['G1/PRI']['n_donors_uid']) == (2, 1)
    assert (by_id['G1/REA']['n_donors'], by_id['G1/REA']['n_donors_uid']) == (2, 0)
    plain = client.get('/donor-groups', params={'donor_counts': 'false'}).json()['groups'][0]
    assert 'donors' not in plain and 'n_donors' not in plain['datasets'][0]


def test_module_variability_excludes_secondaries(serve):
    client, _ = serve
    data = client.get('/module-variability').json()
    assert {r['dataset'] for r in data['rows']} == {'G1/PRI', 'G2/ONE'}
    assert data['n_excluded_secondary'] == 1
    everything = client.get('/module-variability', params={'exclude_secondary': 'false'}).json()
    assert len(everything['rows']) == 3
    explicit = client.get('/module-variability', params={'dataset': 'G1/REA'}).json()
    assert [r['dataset'] for r in explicit['rows']] == ['G1/REA']


def test_gene_stats_all_excludes_secondaries(serve):
    client, _ = serve
    data = client.get('/gene_stats_all', params={'genes': 'APOE'}).json()
    assert {r['db_id'] for r in data['results']} == {'G1/PRI', 'G2/ONE'}
    assert data['n_excluded_secondary'] == 1 and data['exclude_secondary'] is True
    cv_primary = data['results'][0]['stats'][0]['cross_db_cv']
    everything = client.get('/gene_stats_all', params={'genes': 'APOE', 'exclude_secondary': 'false'}).json()
    assert len(everything['results']) == 3
    assert next(r for r in everything['results'] if r['db_id'] == 'G1/REA')['is_secondary'] is True
    assert everything['results'][0]['stats'][0]['cross_db_cv'] != cv_primary


def test_serve_registers_donor_groups_and_lineage():
    source = SERVE_PATH.read_text()
    assert '@app.get("/donor-groups"' in source
    assert '**_lineage_fields(file_metadata, obs_categorical)' in source
    for endpoint in ('def get_facet_tests_across_datasets', 'def get_facet_recurring_genes',
                     'def get_facet_gene_effects', 'def get_gene_facet_effects', 'async def get_facet_detail'):
        start = source.index(endpoint)
        assert 'exclude_secondary' in source[start:start + 1500], endpoint


def test_mcp_lineage_tools():
    tree = ast.parse(MCP_PATH.read_text())
    tools = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'tool':
                    tools[node.name] = node
    assert 'donor_groups' in tools
    for name in ('search_datasets', 'facet_effects', 'facet_top_genes', 'facet_units'):
        arguments = [a.arg for a in tools[name].args.args]
        assert 'exclude_secondary' in arguments, name
    assert 'donor_group' in ast.get_docstring(tools['search_datasets'])


def test_representatives_are_chosen_per_dataset_not_per_cohort():
    """One cohort, two datasets: ds1 has a constant (n.s.) plus a substate,
    ds2 only author substates (significant). ds2 still speaks for the cohort;
    only ds1's substate yields to ds1's constant."""
    units = pd.DataFrame({
        'unit': [0, 1, 2], 'db_id': ['ds1', 'ds1', 'ds2'],
        'cell_type': ['microglial cell', 'Mic P2RY12', 'Mic P2RY12'],
        'celltype_source': ['constant_all_cells', 'cell_type_high_resolution', 'cell_type_high_resolution'],
        'descriptive_only': [False] * 3, 'cohort': ['ROSMAP'] * 3, 'counted': [True] * 3,
        'is_secondary': [False] * 3})
    counted = fr.FacetResults.counting_units(units)
    assert counted['representative'].tolist() == [True, False, True]
    rows = counted.assign(q_value=[0.9, 0.001, 0.001], logFC=[0.1, 0.5, 0.5])
    rep = fr.FacetResults._replication(rows)
    assert (rep['cohorts_sig_same_sign'], rep['cohorts_tested']) == (1, 1)
