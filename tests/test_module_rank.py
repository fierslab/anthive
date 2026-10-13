"""Tests of lib/module_rank.py and the ``/genesets/{study}/{experiment}/{name}/rank`` route of ant-serve."""
import ast
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd
import pytest

import module_rank
import config  # noqa: F401  (ensures the lib shim is importable like the other tests)

SERVE_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-serve'
CATALOG = 'Study/Exp'


def means_frame(rows: List[Tuple]) -> pd.DataFrame:
    """Frame of stratum means; rows without a std get 1.0."""
    return pd.DataFrame([row if len(row) == 7 else tuple(row) + (1.0,) for row in rows], columns=module_rank.MEANS_COLUMNS)


def many_modules(n_modules: int, stratum: str = 'microglia', n_cells: int = 100, source: str = 'geneset') -> List[Tuple]:
    return [(source, 'Other/Cat', f'm{i}', stratum, n_cells, float(i)) for i in range(n_modules)]


def test_rank_is_fraction_of_peers_with_lower_mean():
    frame = means_frame(many_modules(20) + [('geneset', CATALOG, 'MINE', 'microglia', 100, 14.5)])
    result = module_rank.module_rank(frame, 'geneset', CATALOG, 'MINE')
    assert result['available'] and result['rank'] == pytest.approx(15 / 20)
    assert result['n_modules_compared'] == 20 and result['n_cells'] == 100 and result['mean'] == 14.5
    assert result['cell_types'] == [{'cell_type': 'microglia', 'n_cells': 100, 'mean': 14.5, 'std': 1.0, 'rank': 0.75, 'n_modules_compared': 20}]
    assert result['std'] == pytest.approx(1.0)


def test_rank_extremes_and_ties():
    base = many_modules(10)
    top = module_rank.module_rank(means_frame(base + [('geneset', CATALOG, 'T', 'microglia', 50, 99.0)]), 'geneset', CATALOG, 'T')
    low = module_rank.module_rank(means_frame(base + [('geneset', CATALOG, 'L', 'microglia', 50, -9.0)]), 'geneset', CATALOG, 'L')
    tie = module_rank.module_rank(means_frame(base + [('geneset', CATALOG, 'E', 'microglia', 50, 4.0)]), 'geneset', CATALOG, 'E')
    assert (top['rank'], low['rank']) == (1.0, 0.0)
    assert tie['rank'] == pytest.approx((4 + 0.5) / 10)


def test_only_the_same_source_counts_and_the_module_is_not_its_own_peer():
    frame = means_frame(many_modules(12) + many_modules(30, source='ucell') + [('geneset', CATALOG, 'MINE', 'microglia', 80, 5.5)])
    result = module_rank.module_rank(frame, 'geneset', CATALOG, 'MINE')
    assert result['n_modules_compared'] == 12
    assert module_rank.module_rank(frame, 'ucell', CATALOG, 'MINE') == {
        'available': False, 'reason': 'no score of this gene set was found in the score file of this dataset', 'rank': None, 'n_modules_compared': 0, 'n_cells': 0, 'mean': None, 'std': None, 'cell_types': []}


def test_same_module_name_in_another_catalog_is_a_peer():
    frame = means_frame(many_modules(10) + [('geneset', 'Other/Cat', 'MINE', 'microglia', 100, 3.5), ('geneset', CATALOG, 'MINE', 'microglia', 100, 7.5)])
    assert module_rank.module_rank(frame, 'geneset', CATALOG, 'MINE')['n_modules_compared'] == 11


def test_peers_come_from_the_same_cell_type_only():
    rows = many_modules(12, 'microglia') + [(s, 'Other/Cat', f'a{i}', 'astrocyte', 100, 1000.0 + i) for s in ('geneset',) for i in range(12)]
    rows.append(('geneset', CATALOG, 'MINE', 'microglia', 100, 5.5))
    result = module_rank.module_rank(means_frame(rows), 'geneset', CATALOG, 'MINE')
    assert result['rank'] == pytest.approx(6 / 12) and result['n_modules_compared'] == 12


def test_module_over_several_cell_types_gets_the_cell_weighted_mean_rank():
    rows = many_modules(10, 'microglia') + many_modules(10, 'astrocyte')
    rows += [('geneset', CATALOG, 'MINE', 'microglia', 300, 99.0), ('geneset', CATALOG, 'MINE', 'astrocyte', 100, -9.0)]
    result = module_rank.module_rank(means_frame(rows), 'geneset', CATALOG, 'MINE')
    assert result['rank'] == pytest.approx((300 * 1.0 + 100 * 0.0) / 400)
    assert result['n_cells'] == 400 and result['mean'] == pytest.approx((300 * 99.0 - 100 * 9.0) / 400)
    assert [entry['cell_type'] for entry in result['cell_types']] == ['microglia', 'astrocyte']


