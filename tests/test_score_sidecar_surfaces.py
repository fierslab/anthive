"""Module scores held only in ``<stem>.scores.parquet`` reach every surface that lists scores.

Covers ``GET /databases/{id}/module_scores`` (cut out of ant-serve with ``ast``, as in
``test_score_index_sidecars.py``), the ``score_sidecar`` analytics view (real DuckDB, real
sandbox), the schema description and the read helpers in ``lib/scores_parquet.py``.
"""
import ast
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pytest

import analytics
import scores_parquet

SERVE_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-serve'


def _make_dataset(folder: Path, legacy_column: Optional[str] = None) -> Path:
    """A 4-cell duckdb (``__cells``, ``obsnum``, ``obscat``) plus a scores sidecar.

    The sidecar holds two gene set scores and one UCell score; cell 3 has no
    row for ``S/E/b`` (unscored cells must not count in ``n``).
    """
    db_path = folder / 'ds.duckdb'
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE __cells AS SELECT range AS cell_id, 'c' || range AS cell_name FROM range(4)")
    legacy = f', range * 10.0 AS "{legacy_column}"' if legacy_column else ''
    conn.execute(f"CREATE TABLE __obsnum AS SELECT 'c' || range AS cell_name, range * 1.0 AS n_genes{legacy} FROM range(4)")
    conn.execute("CREATE VIEW obsnum AS SELECT * FROM __obsnum")
    conn.execute("CREATE TABLE __obscat AS SELECT 'c' || range AS cell_name, "
                 "CASE WHEN range < 2 THEN 'mg' ELSE 'astro' END AS cell_type FROM range(4)")
    conn.execute("CREATE VIEW obscat AS SELECT * FROM __obscat")
    conn.execute("CREATE TABLE s (cell_id BIGINT, source VARCHAR, catalog VARCHAR, module VARCHAR, score DOUBLE)")
    for cell in range(4):
        conn.execute("INSERT INTO s VALUES (?, 'geneset', 'S/E', 'a', ?)", [cell, float(cell)])
        conn.execute("INSERT INTO s VALUES (?, 'ucell', 'S/E', 'a', ?)", [cell, cell / 10])
        if cell < 3:
            conn.execute("INSERT INTO s VALUES (?, 'geneset', 'S/E', 'b', ?)", [cell, 2.0])
    conn.execute(f"COPY s TO '{folder / 'ds.scores.parquet'}' (FORMAT parquet)")
    conn.execute("DROP TABLE s")
    conn.close()
    return db_path


# ── lib/scores_parquet.py read helpers ────────────────────────────────────────

def test_virtual_column_names_match_the_served_obs_names():
    assert scores_parquet.virtual_column_name('geneset', 'S/E', 'a') == 'S::E::a'
    assert scores_parquet.virtual_column_name('ucell', 'S/E', 'a') == 'ucell__S/E__a'
    assert scores_parquet.virtual_column_name('starcat', 'MG', 'p') == 'starcat__MG__p'


def test_sidecar_lookup_skips_s3_and_missing_files(tmp_path):
    db_path = _make_dataset(tmp_path)
    assert scores_parquet.sidecar_for_db_url(str(db_path)) == tmp_path / 'ds.scores.parquet'
    assert scores_parquet.sidecar_for_db_url(f'file://{db_path}') == tmp_path / 'ds.scores.parquet'
    assert scores_parquet.sidecar_for_db_url('s3://bucket/ds.duckdb') is None
    assert scores_parquet.sidecar_for_db_url(str(tmp_path / 'other.duckdb')) is None


def test_stats_count_scored_cells_and_refresh_on_rewrite(tmp_path):
    db_path = _make_dataset(tmp_path)
    sidecar = tmp_path / 'ds.scores.parquet'
    stats = scores_parquet.sidecar_score_stats(sidecar)
    assert stats[('geneset', 'S/E', 'a')]['n'] == 4
    assert stats[('geneset', 'S/E', 'a')]['mean'] == pytest.approx(1.5)
    assert stats[('geneset', 'S/E', 'b')]['n'] == 3
    assert scores_parquet.sidecar_score_stats(sidecar) is stats  # cached
    scores_parquet.delete_scores(db_path, 'geneset', 'S/E')
    os.utime(sidecar, (1, os.path.getmtime(sidecar) + 10))
    assert set(scores_parquet.sidecar_score_stats(sidecar)) == {('ucell', 'S/E', 'a')}


