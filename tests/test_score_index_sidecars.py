"""``_build_score_index`` merges legacy obs score columns with the sidecar gene sets."""
import ast
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

import duckdb
import pytest

SERVE_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-serve'
_WANTED = {
    '_build_score_index', '_sidecar_score_triples', '_SIDECAR_TRIPLE_CACHE', '_SIDECAR_TRIPLE_LOCK',
    '_score_index_by_db', '_score_index_by_geneset', '_score_columns',
}


class _Pool:
    """Hands out in-memory connections that only carry an obsnum table."""

    def __init__(self, obs_columns: Dict[str, List[str]]) -> None:
        self.obs_columns = obs_columns

    def get_connection(self, db_url: str):
        conn = duckdb.connect(':memory:')
        columns = ', '.join(f'"{c}" DOUBLE' for c in self.obs_columns.get(db_url, ['x']))
        conn.execute(f'CREATE TABLE obsnum ({columns})')
        return conn

    def return_connection(self, db_url: str, conn) -> None:
        conn.close()


class _Registry:
    def __init__(self, databases: Dict[str, str]) -> None:
        self.databases = databases

    def get_database_path(self, db_id: str) -> str:
        return self.databases[db_id]


def _load(databases: Dict[str, str], obs_columns: Dict[str, List[str]]) -> Dict[str, Any]:
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if name in _WANTED:
            body.append(node)
    from geneset import parse_score_column  # noqa: PLC0415
    namespace: Dict[str, Any] = {
        'registry': _Registry(databases), 'connection_pool': _Pool(obs_columns), 'duckdb': duckdb,
        'os': os, 'threading': threading, 'log': logging.getLogger('test'), 'parse_score_column': parse_score_column,
        'Dict': Dict, 'List': List, 'Tuple': Tuple,
        '_MODULE_INFO_CACHE': {}, '_GENE_CONTEXT_STATE': {},
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return namespace


def _write_sidecar(path: Path, rows: List[Tuple[str, str, str]]) -> None:
    conn = duckdb.connect(':memory:')
    conn.execute('CREATE TABLE s (cell_id INT, source VARCHAR, catalog VARCHAR, module VARCHAR, score DOUBLE)')
    for source, catalog, module in rows:
        for cell in range(3):
            conn.execute('INSERT INTO s VALUES (?, ?, ?, ?, 1.0)', [cell, source, catalog, module])
    conn.execute(f"COPY s TO '{path}' (FORMAT parquet)")
    conn.close()


def test_sidecar_genesets_are_indexed_and_deduplicated(tmp_path):
    db_a, db_b, db_c = (str(tmp_path / f'{name}.duckdb') for name in 'abc')
    _write_sidecar(Path(db_a.replace('.duckdb', '.scores.parquet')), [
        ('geneset', 'Mancuso2024/Cell_states', 'DAM'),
        ('ucell', 'Mancuso2024/Cell_states', 'DAM'),
        ('starcat', 'cat', 'mod'),
        ('geneset', 'Legacy/Exp', 'Both'),
    ])
    ns = _load({'a': db_a, 'b': db_b, 'c': db_c}, {db_b: ['Mancuso2024::Cell_states::DAM', 'plain'], db_a: ['Legacy::Exp::Both']})
    ns['_build_score_index']()
    assert ns['_score_index_by_geneset']['Mancuso2024/Cell_states/DAM'] == ['a', 'b']
    assert ns['_score_index_by_db']['a'] == ['Legacy/Exp/Both', 'Mancuso2024/Cell_states/DAM']
    assert ns['_score_index_by_geneset']['Legacy/Exp/Both'] == ['a']
    assert ns['_score_columns'][('a', 'Mancuso2024/Cell_states/DAM')] == 'Mancuso2024::Cell_states::DAM'
    assert 'c' not in ns['_score_index_by_db']


def test_s3_databases_skip_sidecars(tmp_path):
    ns = _load({'s': 's3://bucket/x.duckdb'}, {})
    ns['_build_score_index']()
    assert ns['_score_index_by_db'] == {}


def test_triples_cached_by_mtime_and_refreshed(tmp_path):
    sidecar = tmp_path / 'a.scores.parquet'
    _write_sidecar(sidecar, [('geneset', 'S/E', 'one')])
    ns = _load({}, {})
    first = ns['_sidecar_score_triples'](str(sidecar))
    assert first == [('geneset', 'S/E', 'one')]
    assert ns['_sidecar_score_triples'](str(sidecar)) is first
    _write_sidecar(sidecar, [('geneset', 'S/E', 'two')])
    os.utime(sidecar, (1, os.path.getmtime(sidecar) + 10))
    assert ns['_sidecar_score_triples'](str(sidecar)) == [('geneset', 'S/E', 'two')]


def test_missing_and_corrupt_sidecars_give_empty(tmp_path):
    ns = _load({}, {})
    assert ns['_sidecar_score_triples'](str(tmp_path / 'none.parquet')) == []
    corrupt = tmp_path / 'bad.scores.parquet'
    corrupt.write_text('not parquet')
    assert ns['_sidecar_score_triples'](str(corrupt)) == []