def test_small_strata_and_peers_are_not_ranked():
    # a stratum of the module below the cell floor is listed but not ranked; weights use ranked strata only
    rows = many_modules(10, 'microglia') + many_modules(10, 'astrocyte')
    rows += [('geneset', CATALOG, 'MINE', 'microglia', 100, 99.0), ('geneset', CATALOG, 'MINE', 'astrocyte', module_rank.MIN_CELLS - 1, -9.0)]
    result = module_rank.module_rank(means_frame(rows), 'geneset', CATALOG, 'MINE')
    assert result['rank'] == 1.0
    assert result['cell_types'][1]['rank'] is None
    # peers with too few cells are dropped from the comparison
    few = means_frame(many_modules(9) + [('geneset', 'Other/Cat', 'tiny', 'microglia', 3, 50.0), ('geneset', CATALOG, 'MINE', 'microglia', 100, 5.5)])
    assert module_rank.module_rank(few, 'geneset', CATALOG, 'MINE')['rank'] is None


def test_too_few_peers_reports_why():
    frame = means_frame(many_modules(module_rank.MIN_PEERS - 1) + [('geneset', CATALOG, 'MINE', 'microglia', 100, 1.0)])
    result = module_rank.module_rank(frame, 'geneset', CATALOG, 'MINE')
    assert result['available'] is False and result['rank'] is None and 'too few' in result['reason']
    assert result['n_cells'] == 100 and result['n_modules_compared'] == module_rank.MIN_PEERS - 1


def make_dataset(tmp_path: Path, with_cell_types: bool = True, n_cells: int = 60) -> Tuple[Path, Path]:
    """A duckdb with ``__cells`` and an ``obscat`` view, plus a score sidecar of 12 'all' modules and MINE on microglia."""
    duckdb_path = tmp_path / 'ds.duckdb'
    conn = duckdb.connect(str(duckdb_path))
    conn.execute('CREATE TABLE __cells AS SELECT i AS cell_id, \'c\' || i AS cell_name FROM range(?) t(i)', [n_cells])
    if with_cell_types:
        conn.execute("CREATE TABLE __obscat AS SELECT i AS cell_id, CASE WHEN i < 40 THEN 'microglial cell' WHEN i < 50 THEN 'astrocyte' END AS ant_cell_type FROM range(?) t(i)", [n_cells])
    else:
        conn.execute("CREATE TABLE __obscat AS SELECT i AS cell_id, 'x' AS other FROM range(?) t(i)", [n_cells])
    conn.execute('CREATE VIEW obscat AS SELECT c.cell_name, o.* FROM __obscat o JOIN __cells c USING (cell_id)')
    conn.close()
    rows = []
    for index in range(12):
        for cell_id in range(n_cells):
            rows.append(('geneset', 'Other/Cat', f'm{index}', cell_id, float(index)))
    for cell_id in range(40):
        rows.append(('geneset', CATALOG, 'MINE', cell_id, 5.5))
    frame = pd.DataFrame(rows, columns=['source', 'catalog', 'module', 'cell_id', 'score'])
    sidecar = tmp_path / 'ds.scores.parquet'
    frame.to_parquet(sidecar, index=False)
    return duckdb_path, sidecar


def read_means(duckdb_path: Path, sidecar: Path) -> pd.DataFrame:
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        return module_rank.stratum_means(conn, sidecar, module_rank.cell_strata(conn))
    finally:
        conn.close()


def test_stratum_means_use_cell_type_groups(tmp_path):
    duckdb_path, sidecar = make_dataset(tmp_path)
    means = read_means(duckdb_path, sidecar)
    mine = means[means['module'] == 'MINE']
    assert dict(zip(mine['stratum'], mine['n'])) == {'microglia': 40}
    other = means[means['module'] == 'm3'].set_index('stratum')
    assert other.loc['microglia', 'n'] == 40 and other.loc['astrocyte', 'n'] == 10 and other.loc[module_rank.UNLABELLED, 'n'] == 10
    assert (other['mean'] == 3.0).all()
    assert module_rank.module_rank(means, 'geneset', CATALOG, 'MINE')['rank'] == pytest.approx(6 / 12)