# ── analytics sandbox ─────────────────────────────────────────────────────────

def test_analytics_reads_sidecar_scores_through_the_view(tmp_path, monkeypatch):
    pytest.importorskip('pyarrow')
    monkeypatch.setenv('ANTHIVE_ANALYTICS_TMP', str(tmp_path / 'analytics'))
    db_path = _make_dataset(tmp_path)
    sql = scores_parquet.score_query_sql('geneset', 'S/E', 'a')
    analytics.validate_sql(sql)
    result = analytics.run_query(str(db_path), sql + ' ORDER BY cell_name')
    assert result['columns'] == ['cell_name', 'S::E::a']
    assert [row['S::E::a'] for row in result['preview']] == [0.0, 1.0, 2.0, 3.0]
    joined = analytics.run_query(
        str(db_path),
        "SELECT o.cell_type, AVG(s.score) AS mean_score FROM score_sidecar s JOIN obscat o USING (cell_name) "
        "WHERE s.source = 'geneset' AND s.catalog = 'S/E' AND s.module = 'a' GROUP BY 1 ORDER BY 1")
    assert [(row['cell_type'], row['mean_score']) for row in joined['preview']] == [('astro', 2.5), ('mg', 0.5)]


def test_schema_examples_run_in_the_sandbox(tmp_path, monkeypatch):
    pytest.importorskip('pyarrow')
    monkeypatch.setenv('ANTHIVE_ANALYTICS_TMP', str(tmp_path / 'analytics'))
    db_path = _make_dataset(tmp_path)
    schema = scores_parquet.score_views_schema(str(db_path), group_column='cell_type')
    assert schema['available'] and schema['view'] == 'score_sidecar'
    assert schema['n_scores'] == 3 and schema['n_scores_by_source'] == {'geneset': 2, 'ucell': 1}
    assert len(schema['example_sql']) == 3
    for sql in schema['example_sql']:
        analytics.validate_sql(sql)
        assert analytics.run_query(str(db_path), sql)['row_count'] > 0
    wide = analytics.run_query(str(db_path), schema['example_sql'][2] + ' ORDER BY cell_name')
    assert wide['columns'] == ['cell_name', 'S::E::a', 'S::E::b']
    unscored = wide['preview'][3]['S::E::b']
    assert unscored is None or unscored != unscored  # NULL (NaN in a float preview column)


def test_schema_reports_why_scores_are_not_queryable(tmp_path):
    assert scores_parquet.score_views_schema('s3://bucket/ds.duckdb')['available'] is False
    no_sidecar = scores_parquet.score_views_schema(str(tmp_path / 'none.duckdb'))
    assert no_sidecar == {'available': False, 'reason': 'this dataset has no scores sidecar'}


def test_sandbox_without_sidecar_has_no_view_and_still_works(tmp_path, monkeypatch):
    pytest.importorskip('pyarrow')
    monkeypatch.setenv('ANTHIVE_ANALYTICS_TMP', str(tmp_path / 'analytics'))
    db_path = _make_dataset(tmp_path)
    (tmp_path / 'ds.scores.parquet').unlink()
    assert analytics.run_query(str(db_path), 'SELECT COUNT(*) AS n FROM obsnum')['preview'][0]['n'] == 4
    with pytest.raises(RuntimeError, match='score_sidecar'):
        analytics.run_query(str(db_path), 'SELECT * FROM score_sidecar')


def test_sandbox_view_does_not_open_other_files(tmp_path):
    db_path = _make_dataset(tmp_path)
    (tmp_path / 'secret.txt').write_text('CANARY')
    conn = analytics._open_analytics_connection(str(db_path))
    try:
        assert conn.execute('SELECT COUNT(*) FROM score_sidecar').fetchone()[0] == 11
        with pytest.raises(duckdb.Error):
            conn.execute(f"SELECT * FROM read_text('{tmp_path / 'secret.txt'}')").fetchall()
    finally:
        conn.close()