def test_dataset_without_cell_types_is_one_stratum(tmp_path):
    duckdb_path, sidecar = make_dataset(tmp_path, with_cell_types=False)
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    assert module_rank.cell_strata(conn) is None
    means = module_rank.stratum_means(conn, sidecar, None)
    conn.close()
    assert set(means['stratum']) == {''}
    assert module_rank.module_rank(means, 'geneset', CATALOG, 'MINE')['n_modules_compared'] == 12


def test_store_builds_once_caches_on_disk_and_replaces_old_fingerprints(tmp_path):
    store = module_rank.ModuleRankStore(tmp_path / 'cache')
    calls: List[int] = []

    def build() -> pd.DataFrame:
        calls.append(1)
        return means_frame(many_modules(3))

    first = store.get('G/ds', 'v1', build)
    assert store.get('G/ds', 'v1', build) is first and len(calls) == 1
    fresh = module_rank.ModuleRankStore(tmp_path / 'cache')
    assert len(fresh.get('G/ds', 'v1', build)) == 3 and len(calls) == 1      # read back from parquet
    store.get('G/ds', 'v2', build)
    assert len(calls) == 2
    assert [path.name for path in (tmp_path / 'cache').glob('*.parquet')] == ['G__ds~v2.parquet']


def test_store_single_flight_under_concurrent_requests(tmp_path):
    store = module_rank.ModuleRankStore(tmp_path / 'cache')
    started = threading.Event()
    calls: List[int] = []

    def build() -> pd.DataFrame:
        calls.append(1)
        started.wait(0.2)
        return means_frame(many_modules(3))

    threads = [threading.Thread(target=store.get, args=('G/ds', 'v1', build)) for _ in range(6)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert len(calls) == 1


def test_store_rebuilds_a_corrupt_cache_file(tmp_path):
    store = module_rank.ModuleRankStore(tmp_path / 'cache')
    (tmp_path / 'cache').mkdir()
    (tmp_path / 'cache' / 'G__ds~v1.parquet').write_bytes(b'not parquet')
    assert len(store.get('G/ds', 'v1', lambda: means_frame(many_modules(2)))) == 2


# ---- the route ----

_ROUTE_WANTED = {'_MODULE_RANK_STORE', '_MODULE_RANK_LOCK', '_MODULE_RANK_HELP_TOPICS', '_module_rank_store', '_module_rank_means',
                 'get_geneset_rank', '_require_geneset_registry', 'get_database_module_ranks'}


class _Pool:
    def __init__(self, paths: Dict[str, Path]):
        self.paths = paths

    def get_connection(self, db_url: str):
        return duckdb.connect(db_url, read_only=True)

    def return_connection(self, db_url: str, conn: Any) -> None:
        conn.close()


class _Gene:
    def to_detail(self) -> Dict[str, Any]:
        return {'display_name': 'Mine set'}


class _Registry:
    def __init__(self, paths: Dict[str, str]):
        self.paths = paths
        self.databases = {db_id: object() for db_id in paths}

    def get_database_path(self, db_id: str) -> str:
        return self.paths[db_id]


def build_route(tmp_path: Path, paths: Dict[str, str], scored_in: Dict[str, List[str]]):
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.testclient import TestClient

    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if name in _ROUTE_WANTED:
            body.append(node)
    app = FastAPI()
    known = {'Study/Exp/MINE': _Gene()}
    geneset_registry = type('G', (), {'get': staticmethod(lambda geneset_id: known.get(geneset_id))})()
    namespace: Dict[str, Any] = {
        'app': app, 'registry': _Registry(paths), 'connection_pool': _Pool({}), 'geneset_registry': geneset_registry,
        '_score_index_by_geneset': scored_in, 'module_rank_lib': module_rank, 'threading': threading,
        'get_tmp_dir': lambda: tmp_path / 'tmp', 'HTTPException': HTTPException, 'Query': Query,
        '_with_help': lambda payload, *topics: {**payload, 'help_topics': list(topics)},
        'SCORE_COLUMN_TEMPLATE': '{study}::{experiment}::{name}',
        'Dict': Dict, 'List': List, 'Any': Any, 'Optional': Optional, 'Tuple': Tuple, 'Path': Path, 'GenesetRegistry': object,
        'unknown_database_detail': lambda db_id: f"Database '{db_id}' not found", '_visible_db_ids': list,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return TestClient(app)


def test_route_returns_rank_per_dataset_and_lists_unavailable_ones(tmp_path):
    duckdb_path, _ = make_dataset(tmp_path)
    client = build_route(tmp_path, {'G/ds': str(duckdb_path), 'G/remote': 's3://bucket/x.duckdb', 'G/noscores': str(tmp_path / 'none.duckdb')},
                         {'Study/Exp/MINE': ['G/ds', 'G/remote', 'G/noscores']})
    (tmp_path / 'none.duckdb').write_bytes(b'')
    body = client.get('/genesets/Study/Exp/MINE/rank').json()
    assert body['primary_source'] == 'geneset' and body['help_topics'] == ['geneset-view', 'module-scores']
    assert [entry['db_id'] for entry in body['datasets']] == ['G/ds']
    geneset = body['datasets'][0]['geneset']
    assert geneset['available'] and geneset['rank'] == pytest.approx(0.5) and geneset['n_modules_compared'] == 12
    assert body['datasets'][0]['ucell']['available'] is False
    assert {item['db_id']: item['reason'] for item in body['not_available']} == {
        'G/remote': 'the dataset is not stored on this server', 'G/noscores': 'no per-cell scores for this dataset'}


def test_route_filters_one_dataset_and_rejects_unknown_ones(tmp_path):
    duckdb_path, _ = make_dataset(tmp_path)
    client = build_route(tmp_path, {'G/ds': str(duckdb_path)}, {'Study/Exp/MINE': ['G/ds']})
    assert len(client.get('/genesets/Study/Exp/MINE/rank', params={'db_id': 'G/ds'}).json()['datasets']) == 1
    assert client.get('/genesets/Study/Exp/MINE/rank', params={'db_id': 'G/other'}).status_code == 404
    assert client.get('/genesets/Study/Exp/NOPE/rank').status_code == 404


def test_route_is_registered_in_the_source():
    assert '@app.get("/genesets/{study}/{experiment}/{name}/rank"' in SERVE_PATH.read_text()


def test_pooled_std_combines_strata_like_one_sample():
    first, second = np.array([1.0, 2.0, 3.0, 4.0]), np.array([10.0, 11.0, 12.0])
    frame = means_frame([('geneset', CATALOG, 'M', 'a', len(first), first.mean(), first.std(ddof=1)),
                         ('geneset', CATALOG, 'M', 'b', len(second), second.mean(), second.std(ddof=1))])
    result = module_rank.module_rank(frame, 'geneset', CATALOG, 'M')
    assert result['std'] == pytest.approx(np.concatenate([first, second]).std(ddof=1))
    assert sorted(entry['std'] for entry in result['cell_types']) == pytest.approx(sorted([first.std(ddof=1), second.std(ddof=1)]))


def test_std_missing_for_a_single_cell():
    frame = means_frame([('geneset', CATALOG, 'M', 'a', 1, 2.0, float('nan'))])
    assert module_rank.module_rank(frame, 'geneset', CATALOG, 'M')['std'] is None


def test_store_rebuilds_a_cache_written_without_std(tmp_path):
    store = module_rank.ModuleRankStore(tmp_path)
    old = pd.DataFrame([('geneset', CATALOG, 'M', 'a', 10, 1.0)], columns=module_rank.MEANS_COLUMNS[:-1])
    old.to_parquet(store._file('A/b', 'fp'), index=False)
    fresh = means_frame([('geneset', CATALOG, 'M', 'a', 10, 1.0, 0.5)])
    assert list(store.get('A/b', 'fp', lambda: fresh).columns) == module_rank.MEANS_COLUMNS


def test_dataset_route_lists_every_scored_gene_set_with_rank_and_std(tmp_path):
    duckdb_path, _ = make_dataset(tmp_path)
    client = build_route(tmp_path, {'G/ds': str(duckdb_path), 'G/remote': 's3://bucket/x.duckdb'}, {'Study/Exp/MINE': ['G/ds']})
    body = client.get('/databases/G/ds/module-ranks').json()
    assert body['available'] and body['help_topics'] == ['geneset-view', 'module-scores']
    mine = [row for row in body['modules'] if row['geneset_id'] == 'Study/Exp/MINE'][0]
    assert mine['rank'] == pytest.approx(0.5) and mine['display_name'] == 'Mine set' and mine['column_name'] == 'Study::Exp::MINE'
    assert mine['std'] is not None and mine['n_cells'] > 0 and len(body['modules']) == 13
    assert client.get('/databases/G/remote/module-ranks').json() == {'db_id': 'G/remote', 'available': False,
                                                                    'reason': 'the dataset is not stored on this server', 'modules': []}
    assert client.get('/databases/G/none/module-ranks').status_code == 404