# ── GET /databases/{id}/module_scores ─────────────────────────────────────────

class _Pool:
    def get_connection(self, db_url: str):
        conn = duckdb.connect(':memory:')
        conn.execute(f"ATTACH '{db_url}' AS db (READ_ONLY)")
        conn.execute('USE db')
        return conn

    def return_connection(self, db_url: str, conn) -> None:
        conn.close()


class _Registry:
    def __init__(self, databases: Dict[str, str]) -> None:
        self.databases = databases

    def get_database_path(self, db_id: str) -> str:
        return self.databases[db_id]


class _HTTPException(Exception):
    def __init__(self, status_code: int, detail: Any = None) -> None:
        super().__init__(detail)
        self.status_code = status_code


def _module_scores_handler(databases: Dict[str, str]):
    tree = ast.parse(SERVE_PATH.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_database_module_scores')
    node.decorator_list = []
    from anthive_db import safe_ident  # noqa: PLC0415
    from geneset import SCORE_COLUMN_SEPARATOR, SCORE_COLUMN_TEMPLATE, is_score_column, parse_score_column  # noqa: PLC0415
    namespace: Dict[str, Any] = {
        'registry': _Registry(databases), 'connection_pool': _Pool(), 'geneset_registry': None,
        'HTTPException': _HTTPException, 'unknown_database_detail': lambda db_id: f'unknown {db_id}',
        'is_score_column': is_score_column, 'parse_score_column': parse_score_column, 'safe_ident': safe_ident,
        'scores_parquet_lib': scores_parquet, 'log': logging.getLogger('test'),
        'SCORE_COLUMN_SEPARATOR': SCORE_COLUMN_SEPARATOR, 'SCORE_COLUMN_TEMPLATE': SCORE_COLUMN_TEMPLATE,
        'Dict': Dict, 'Any': Any, 'Optional': Optional, 'List': List, 'Tuple': Tuple,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return namespace['get_database_module_scores']


def test_module_scores_lists_sidecar_only_scores(tmp_path):
    db_path = _make_dataset(tmp_path)
    payload = _module_scores_handler({'X/Y': str(db_path)})('X/Y')
    assert payload['count'] == 2 and payload['count_by_storage'] == {'scores_sidecar': 2}
    first, second = payload['scored_genesets']
    assert (first['geneset_id'], first['column_name'], first['storage']) == ('S/E/a', 'S::E::a', 'scores_sidecar')
    assert first['query_table'] == 'score_sidecar'
    assert first['stats']['n'] == 4 and first['stats']['median'] == pytest.approx(1.5)
    assert first['ucell_column_name'] == 'ucell__S/E__a' and first['ucell_stats']['n'] == 4
    assert second['geneset_id'] == 'S/E/b' and second['stats']['n'] == 3 and 'ucell_column_name' not in second
    conn = duckdb.connect()
    conn.execute(f"ATTACH '{db_path}' AS db (READ_ONLY)")
    conn.execute('USE db')
    scores_parquet.install_score_views(conn, str(db_path))
    assert conn.execute(first['query_sql'] + ' ORDER BY cell_name').fetchall()[-1] == ('c3', 3.0)
    conn.close()


def test_module_scores_keeps_duckdb_columns_and_prefers_them(tmp_path):
    db_path = _make_dataset(tmp_path, legacy_column='S::E::a')
    payload = _module_scores_handler({'X/Y': str(db_path)})('X/Y')
    rows = {row['column_name']: row for row in payload['scored_genesets']}
    assert payload['count'] == 2 and payload['count_by_storage'] == {'duckdb': 1, 'scores_sidecar': 1}
    assert rows['S::E::a']['storage'] == 'duckdb' and rows['S::E::a']['query_table'] == 'obsnum'
    assert rows['S::E::a']['stats']['max'] == 30.0
    assert rows['S::E::a']['query_sql'] == 'SELECT cell_name, "S::E::a" FROM obsnum'
    assert rows['S::E::b']['storage'] == 'scores_sidecar'


def test_module_scores_unknown_dataset_is_404(tmp_path):
    with pytest.raises(_HTTPException) as excinfo:
        _module_scores_handler({})('X/Y')
    assert excinfo.value.status_code == 404
