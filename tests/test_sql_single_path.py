"""One path for user SQL (issues #2, #3, #4, #5, #83, review of 2026-11-04).

Covers:
- the allowlist over DuckDB's parse tree in ``analytics.validate_sql``
  (table functions, base tables, qualifiers, scalar functions, one statement,
  length and nesting limits) and the two bypasses the review reproduced
  (a file path used as a table in a comma join, SQL hidden in a string and run
  by json_execute_serialized_sql);
- ``analytics.execute_query`` on a real locked-down instance: published
  relations only, no Python replacement scans, row cap around the query,
  trailing semicolons, timeout, memory limit, error types;
- the hidden-dataset filter on the fleet-wide ``module_variability`` view;
- the two routes on the real ant-serve app: ``POST /databases/{id}/query/sql``
  and ``POST /databases/{id}/analytics/query``.
"""

import time
from pathlib import Path

import duckdb
import pandas as pd
import pytest

import analytics
from analytics import (
    ALLOWED_TABLE_FUNCTIONS,
    SqlReferences,
    check_relations,
    execute_query,
    published_relations,
    validate_sql,
)

# Route fixtures (real ant-serve app over a converted synthetic dataset).
from test_layer_path_portability import moved_store, serve_client  # noqa: F401

CANARY = 'CANARY_SECRET_VALUE'


# ── Fixture: one dataset with hidden columns, a neighbour and a secret ───────

@pytest.fixture
def dataset(tmp_path, monkeypatch):
    """A small dataset as ant-convert lays it out, next to files a query must
    never reach: another dataset's parquet and a config file with a secret."""
    monkeypatch.setenv('ANTHIVE_ANALYTICS_TMP', str(tmp_path / 'analytics'))
    store = tmp_path / 'store'
    (store / 'grp').mkdir(parents=True)
    (store / 'other').mkdir()
    secret = store / 'anthive.yaml'
    secret.write_text(f'secret_access_key: {CANARY}\n')
    neighbour = store / 'other' / 'internal.layer.X.by_cell.parquet'
    pd.DataFrame({'cell_id': [0], 'gene_id': [0], 'value': [7.0],
                  'note': [CANARY]}).to_parquet(neighbour, index=False)
    sidecar = store / 'grp' / 'ds.layer.X.by_cell.parquet'
    pd.DataFrame({'cell_id': list(range(5)), 'gene_id': [0] * 5,
                  'value': [i * 1.5 for i in range(5)]}).to_parquet(sidecar, index=False)
    db_path = store / 'grp' / 'ds.duckdb'
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE __cells AS SELECT range AS cell_id, 'c' || range AS cell_name FROM range(5)")
    conn.execute("CREATE TABLE __genes AS SELECT 0 AS gene_id, 'APOE' AS gene_name")
    conn.execute(f"CREATE TABLE __obscat AS SELECT range AS cell_id, 'mg' AS cell_type, "
                 f"'{CANARY}' AS hidden_capdate FROM range(5)")
    conn.execute("CREATE TABLE __pending_cols (col_name VARCHAR, annotator VARCHAR)")
    conn.execute(f"INSERT INTO __pending_cols VALUES ('x', '{CANARY}')")
    conn.execute("CREATE VIEW obscat AS SELECT c.cell_name, o.cell_type "
                 "FROM __obscat o JOIN __cells c USING (cell_id)")
    conn.execute(f"CREATE VIEW layer__X AS SELECT * FROM read_parquet('{sidecar}')")
    conn.close()
    return {'db': db_path, 'store': store, 'secret': secret, 'neighbour': neighbour,
            'tmp': tmp_path}


def _run(dataset, sql, **kwargs):
    """execute_query, with the Arrow result as a pandas frame."""
    kwargs.setdefault('row_limit', 1000)
    table, truncated = execute_query(str(dataset['db']), sql, **kwargs)
    return table.to_pandas(), truncated


# ── validate_sql: the allowlist ───────────────────────────────────────────────

class TestTreeAllowlist:

    @pytest.mark.security
    @pytest.mark.parametrize('sql', [
        # The two bypasses reproduced by the review (#2).
        "SELECT * FROM obscat, '/etc/hostname'",
        "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT * FROM read_text(''/etc/hostname'')'))",
        # Path-as-table in places a token scan does not look.
        "SELECT * FROM obscat, LATERAL (SELECT * FROM 'x.parquet')",
        "WITH t AS (FROM 'x.parquet') SELECT * FROM t",
        "SELECT (SELECT count(*) FROM 's3://bucket/prefix/a.parquet')",
        'SELECT * FROM "/etc/hostname"',
        'SELECT * FROM "x.parquet"',
        # Schema leaks (#3): catalog functions and views.
        "SELECT * FROM duckdb_views()",
        "SELECT * FROM duckdb_columns()",
        "SELECT * FROM duckdb_tables()",
        "SELECT * FROM pragma_storage_info('__obscat')",
        "SELECT * FROM pragma_table_info('__obscat')",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM information_schema.columns",
        "SELECT * FROM pg_catalog.pg_class",
        "SELECT * FROM system.main.duckdb_views",
        "SELECT * FROM other_db.main.obscat",
        # Other table functions and engine state.
        "SELECT * FROM glob('*')",
        "SELECT * FROM read_blob('/etc/hostname')",
        "SELECT * FROM parquet_metadata('x.parquet')",
        "SELECT * FROM duckdb_secrets()",
        "SELECT * FROM which_secret('s3://x', 's3')",
        "SELECT * FROM query_table('__obscat')",
        "SELECT * FROM summary((SELECT 1))",
        "SELECT * FROM test_all_types()",
        "SELECT * FROM main.range(3)",
        "SELECT current_setting('s3_secret_access_key')",
        "SELECT current_query()",
        "SELECT json_serialize_sql('SELECT 1')",
        "SELECT json_deserialize_sql('{}')",
        "SELECT main.current_setting('x')",
        "SELECT write_log('x')",
        # Internal tables.
        "SELECT * FROM __pending_cols",
        "SELECT * FROM __gene_bins",
        "SELECT * FROM db.main.__obscat",
        # Statement shape.
        "SELECT 1; SELECT 2",
        "DESCRIBE obscat",
        "SUMMARIZE obscat",
        "SHOW TABLES",
    ])
    def test_refused(self, sql):
        with pytest.raises(ValueError):
            validate_sql(sql)

    @pytest.mark.parametrize('sql,tables', [
        ("SELECT * FROM obscat", {'obscat'}),
        ("SELECT * FROM db.obscat", {'obscat'}),
        ("SELECT * FROM db.main.obscat", {'obscat'}),
        ("SELECT * FROM main.obscat", {'obscat'}),
        ("SELECT o.* FROM obscat o JOIN __cells c USING (cell_name)", {'obscat', '__cells'}),
        ("SELECT * FROM __genes", {'__genes'}),
        ("SELECT * FROM range(3)", set()),
        ("SELECT * FROM generate_series(1, 3)", set()),
        ("SELECT unnest([1, 2])", set()),
        ("SELECT * FROM unnest([1, 2])", set()),
        ("SELECT * FROM json_each('{\"a\": 1}')", set()),
        ("SELECT * FROM json_tree('{\"a\": 1}')", set()),
        ("SELECT upper(cell_name), count(*) OVER () FROM obscat", {'obscat'}),
        ("SELECT * FROM obscat USING SAMPLE 10 ROWS (reservoir)", {'obscat'}),
        ("SELECT 'read_text(x)', 'FROM ''a.parquet''' AS s", set()),
        ("SELECT 1;", set()),
        ("SELECT 1; -- trailing comment", set()),
    ])
    def test_allowed_and_references(self, sql, tables):
        references = validate_sql(sql)
        assert set(references.tables) == tables

    def test_cte_names_are_recorded(self):
        references = validate_sql("WITH t AS (SELECT 1 AS x), u AS (SELECT * FROM t) SELECT * FROM u")
        assert references.ctes == {'t', 'u'}
        assert references.tables == {'t', 'u'}

    def test_every_allowlisted_table_function_parses(self):
        for name in ALLOWED_TABLE_FUNCTIONS:
            argument = "'{}'" if name.startswith('json_') else ('[1]' if name == 'unnest' else '1')
            validate_sql(f"SELECT * FROM {name}({argument})")

    def test_non_string_refused(self):
        with pytest.raises(ValueError):
            validate_sql(None)  # type: ignore[arg-type]

    def test_length_cap(self):
        with pytest.raises(ValueError, match='limit'):
            validate_sql('SELECT 1 ' + ' ' * analytics.MAX_SQL_CHARS)

    @pytest.mark.parametrize('sql', [
        'SELECT * FROM ' + '(SELECT * FROM ' * 600 + 'range(1)' + ') t' * 600,
        'SELECT * FROM ' + '(SELECT * FROM ' * 5000 + 'range(1)' + ') t' * 5000,
        'SELECT ' + '(' * 30000 + '1' + ')' * 30000,
        'SELECT ' + '+'.join(['1'] * 12000),
        "SELECT '" + 'x' * 1000,
        "",
        "   ",
        "-- only a comment",
        "\x00",
        "SELECT * FROM",
    ])
    def test_malformed_or_deep_input_is_a_clean_refusal(self, sql):
        started = time.monotonic()
        with pytest.raises(ValueError):
            validate_sql(sql)
        assert time.monotonic() - started < 5

    def test_validation_of_a_long_legal_query_is_fast(self):
        sql = "SELECT 1 " + " UNION ALL SELECT 1" * 2500
        assert len(sql) < analytics.MAX_SQL_CHARS
        started = time.monotonic()
        validate_sql(sql)
        assert time.monotonic() - started < 5

    def test_json_decode_failure_is_a_refusal(self, monkeypatch):
        monkeypatch.setattr(analytics.json, 'loads',
                            lambda raw: (_ for _ in ()).throw(RecursionError('deep')))
        with pytest.raises(ValueError, match='nested too deeply'):
            validate_sql('SELECT 1')

    def test_parser_error_tree_is_a_refusal(self, monkeypatch):
        monkeypatch.setattr(analytics, '_serialized_tree',
                            lambda sql: (_ for _ in ()).throw(ValueError('Could not analyse SQL: x')))
        with pytest.raises(ValueError, match='Could not analyse'):
            validate_sql('SELECT 1')


class TestTreeWalkDirect:
    """The tree walk on its own: the token scan in validate_sql catches some of
    these first, so call the second gate directly to prove it holds alone."""

    @pytest.mark.parametrize('sql', [
        "SELECT * FROM __obscat",
        "SELECT * FROM main.__obsnum",
        "SELECT * FROM read_text('/etc/hostname')",
        "SELECT * FROM obscat JOIN '/etc/hostname' ON true",
        "SELECT current_setting('memory_limit')",
    ])
    def test_second_gate_alone(self, sql):
        with pytest.raises(ValueError):
            analytics._check_tree(sql)

    def test_non_select_has_no_tree(self):
        with pytest.raises(ValueError, match='Could not analyse SQL'):
            analytics._serialized_tree('PRAGMA database_list')

    def test_parser_crash_is_a_refusal(self, monkeypatch):
        class _Broken:
            def execute(self, *args, **kwargs):
                raise duckdb.Error('boom')

            def close(self):
                pass
        monkeypatch.setattr(analytics.duckdb, 'connect', lambda *args, **kwargs: _Broken())
        with pytest.raises(ValueError, match='Could not parse SQL'):
            analytics._serialized_tree('SELECT 1')


class TestCheckRelations:

    def test_unknown_table_named_with_published_list(self):
        references = SqlReferences(tables=frozenset({'duckdb_views'}), ctes=frozenset())
        with pytest.raises(ValueError, match="Unknown table 'duckdb_views'.*obscat"):
            check_relations(references, frozenset({'obscat', 'layer__x'}))

    def test_cte_and_published_pass(self):
        references = SqlReferences(tables=frozenset({'t', 'obscat'}), ctes=frozenset({'t'}))
        check_relations(references, frozenset({'obscat'}))

    def test_published_relations_leave_out_internal_tables(self, dataset):
        conn = analytics._open_analytics_connection(str(dataset['db']))
        try:
            names = published_relations(conn)
        finally:
            conn.close()
        assert {'obscat', 'layer__x', '__cells', '__genes'} <= names
        assert not {'__obscat', '__pending_cols'} & names
        assert 'duckdb_views' not in names


# ── execute_query on a real locked-down instance ──────────────────────────────

class TestExecuteQuery:

    @pytest.mark.security
    @pytest.mark.parametrize('template', [
        "SELECT * FROM obscat, '{secret}'",
        "SELECT * FROM obscat, '{neighbour}'",
        "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT * FROM read_text(''{secret}'')'))",
        "SELECT * FROM duckdb_views",
        "SELECT * FROM duckdb_columns",
        "SELECT sql FROM sqlite_master",
        "SELECT * FROM pragma_database_list",
        "SELECT * FROM __obscat",
        "SELECT * FROM __pending_cols",
        "SELECT * FROM read_parquet('{neighbour}')",
        "SELECT * FROM glob('{store}/**')",
    ])
    def test_attacks_refused_without_leaking(self, dataset, template):
        sql = template.format(secret=dataset['secret'], neighbour=dataset['neighbour'],
                              store=dataset['store'])
        with pytest.raises(ValueError) as excinfo:
            _run(dataset, sql)
        assert CANARY not in str(excinfo.value)

    def test_published_views_work(self, dataset):
        frame, truncated = _run(dataset, "SELECT cell_name, cell_type FROM obscat ORDER BY cell_name")
        assert list(frame['cell_name']) == ['c0', 'c1', 'c2', 'c3', 'c4']
        assert 'hidden_capdate' not in frame.columns and truncated is False
        frame, _ = _run(dataset, "SELECT SUM(value) AS s FROM layer__X")
        assert frame['s'][0] == pytest.approx(15.0)
        frame, _ = _run(dataset, "SELECT g.gene_name FROM __genes g")
        assert list(frame['gene_name']) == ['APOE']

    def test_python_replacement_scan_is_off(self, dataset):
        python_local_frame = pd.DataFrame({'leak': [CANARY]})  # noqa: F841 (the name is the attack)
        with pytest.raises(ValueError, match='Unknown table'):
            _run(dataset, "SELECT * FROM python_local_frame")
        conn = analytics._open_analytics_connection(str(dataset['db']))
        try:
            with pytest.raises(duckdb.Error):
                conn.execute("SELECT * FROM python_local_frame").fetchall()
            assert conn.execute("SELECT current_setting('python_enable_replacements')").fetchone()[0] is False
        finally:
            conn.close()

    @pytest.mark.security
    @pytest.mark.parametrize('sql', [
        "SELECT * FROM range(1000) --",
        "SELECT * FROM range(1000) -- LIMIT 1000000",
        "SELECT range AS limit_ FROM range(1000)",
        "SELECT * FROM range(1000) /* LIMIT */",
        "SELECT * FROM range(1000) LIMIT 100000",
    ])
    def test_row_cap_cannot_be_removed(self, dataset, sql):
        frame, truncated = _run(dataset, sql, row_limit=10)
        assert len(frame) == 10 and truncated is True

    def test_not_truncated_at_exact_limit(self, dataset):
        frame, truncated = _run(dataset, "SELECT * FROM range(10)", row_limit=10)
        assert len(frame) == 10 and truncated is False

    def test_row_limit_floor_is_one(self, dataset):
        frame, truncated = _run(dataset, "SELECT * FROM range(5)", row_limit=0)
        assert len(frame) == 1 and truncated is True

    @pytest.mark.parametrize('sql', [
        "SELECT 1 AS x;",
        "SELECT 1 AS x ;; ",
        "SELECT 1 AS x; -- done",
        "SELECT ';' AS s, 1 AS x",
    ])
    def test_trailing_semicolons(self, dataset, sql):
        frame, _ = _run(dataset, sql)
        assert frame['x'][0] == 1

    @pytest.mark.security
    def test_timeout_interrupts(self, dataset, monkeypatch):
        monkeypatch.setattr(analytics, 'QUERY_TIMEOUT', 1)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match='1s'):
            _run(dataset, "SELECT COUNT(*) FROM range(100000000000) a", timeout=10_000)
        assert time.monotonic() - started < 15

    @pytest.mark.security
    def test_memory_hog_fails_cleanly(self, dataset, monkeypatch):
        monkeypatch.setenv('ANTHIVE_DUCKDB_MEMORY_LIMIT', '64MB')
        with pytest.raises((RuntimeError, TimeoutError)):
            _run(dataset, "SELECT list(range) FROM range(200000000)", timeout=60)
        frame, _ = _run(dataset, "SELECT 1 AS x")  # the next query is unaffected
        assert frame['x'][0] == 1

    def test_runtime_error_is_runtime_error(self, dataset):
        with pytest.raises(RuntimeError, match='no_such_column'):
            _run(dataset, "SELECT no_such_column FROM obscat")

    def test_refusal_happens_before_connecting(self, dataset, monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError('connection opened for a refused query')
        monkeypatch.setattr(analytics, '_open_analytics_connection', _fail)
        with pytest.raises(ValueError):
            _run(dataset, "SELECT * FROM duckdb_views()")

    def test_bad_attach_session_refused(self, dataset):
        with pytest.raises(ValueError):
            _run(dataset, "SELECT 1", attach_sessions=['../../etc/passwd'])

    def test_run_query_reports_truncation(self, dataset, monkeypatch):
        monkeypatch.setattr(analytics, 'MAX_ROWS', 3)
        result = analytics.run_query(str(dataset['db']), "SELECT * FROM range(10)")
        assert result['row_count'] == 3 and result['truncated'] is True
        result = analytics.run_query(str(dataset['db']), "SELECT * FROM range(3)")
        assert result['row_count'] == 3 and result['truncated'] is False


# ── #4: hidden datasets left out of the fleet-wide view ───────────────────────

class TestExtraViewExclusions:

    @pytest.fixture
    def module_variability(self, dataset):
        path = dataset['store'] / 'module_variability.parquet'
        pd.DataFrame({'dataset': ['PUB/A', 'INT/B', 'INT/B', None],
                      'module': ['m1', 'm1', 'm2', 'm3']}).to_parquet(path, index=False)
        return str(path)

    def test_hidden_rows_left_out(self, dataset, module_variability):
        frame, _ = _run(dataset, "SELECT dataset, module FROM module_variability ORDER BY module",
                        extra_parquet_views={'module_variability': module_variability},
                        extra_view_exclusions={'module_variability': ('dataset', ['INT/B'])})
        assert 'INT/B' not in set(frame['dataset'].dropna())
        assert list(frame['module']) == ['m1', 'm3']

    def test_no_exclusions_keeps_all(self, dataset, module_variability):
        frame, _ = _run(dataset, "SELECT count(*) AS n FROM module_variability",
                        extra_parquet_views={'module_variability': module_variability},
                        extra_view_exclusions={'module_variability': ('dataset', [])})
        assert frame['n'][0] == 4

    def test_quotes_in_hidden_ids_are_literals(self, dataset, module_variability):
        frame, _ = _run(dataset, "SELECT count(*) AS n FROM module_variability",
                        extra_parquet_views={'module_variability': module_variability},
                        extra_view_exclusions={'module_variability': ('dataset', ["x') OR 1=1 --"])})
        assert frame['n'][0] == 4

    def test_bad_view_name_refused(self, dataset, module_variability):
        with pytest.raises(ValueError, match='Invalid extra view name'):
            _run(dataset, "SELECT 1", extra_parquet_views={'mv; DROP': module_variability})

    def test_bad_column_refused(self, dataset, module_variability):
        with pytest.raises(ValueError, match='Invalid exclusion column'):
            _run(dataset, "SELECT 1",
                 extra_parquet_views={'module_variability': module_variability},
                 extra_view_exclusions={'module_variability': ('dataset"; --', ['x'])})


# ── get_schema: published relations only ─────────────────────────────────────

def test_schema_lists_published_relations_only():
    conn = duckdb.connect()
    for table in ('__obscat', '__pending_cols', '__gene_bins', '__cells', '__genes'):
        conn.execute(f'CREATE TABLE "{table}" (cell_id INT)')
    conn.execute("CREATE VIEW obscat AS SELECT cell_id FROM __obscat")
    tables = {entry['table'] for entry in analytics.get_schema(conn)}
    assert tables == {'obscat', '__cells', '__genes'}


# ── The routes on the real ant-serve app ──────────────────────────────────────

def _post_sql(client, db_id, query, **extra):
    return client.post(f'/databases/{db_id}/query/sql', json={'query': query, **extra})


class TestQuerySqlRoute:

    def test_ok_shape_and_truncated_flag(self, serve_client):
        client, db_id = serve_client
        response = _post_sql(client, db_id, 'SELECT cell_name FROM obscat ORDER BY cell_name')
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) >= {'query', 'n_rows', 'n_columns', 'columns', 'dtypes', 'data', 'truncated'}
        assert body['truncated'] is False and body['n_rows'] == len(body['data']) > 0

    @pytest.mark.security
    def test_limit_applied_around_query(self, serve_client):
        client, db_id = serve_client
        response = _post_sql(client, db_id, 'SELECT * FROM range(1000) --', limit=7)
        assert response.status_code == 200, response.text
        assert response.json()['n_rows'] == 7 and response.json()['truncated'] is True

    def test_limit_capped_at_100000(self, serve_client):
        client, db_id = serve_client
        response = _post_sql(client, db_id, 'SELECT * FROM range(100005)', limit=10_000_000)
        assert response.status_code == 200, response.text
        assert response.json()['n_rows'] == 100_000

    def test_non_positive_limit(self, serve_client):
        client, db_id = serve_client
        # 0 means "not given" (the default cap); a negative limit is refused.
        assert _post_sql(client, db_id, 'SELECT 1', limit=0).status_code == 200
        assert _post_sql(client, db_id, 'SELECT 1', limit=-5).status_code == 400

    @pytest.mark.security
    @pytest.mark.parametrize('query', [
        "SELECT * FROM obscat, '/etc/hostname'",
        "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT 1'))",
        "SELECT * FROM __obscat",
        "SELECT * FROM duckdb_views()",
        "SELECT * FROM duckdb_views",
        "SELECT * FROM information_schema.tables",
        "SELECT 1; SELECT 2",
        "DROP TABLE __cells",
    ])
    def test_attacks_answer_400(self, serve_client, query):
        client, db_id = serve_client
        response = _post_sql(client, db_id, query)
        assert response.status_code == 400, response.text
        assert response.json()['detail'].startswith('Query refused')

    def test_execution_error_answers_400(self, serve_client):
        client, db_id = serve_client
        response = _post_sql(client, db_id, 'SELECT no_such_column FROM obscat')
        assert response.status_code == 400
        assert response.json()['detail'].startswith('Query error')

    def test_unknown_dataset_404(self, serve_client):
        client, _ = serve_client
        assert _post_sql(client, 'NOPE/NOPE', 'SELECT 1').status_code == 404

    @pytest.mark.security
    def test_timeout_answers_408(self, serve_client, monkeypatch):
        client, db_id = serve_client
        monkeypatch.setattr(analytics, 'QUERY_TIMEOUT', 1)
        response = _post_sql(client, db_id, 'SELECT COUNT(*) FROM range(100000000000)')
        assert response.status_code == 408, response.text

    def test_malformed_body_is_422(self, serve_client):
        client, db_id = serve_client
        assert client.post(f'/databases/{db_id}/query/sql', json={'query': 5}).status_code == 422
        assert client.post(f'/databases/{db_id}/query/sql', json={}).status_code == 422

    @pytest.mark.security
    def test_server_survives_a_burst_of_hostile_queries(self, serve_client):
        client, db_id = serve_client
        hostile = ['SELECT ' + '(' * 30000, "SELECT * FROM 'x'", '\x00', 'SELECT 1 ' + ' ' * 60000,
                   "SELECT * FROM read_text('/etc/passwd')", 'PRAGMA database_list']
        for query in hostile * 3:
            assert _post_sql(client, db_id, query).status_code == 400
        assert _post_sql(client, db_id, 'SELECT 1 AS x').json()['data'] == [{'x': 1}]


class TestAnalyticsQueryRoute:

    def test_module_variability_hides_hidden_datasets(self, serve_client, tmp_path, monkeypatch):
        client, db_id = serve_client
        import test_layer_path_portability as portability
        serve = portability._load_ant_serve()
        path = tmp_path / 'module_variability.parquet'
        pd.DataFrame({'dataset': ['PUB/A', 'INT/B'], 'module': ['m1', 'm2']}).to_parquet(path, index=False)
        monkeypatch.setattr(serve, '_module_variability_path', lambda: Path(path))
        monkeypatch.setattr(serve, '_hidden_db_ids', lambda: frozenset({'INT/B'}))
        response = client.post(f'/databases/{db_id}/analytics/query',
                               json={'sql': 'SELECT dataset FROM module_variability'})
        assert response.status_code == 200, response.text
        assert [row['dataset'] for row in response.json()['preview']] == ['PUB/A']

    @pytest.mark.security
    def test_analytics_refuses_the_same_attacks(self, serve_client):
        client, db_id = serve_client
        response = client.post(f'/databases/{db_id}/analytics/query',
                               json={'sql': "SELECT * FROM obscat, '/etc/hostname'"})
        assert response.status_code == 400


# ── Regression tests from the adversarial review (Fable, 2026-11-20) ─────────

@pytest.fixture
def masked_dataset(dataset):
    """obscat as build_obs_view_sql makes it: a masked value and an embedded crosswalk."""
    conn = duckdb.connect(str(dataset['db']))
    conn.execute("ALTER TABLE __obscat ADD COLUMN donor VARCHAR DEFAULT 'HIDDENVAL_DONOR7'")
    conn.execute(
        "CREATE OR REPLACE VIEW obscat AS SELECT c.cell_name, o.cell_type, "
        "CASE WHEN o.donor IN ('HIDDENVAL_DONOR7') THEN NULL ELSE o.donor END AS donor, xw.uid AS ant_donor_uid "
        "FROM __obscat o JOIN __cells c USING (cell_id) "
        "LEFT JOIN (VALUES ('d1', 'CROSSWALK_UID_A')) xw(alias, uid) ON xw.alias = o.donor")
    conn.close()
    return dataset


class TestReviewFindings:

    @pytest.mark.security
    @pytest.mark.parametrize('sql', [
        # F1: a CTE named like a system view whitelisted every reference to it.
        "WITH a AS (SELECT * FROM duckdb_views), duckdb_views AS (SELECT 1) SELECT sql FROM a",
        "SELECT sql FROM duckdb_views WHERE (WITH duckdb_views AS (SELECT 1 x) SELECT count(*) FROM duckdb_views) > 0",
        "SELECT * FROM (WITH pg_class AS (SELECT 1) SELECT 1) a, pg_class b",
        "WITH duckdb_tables AS (SELECT 1) SELECT * FROM duckdb_tables",
        "WITH sqlite_master AS (SELECT 1), a AS (SELECT * FROM sqlite_master) SELECT * FROM a",
    ])
    def test_cte_cannot_shadow_system_relations(self, masked_dataset, sql):
        with pytest.raises(ValueError) as excinfo:
            _run(masked_dataset, sql)
        for secret in ('HIDDENVAL', 'CROSSWALK', 'hidden_capdate', CANARY):
            assert secret not in str(excinfo.value)

    def test_cte_may_shadow_a_published_view_or_a_new_name(self, masked_dataset):
        frame, _ = _run(masked_dataset, "WITH obscat AS (SELECT 1 AS x) SELECT x FROM obscat")
        assert frame['x'][0] == 1
        frame, _ = _run(masked_dataset, "WITH mine AS (SELECT cell_name FROM obscat) SELECT count(*) AS n FROM mine")
        assert frame['n'][0] == 5

    @pytest.mark.security
    @pytest.mark.parametrize('sql', [
        # F2: json_serialize_plan binds SQL from a string (settings, view internals, min/max oracle).
        "SELECT json_serialize_plan('SELECT current_setting(''allowed_paths'')', optimize := true)",
        "SELECT json_serialize_plan('SELECT * FROM obscat')",
        "SELECT json_serialize_plan('SELECT 1 FROM __obscat WHERE hidden_capdate > ''C''', optimize := true)",
        # F3: built-in macros with a hidden FROM.
        "SELECT pg_get_viewdef(x) FROM range(0, 100000) t(x)",
        "SELECT pg_get_constraintdef(1)",
        "SELECT format_type(1, 1)",
        "SELECT get_block_size('db')",
        # F4: storage statistics below the mask.
        "SELECT stats(donor) FROM obscat LIMIT 1",
        # Engine state and side effects.
        "SELECT getvariable('x')",
        "SELECT sleep_ms(100000)",
        "SELECT pg_sleep(1000)",
        # Unknown names fail closed.
        "SELECT no_such_function_xyz(1)",
    ])
    def test_functions_that_leak_are_refused(self, masked_dataset, sql):
        with pytest.raises(ValueError, match='not allowed'):
            _run(masked_dataset, sql)

    def test_masked_value_not_served(self, masked_dataset):
        frame, _ = _run(masked_dataset, "SELECT DISTINCT donor FROM obscat")
        assert frame['donor'].isna().all()

    @pytest.mark.parametrize('name', [
        'upper', 'avg', 'count_star', 'row_number', 'list_value', '+', '~~', 'regexp_matches',
        'struct_pack', 'error', 'quantile_cont', 'median', 'string_agg',
    ])
    def test_common_functions_allowed(self, name):
        assert name in analytics.allowed_scalar_functions()

    @pytest.mark.parametrize('sql', [
        "SELECT coalesce(NULL, 1) AS x",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY range) AS x FROM range(3)",
        "SELECT CASE WHEN 1 IN (1, 2) THEN 1 END AS x",
        "SELECT CAST('1' AS INTEGER) AS x",
        "SELECT 1 AS x WHERE 'a' LIKE 'a%' AND 2 BETWEEN 1 AND 3",
        "SELECT list_transform([1], v -> v)[1] AS x",
        "SELECT count(*) FILTER (WHERE range > 0) AS x FROM range(2)",
        "SELECT median(range)::INT + 1 AS x FROM range(1)",
        "SELECT regexp_matches('ab', 'a')::INT AS x",
        "SELECT * FROM (SELECT 1 AS x) PIVOT (sum(x) FOR x IN (1))",
    ])
    def test_everyday_expressions_run(self, dataset, sql):
        frame, _ = _run(dataset, sql)
        assert len(frame) >= 1

    @pytest.mark.parametrize('name', sorted(analytics.BLOCKED_SCALAR_FUNCTIONS | {
        'pg_get_viewdef', 'pg_get_constraintdef', 'format_type', 'get_block_size'}))
    def test_blocked_functions_not_allowed(self, name):
        assert name not in analytics.allowed_scalar_functions()

    @pytest.mark.security
    def test_qualified_layer_view_cannot_read_another_datasets_sidecar(self, dataset):
        """F5: a duckdb copied from another dataset keeps that dataset's path in
        its persisted views; `db.layer__X` must not read it."""
        import shutil
        public_db = dataset['store'] / 'grp' / 'pub.duckdb'
        shutil.copy(dataset['db'], public_db)
        pd.DataFrame({'cell_id': [0], 'gene_id': [0], 'value': [0.0]}).to_parquet(
            dataset['store'] / 'grp' / 'pub.layer.X.by_cell.parquet', index=False)
        for sql in ("SELECT max(value) AS m FROM db.main.layer__X", "SELECT max(value) AS m FROM db.layer__X"):
            try:
                table, _ = execute_query(str(public_db), sql, row_limit=10)
            except (ValueError, RuntimeError):
                continue
            frame = table.to_pandas()
            assert frame['m'][0] != pytest.approx(6.0), 'read the other dataset\'s sidecar'

    @pytest.mark.security
    def test_excluded_rows_never_enter_the_instance(self, dataset):
        """F7 and the plan / stats routes: hidden ids must not surface in any answer or error."""
        path = dataset['store'] / 'module_variability.parquet'
        rows = 300_000
        pd.DataFrame({'dataset': ['HIDDEN_INTERNAL_DS' if i % 3 == 0 else 'PUB/A' for i in range(rows)],
                      'v': [99.5 if i % 3 == 0 else 1.0 for i in range(rows)]}).to_parquet(path, index=False)
        options = dict(extra_parquet_views={'module_variability': str(path)},
                       extra_view_exclusions={'module_variability': ('dataset', ['HIDDEN_INTERNAL_DS'])})
        frame, _ = _run(dataset, "SELECT max(v) AS m, count(*) AS n, min(dataset) AS d FROM module_variability",
                        **options)
        assert frame['m'][0] == 1.0 and frame['n'][0] == rows - rows // 3
        assert frame['d'][0] == 'PUB/A'
        for sql in ("SELECT count(*) FROM module_variability WHERE dataset::INT IS NULL",
                    "SELECT count(*) FROM module_variability WHERE dataset::VARCHAR[] IS NULL",
                    f"SELECT * FROM read_parquet('{path}')",
                    "SELECT sql FROM duckdb_views()"):
            try:
                _run(dataset, sql, **options)
            except (ValueError, RuntimeError) as exc:
                assert 'HIDDEN_INTERNAL_DS' not in str(exc)


class TestCodeReviewFindings:

    @pytest.mark.security
    def test_timeout_kills_the_worker(self, dataset, monkeypatch):
        """No query outlives its time limit: the worker process is killed."""
        import os
        monkeypatch.setattr(analytics, 'QUERY_TIMEOUT', 2)
        before = set(os.listdir('/proc'))
        with pytest.raises(TimeoutError):
            _run(dataset, "SELECT COUNT(*) FROM range(100000000000)")
        time.sleep(0.5)
        leftovers = []
        for pid in set(os.listdir('/proc')) - before:
            try:
                cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
            except OSError:
                continue
            if b'analytics_worker.py' in cmdline and Path(f'/proc/{pid}/stat').read_text().split()[2] != 'Z':
                leftovers.append(pid)
        # At most the one new spare worker, which is idle (sleeping on stdin), may remain.
        assert len(leftovers) <= analytics.SPARE_WORKERS

    @pytest.mark.parametrize('name', ['columns', 'tables', 'views', 'schemata', 't', 'mine'])
    def test_cte_names_outside_the_search_path_are_fine(self, dataset, name):
        frame, _ = _run(dataset, f"WITH {name} AS (SELECT 1 AS a) SELECT a FROM {name}")
        assert frame['a'][0] == 1

    def test_pivot_statement_gets_a_useful_message(self):
        with pytest.raises(ValueError, match='PIVOT'):
            validate_sql("PIVOT obscat ON cell_type USING count(*)")

    def test_standard_pivot_runs(self, dataset):
        frame, _ = _run(dataset, "FROM (SELECT cell_type FROM obscat) PIVOT (count(*) FOR cell_type IN ('mg'))")
        assert frame['mg'][0] == 5

    @pytest.mark.parametrize('sql', [
        "SELECT row_number() OVER () AS r FROM obscat",
        "SELECT lag(cell_name) OVER (ORDER BY cell_name) AS r FROM obscat",
        "SELECT rank() OVER (ORDER BY cell_name) AS r, ntile(2) OVER () AS q FROM obscat",
    ])
    def test_window_functions_run(self, dataset, sql):
        frame, _ = _run(dataset, sql)
        assert len(frame) == 5

    def test_window_functions_allowed_even_if_the_catalog_lacks_them(self):
        assert analytics._WINDOW_FUNCTIONS <= analytics.allowed_scalar_functions()

    @pytest.mark.parametrize('sql,hint', [
        ("SELECT * FROM information_schema.columns", 'analytics/schema'),
        ("SELECT * FROM no_such_view", 'analytics/schema'),
    ])
    def test_refusals_point_to_the_schema_route(self, dataset, sql, hint):
        with pytest.raises(ValueError, match=hint):
            _run(dataset, sql)


def test_route_dependencies_present():
    """The route tests skip when ant-serve cannot load; make that skip visible as a failure here."""
    for module in ('uvicorn', 'fastapi', 'httpx', 'matplotlib', 'seaborn'):
        __import__(module)


# ── Server-killing queries (Fable DoS review, 2026-11-20) ────────────────────

class TestServerSurvives:
    """Each of these ran in ant-serve's own process before the worker processes:
    a segfault, unbounded memory, uninterruptible CPU, a quadratic lexer."""

    @pytest.mark.security
    def test_segfault_ends_with_the_worker(self, dataset):
        with pytest.raises(RuntimeError, match='worker'):
            _run(dataset, "SELECT json_structure(repeat('[', 300000) || repeat(']', 300000))")
        frame, _ = _run(dataset, "SELECT 1 AS x")
        assert frame['x'][0] == 1

    @pytest.mark.security
    @pytest.mark.parametrize('sql', [
        "SELECT length(repeat('x', 3000000000)) AS n",
        "SELECT length(lpad('x', 2000000000, 'y')) AS n",
        "SELECT len(range(400000000)) AS n",
        "SELECT damerau_levenshtein(repeat('ab', 15000), repeat('ba', 15000)) AS n",
    ])
    def test_memory_outside_duckdbs_limit_is_capped(self, dataset, monkeypatch, sql):
        monkeypatch.setattr(analytics, 'WORKER_MEMORY_BUDGET', 1024 * 1024 * 1024)
        monkeypatch.setattr(analytics, 'QUERY_TIMEOUT', 60)
        with pytest.raises((RuntimeError, TimeoutError)):
            _run(dataset, sql)

    @pytest.mark.security
    def test_uninterruptible_function_is_killed_on_time(self, dataset, monkeypatch):
        monkeypatch.setattr(analytics, 'QUERY_TIMEOUT', 2)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            _run(dataset, "SELECT levenshtein(repeat('a', 100000), repeat('b', 100000))")
        assert time.monotonic() - started < 8

    @pytest.mark.security
    @pytest.mark.parametrize('sql', [
        'SELECT 1 ' + '/**/' * 12490,
        '/*' * 24990,
        '*/' * 24990,
        'SELECT 1 ' + '+' * 300 + '1',
    ])
    def test_operator_runs_refused_fast(self, sql):
        started = time.monotonic()
        with pytest.raises(ValueError, match='operator characters'):
            validate_sql(sql)
        assert time.monotonic() - started < 0.5

    def test_banner_comment_still_allowed(self):
        validate_sql('/* ' + '*' * 200 + ' */ SELECT 1')

    def test_concurrency_cap(self, dataset, monkeypatch):
        import threading
        monkeypatch.setattr(analytics, '_query_slots', threading.BoundedSemaphore(1))
        analytics._query_slots.acquire()
        try:
            with pytest.raises(analytics.QueryBusy):
                _run(dataset, "SELECT 1")
        finally:
            analytics._query_slots.release()
        frame, _ = _run(dataset, "SELECT 1 AS x")
        assert frame['x'][0] == 1

    def test_slot_released_after_every_outcome(self, dataset, monkeypatch):
        import threading
        monkeypatch.setattr(analytics, '_query_slots', threading.BoundedSemaphore(1))
        monkeypatch.setattr(analytics, 'QUERY_TIMEOUT', 2)
        for sql in ("SELECT no_such_column FROM obscat", "SELECT COUNT(*) FROM range(100000000000)",
                    "SELECT 1 AS x"):
            try:
                _run(dataset, sql)
            except (RuntimeError, TimeoutError):
                pass
        assert analytics._query_slots.acquire(blocking=False)
        analytics._query_slots.release()

    def test_result_byte_cap(self, dataset, monkeypatch):
        monkeypatch.setattr(analytics, 'MAX_RESULT_BYTES', 1_000_000)
        with pytest.raises(ValueError, match='larger than'):
            _run(dataset, "SELECT repeat('x', 10000) AS s FROM range(1000)")

    def test_cell_cap_truncates(self, dataset):
        frame, truncated = _run(dataset, "SELECT range a, range b FROM range(100)", max_cells=50)
        assert len(frame) == 25 and truncated is True

    def test_spill_and_result_files_removed(self, dataset):
        _run(dataset, "SELECT 1 AS x")
        tmp = analytics._get_tmp_dir()
        assert not list(tmp.glob('_result_*')) and not list(tmp.glob('_spill_*'))

    def test_worker_gets_no_credentials_from_the_environment(self, monkeypatch):
        monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', CANARY)
        monkeypatch.setenv('OPENROUTER_API_KEY', CANARY)
        env = analytics._worker_env()
        assert CANARY not in ''.join(env.values())

    @pytest.mark.parametrize('sql', [
        "SELECT range(5) AS x",
        "SELECT [[1, 2], [3]] AS x",
        "SELECT [NULL, 1] AS x",
        "SELECT '1010'::BIT AS x",
        "SELECT {'a': 'x'::BLOB} AS x",
        "SELECT 'x'::BLOB AS x",
        "SELECT 1.5::DECIMAL(10, 2) AS x",
        "SELECT DATE '2026-01-01' AS x, INTERVAL 1 DAY AS y",
        "SELECT MAP {'a': 1} AS x",
        "SELECT [1.0, 'nan'::DOUBLE, 'inf'::DOUBLE] AS x",
        "SELECT 'nan'::DOUBLE AS x",
        "SELECT 12345678901234567890123::HUGEINT AS x",
        "SELECT uuid() AS x",
    ])
    def test_odd_types_encode_on_the_route(self, serve_client, sql):
        client, db_id = serve_client
        response = _post_sql(client, db_id, sql)
        assert response.status_code == 200, response.text
        assert len(response.json()['data']) == 1

    def test_route_answers_429_when_busy(self, serve_client, monkeypatch):
        import threading
        client, db_id = serve_client
        monkeypatch.setattr(analytics, '_query_slots', threading.BoundedSemaphore(1))
        analytics._query_slots.acquire()
        try:
            response = _post_sql(client, db_id, 'SELECT 1')
            assert response.status_code == 429 and response.headers.get('retry-after') == '5'
            response = client.post(f'/databases/{db_id}/analytics/query', json={'sql': 'SELECT 1'})
            assert response.status_code == 429
        finally:
            analytics._query_slots.release()

    def test_route_crash_is_400_and_server_keeps_answering(self, serve_client):
        client, db_id = serve_client
        response = _post_sql(client, db_id, "SELECT json_structure(repeat('[', 300000) || repeat(']', 300000))")
        assert response.status_code == 400 and 'worker' in response.json()['detail']
        assert _post_sql(client, db_id, 'SELECT 1 AS x').json()['data'] == [{'x': 1}]


# ── Round-2 review (Fable, 2026-11-20): the parent only passes bytes on ──────

def _fake_worker(script: str):
    """A stand-in worker process running script (it must print the ready line first)."""
    import subprocess
    import sys as _sys
    return subprocess.Popen([_sys.executable, '-c', script], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)


class TestRoundTwo:

    @pytest.mark.security
    def test_json_body_cap(self, dataset, monkeypatch):
        """Nested results expand far beyond their Arrow size; the JSON answer itself is capped."""
        monkeypatch.setattr(analytics, 'MAX_BODY_BYTES', 1_000_000)
        with pytest.raises(ValueError, match='larger than 1 MB of JSON'):
            analytics.query_json(str(dataset['db']),
                                 "SELECT list_resize([true], 10000, true) AS x FROM range(1000)", row_limit=1000)

    def test_query_json_shape(self, dataset):
        import json
        body = json.loads(analytics.query_json(str(dataset['db']),
                                               "SELECT cell_name, [1, 2] AS l FROM obscat ORDER BY 1",
                                               row_limit=3))
        assert body['columns'] == ['cell_name', 'l'] and body['n_rows'] == 3 and body['truncated'] is True
        assert body['data'][0] == {'cell_name': 'c0', 'l': [1, 2]} and set(body['dtypes']) == {'cell_name', 'l'}

    def test_query_json_empty_result(self, dataset):
        import json
        body = json.loads(analytics.query_json(str(dataset['db']), "SELECT 1 AS x WHERE false", row_limit=3))
        assert body['data'] == [] and body['n_rows'] == 0 and body['truncated'] is False

    def test_analytics_answer_encoded_by_worker(self, dataset):
        result = analytics.run_query(str(dataset['db']),
                                     "SELECT [1.0, 'nan'::DOUBLE] AS l, 'x'::BLOB AS b, {'a': 1} AS s")
        assert result['row_count'] == 1 and result['preview'][0]['l'] == [1.0, None]
        assert (analytics._get_tmp_dir() / f"{result['session_id']}.parquet").exists()

    @pytest.mark.security
    def test_wide_preview_is_cut(self, dataset):
        columns = ', '.join(f"repeat('x', 100) AS c{i}" for i in range(400))
        result = analytics.run_query(str(dataset['db']), f"SELECT {columns} FROM range(500)")
        assert result['row_count'] == 500 and result['full_table'] is True
        assert result['preview_complete'] is False and len(result['preview']) < 500

    def test_failed_analytics_query_leaves_no_session_file(self, dataset):
        before = set(analytics._get_tmp_dir().glob('*.parquet'))
        with pytest.raises(RuntimeError):
            analytics.run_query(str(dataset['db']), "SELECT no_such_column FROM obscat")
        assert set(analytics._get_tmp_dir().glob('*.parquet')) == before
        assert not list(analytics._get_tmp_dir().glob('_result_*'))

    def test_memory_budget_default(self, monkeypatch):
        assert analytics.WORKER_MEMORY_BUDGET >= 6 * 1024 ** 3
        assert analytics._bytes_of('2GB') == 2 * 1024 ** 3
        assert analytics._bytes_of('512MB') == 512 * 1024 ** 2
        assert analytics._bytes_of('1.5GiB') == int(1.5 * 1024 ** 3)
        assert analytics._bytes_of('lots') == 0

    def test_large_aggregation_completes(self, dataset, monkeypatch):
        """3 GB of address space refused aggregations DuckDB finishes by spilling (F3)."""
        monkeypatch.setattr(analytics, 'QUERY_TIMEOUT', 110)
        frame, _ = _run(dataset, "SELECT count(DISTINCT hash(range)) AS n FROM range(30000000)")
        assert frame['n'][0] > 29_000_000

    @pytest.mark.security
    def test_cpu_limit_kill_is_a_timeout(self, dataset, monkeypatch):
        """SIGXCPU (CPU time of all threads) is reported as the time limit, not a crash (F4)."""
        script = ("import os, signal, sys; print('{\"ready\": true}', flush=True); sys.stdin.readline(); "
                  "os.kill(os.getpid(), signal.SIGXCPU)")
        monkeypatch.setattr(analytics, '_take_worker', lambda: _fake_worker(script))
        with pytest.raises(TimeoutError, match='CPU time'):
            _run(dataset, "SELECT 1")

    def test_cpu_seconds_count_threads(self, dataset, monkeypatch):
        import json
        seen = {}
        script = ("import json, sys; print('{\"ready\": true}', flush=True); "
                  "job = json.loads(sys.stdin.readline()); "
                  "print(json.dumps({'ok': False, 'kind': 'error', 'message': str(job['cpu_seconds'])}), flush=True)")
        monkeypatch.setenv('ANTHIVE_DUCKDB_THREADS', '4')
        monkeypatch.setattr(analytics, '_take_worker', lambda: _fake_worker(script))
        with pytest.raises(RuntimeError) as excinfo:
            _run(dataset, "SELECT 1", timeout=10)
        assert str(excinfo.value) == str(10 * 4 + 30)

    def test_worker_gone_before_the_job_is_a_query_error(self, dataset, monkeypatch):
        script = "print('{\"ready\": true}', flush=True)"
        monkeypatch.setattr(analytics, '_take_worker', lambda: _fake_worker(script))
        with pytest.raises(RuntimeError):
            _run(dataset, "SELECT 1")

    def test_oversized_status_line_is_not_trusted(self, dataset, monkeypatch):
        script = ("import sys; print('{\"ready\": true}', flush=True); sys.stdin.readline(); "
                  "sys.stdout.write('x' * 200000); sys.stdout.flush()")
        monkeypatch.setattr(analytics, '_take_worker', lambda: _fake_worker(script))
        with pytest.raises((RuntimeError, ValueError)):
            _run(dataset, "SELECT 1")

    @pytest.mark.security
    def test_per_caller_limit(self, dataset, monkeypatch):
        monkeypatch.setitem(analytics._caller_running, 'user:greedy', analytics.MAX_QUERIES_PER_CALLER)
        with pytest.raises(analytics.QueryBusy, match='already have'):
            _run(dataset, "SELECT 1", caller='user:greedy')
        frame, _ = _run(dataset, "SELECT 1 AS x", caller='user:other')
        assert frame['x'][0] == 1
        assert analytics._caller_running['user:greedy'] == analytics.MAX_QUERIES_PER_CALLER
        assert 'user:other' not in analytics._caller_running

    @pytest.mark.security
    def test_no_slot_leak_when_tmp_fails(self, dataset, monkeypatch):
        import threading
        monkeypatch.setattr(analytics, '_query_slots', threading.BoundedSemaphore(2))
        real_tmp = analytics._get_tmp_dir

        def broken():
            raise NotADirectoryError('tmp is broken')
        monkeypatch.setattr(analytics, '_get_tmp_dir', broken)
        for _ in range(4):
            with pytest.raises(OSError):
                _run(dataset, "SELECT 1")
        monkeypatch.setattr(analytics, '_get_tmp_dir', real_tmp)
        frame, _ = _run(dataset, "SELECT 1 AS x")
        assert frame['x'][0] == 1 and not analytics._caller_running

    @pytest.mark.security
    def test_session_budget_evicts_oldest(self, dataset, monkeypatch):
        import os
        tmp = analytics._get_tmp_dir()
        old = [tmp / f"{i:032x}.parquet" for i in range(3)]
        for age, path in enumerate(old):
            path.write_bytes(b'x' * 400_000)
            os.utime(path, (time.time() - 1000 + age, time.time() - 1000 + age))
        monkeypatch.setattr(analytics, 'SESSION_BUDGET_BYTES', 900_000)
        monkeypatch.setattr(analytics, '_last_sweep', 0.0)
        analytics.run_query(str(dataset['db']), "SELECT 1 AS x")
        assert not old[0].exists() and old[2].exists()

    def test_stale_worker_leftovers_swept(self, dataset, monkeypatch):
        import os
        monkeypatch.setattr(analytics, '_last_sweep', 0.0)
        tmp = analytics._get_tmp_dir()
        stale = [tmp / '_result_dead', tmp / '_spill_dead', tmp / '_viz_dead']
        stale[0].write_text('x')
        stale[1].mkdir()
        stale[2].mkdir()
        fresh = tmp / '_result_live'
        fresh.write_text('x')
        for path in stale:
            os.utime(path, (time.time() - 10_000, time.time() - 10_000))
        analytics.run_query(str(dataset['db']), "SELECT 1 AS x")
        assert not any(path.exists() for path in stale) and fresh.exists()

    @pytest.mark.security
    def test_error_messages_are_bounded_and_pathless(self, dataset):
        with pytest.raises(RuntimeError) as excinfo:
            _run(dataset, "SELECT error(repeat('x', 5000000))")
        assert len(str(excinfo.value)) < 5000
        with pytest.raises((RuntimeError, ValueError)) as excinfo:
            _run(dataset, "SELECT * FROM db.main.no_such_view")
        assert str(dataset['store']) not in str(excinfo.value)

    def test_worker_sanitize(self):
        import analytics_worker
        job = {'db_url': '/srv/store/grp/ds.duckdb', 'result_path': '/srv/tmp/_result_x'}
        message = analytics_worker._sanitize(
            'Permission Error: /srv/store/grp/ds.layer.X.parquet (e.g., SET memory_limit=...)' + 'y' * 5000, job)
        assert '/srv/store/grp' not in message and 'SET memory_limit' not in message and len(message) < 4100

    def test_route_body_cap_answers_400(self, serve_client, monkeypatch):
        client, db_id = serve_client
        monkeypatch.setattr(analytics, 'MAX_BODY_BYTES', 1_000_000)
        response = _post_sql(client, db_id, "SELECT list_resize([true], 10000, true) AS x FROM range(1000)")
        assert response.status_code == 400 and 'larger than' in response.json()['detail']

    def test_route_analytics_odd_types_answer_200(self, serve_client):
        client, db_id = serve_client
        response = client.post(f'/databases/{db_id}/analytics/query',
                               json={'sql': "SELECT [1, 2] AS l, 'x'::BLOB AS b, '1010'::BIT AS t"})
        assert response.status_code == 200, response.text
        assert response.json()['preview'][0]['l'] == [1, 2]

    def test_route_per_caller_limit(self, serve_client, monkeypatch):
        client, db_id = serve_client  # signed in as 'tester'
        monkeypatch.setitem(analytics._caller_running, 'user:tester', analytics.MAX_QUERIES_PER_CALLER)
        assert _post_sql(client, db_id, 'SELECT 1').status_code == 429


class TestRoundThree:
    """Third review (2026-11-20): streaming, sweeps, callers, waiters, sessions, types, spawner, schema_columns."""

    @pytest.mark.security
    def test_answer_is_streamed_and_holds_the_caller(self, dataset):
        import json
        answer = analytics.query_json_answer(str(dataset['db']), "SELECT * FROM range(20000)",
                                             row_limit=100_000, caller='user:stream')
        assert answer.path.exists() and answer.path.name.startswith('_send_')
        assert analytics._caller_running['user:stream'] == 1
        chunks = list(answer.chunks())
        assert len(chunks) > 1 and all(len(chunk) <= answer.CHUNK for chunk in chunks)
        body = json.loads(b''.join(chunks))
        assert body['n_rows'] == 20000 and sum(map(len, chunks)) == answer.size
        assert not answer.path.exists() and 'user:stream' not in analytics._caller_running

    @pytest.mark.security
    def test_unread_answer_is_released_by_the_timer(self, dataset, monkeypatch):
        monkeypatch.setattr(analytics, 'STREAM_RELEASE_SECONDS', 0.3)
        answer = analytics.query_json_answer(str(dataset['db']), "SELECT 1 AS x", row_limit=10,
                                             caller='user:gone')
        assert analytics._caller_running['user:gone'] == 1
        deadline = time.time() + 5
        while 'user:gone' in analytics._caller_running and time.time() < deadline:
            time.sleep(0.05)
        assert 'user:gone' not in analytics._caller_running and not answer.path.exists()
        answer.close()  # idempotent: no double release
        assert 'user:gone' not in analytics._caller_running

    def test_failed_query_releases_the_caller(self, dataset):
        with pytest.raises(RuntimeError):
            analytics.query_json_answer(str(dataset['db']), "SELECT no_such FROM obscat", row_limit=10,
                                        caller='user:fails')
        assert 'user:fails' not in analytics._caller_running

    def test_route_streams_with_length(self, serve_client):
        client, db_id = serve_client
        response = _post_sql(client, db_id, 'SELECT * FROM range(5000)')
        assert response.status_code == 200, response.text
        assert int(response.headers['content-length']) == len(response.content)
        assert response.json()['n_rows'] == 5000
        assert not analytics._caller_running

    @pytest.mark.security
    def test_sweep_runs_at_most_once_per_interval(self, dataset, monkeypatch):
        import os
        monkeypatch.setattr(analytics, '_last_sweep', 0.0)
        analytics.run_query(str(dataset['db']), "SELECT 1 AS x")
        stale = analytics._get_tmp_dir() / '_result_stale'
        stale.write_text('x')
        os.utime(stale, (time.time() - 10_000, time.time() - 10_000))
        analytics.run_query(str(dataset['db']), "SELECT 1 AS x")
        assert stale.exists()  # swept again only after SWEEP_INTERVAL_SECONDS
        monkeypatch.setattr(analytics, '_last_sweep', 0.0)
        analytics.query_json(str(dataset['db']), "SELECT 1 AS x", row_limit=1)  # /query/sql sweeps too
        assert not stale.exists()

    def test_sweep_entry_cap(self, tmp_path, monkeypatch):
        import os
        monkeypatch.setattr(analytics, 'SWEEP_MAX_ENTRIES', 3)
        for index in range(10):
            path = tmp_path / f'_result_{index}'
            path.write_text('x')
            os.utime(path, (time.time() - 10_000, time.time() - 10_000))
        analytics._sweep_tmp(tmp_path)
        assert len(list(tmp_path.iterdir())) == 7

    def test_sweep_removes_old_sessions_and_figures(self, tmp_path, monkeypatch):
        import os
        old_session = tmp_path / f"{'a' * 32}.parquet"
        old_figure = tmp_path / 'figure.png'
        new_session = tmp_path / f"{'b' * 32}.parquet"
        for path in (old_session, old_figure, new_session):
            path.write_bytes(b'x')
        for path in (old_session, old_figure):
            os.utime(path, (time.time() - analytics.SESSION_MAX_AGE - 10,) * 2)
        analytics._sweep_tmp(tmp_path)
        assert not old_session.exists() and not old_figure.exists() and new_session.exists()

    @pytest.mark.security
    def test_anonymous_callers_keyed_by_address(self):
        from types import SimpleNamespace
        from test_layer_path_portability import _load_ant_serve
        serve = _load_ant_serve()

        def request(headers, host='127.0.0.1'):
            return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))
        assert serve._sql_caller(request({'Remote-User': 'ann'})) == 'user:ann'
        assert serve._sql_caller(request({}, host='10.0.0.9')) == 'anonymous:10.0.0.9'
        # Behind a proxy the last hop is the one the proxy appended; earlier hops are the client's claim.
        assert serve._sql_caller(request({'X-Forwarded-For': '1.2.3.4, 198.51.100.7'})) == 'anonymous:198.51.100.7'
        assert serve._sql_caller(request({'X-Forwarded-For': 'x' * 500})) == 'anonymous:' + 'x' * 64
        # A client reaching ant-serve directly cannot choose its key.
        assert serve._sql_caller(request({'X-Forwarded-For': '9.9.9.9'}, host='8.8.4.4')) == 'anonymous:8.8.4.4'
        assert serve._sql_caller(SimpleNamespace(headers={}, client=None)) == 'anonymous'

    @pytest.mark.security
    def test_waiters_are_capped(self, dataset, monkeypatch):
        import threading
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        monkeypatch.setattr(analytics, '_query_slots', slots)
        monkeypatch.setattr(analytics, 'MAX_SLOT_WAITERS', 0)
        started = time.monotonic()
        with pytest.raises(analytics.QueryBusy, match='slots are in use'):
            _run(dataset, "SELECT 1", caller='user:waiter')
        assert time.monotonic() - started < 1.0  # refused without waiting
        assert 'user:waiter' not in analytics._caller_running and analytics._waiters == 0
        monkeypatch.setattr(analytics, 'MAX_SLOT_WAITERS', 4)
        monkeypatch.setattr(analytics, 'SLOT_WAIT_SECONDS', 0.2)
        with pytest.raises(analytics.QueryBusy):
            _run(dataset, "SELECT 1", caller='user:waiter')
        assert analytics._waiters == 0
        slots.release()

    @pytest.mark.security
    def test_same_session_twice_is_refused(self, dataset, monkeypatch):
        session = 'c' * 32
        monkeypatch.setattr(analytics, '_sessions_running', {session})
        with pytest.raises(analytics.QueryBusy, match='session_id'):
            analytics.run_query(str(dataset['db']), "SELECT 1 AS x", session_id=session)
        monkeypatch.setattr(analytics, '_sessions_running', set())
        assert analytics.run_query(str(dataset['db']), "SELECT 1 AS x", session_id=session)['row_count'] == 1
        assert session not in analytics._sessions_running

    @pytest.mark.parametrize('sql, expected', [
        ("SELECT '9999-12-31'::DATE + 10 AS v", '10000-01-10'),
        ("SELECT '0001-01-01'::DATE - 400 AS v", '-0001-11-28'),
        ("SELECT 170141183460469231731687303715884105727::HUGEINT AS v", 170141183460469231731687303715884105727),
        ("SELECT 3::UHUGEINT AS v", 3),
        ("SELECT 12.30::DECIMAL(10, 2) AS v", 12.3),
    ])
    def test_awkward_types_in_json(self, dataset, sql, expected):
        import json
        body = json.loads(analytics.query_json(str(dataset['db']), sql, row_limit=5))
        assert body['data'][0]['v'] == expected

    @pytest.mark.parametrize('sql', [
        "SELECT '9999-12-31'::DATE + 10 AS v",
        "SELECT INTERVAL 3 MONTH AS v",
        "SELECT union_value(a := 1) AS v",
        "SELECT [INTERVAL 1 DAY] AS v",
    ])
    def test_awkward_types_in_session_files(self, dataset, sql):
        import pyarrow.parquet as pq
        result = analytics.run_query(str(dataset['db']), sql)
        assert result['row_count'] == 1 and len(result['preview']) == 1
        assert pq.read_table(analytics._get_tmp_dir() / f"{result['session_id']}.parquet").num_rows == 1

    @pytest.mark.security
    def test_one_huge_preview_row_is_left_out(self, dataset):
        result = analytics.run_query(str(dataset['db']), "SELECT repeat('x', 3000000) AS v, 1 AS n")
        assert result['row_count'] == 1 and result['preview'] == [] and result['preview_complete'] is False

    @pytest.mark.security
    def test_error_text_hides_extra_view_paths(self, tmp_path):
        import analytics_worker
        fleet = tmp_path / 'fleet' / 'module_variability.parquet'
        job = {'db_url': str(tmp_path / 'store' / 'grp' / 'ds.duckdb'),
               'result_path': str(tmp_path / 'analytics' / '_result_x'),
               'extra_parquet_views': {'module_variability': str(fleet)}}
        message = analytics_worker._sanitize(f"IO Error: cannot read {fleet} or {fleet.parent}/other", job)
        assert str(tmp_path) not in message and '<module_variability>' in message

    @pytest.mark.security
    def test_spare_survives_the_thread_that_asked_for_it(self):
        import threading
        started = []
        thread = threading.Thread(target=lambda: started.append(analytics._start_worker()))
        thread.start()
        thread.join()
        proc = started[0]
        try:
            assert analytics._read_line(proc, 60) is not None  # ready line
            time.sleep(0.5)
            assert proc.poll() is None  # PR_SET_PDEATHSIG is tied to the spawner thread, which lives on
        finally:
            analytics._kill(proc)

    def test_schema_columns_lists_published_columns_only(self, dataset):
        frame, _ = _run(dataset, "SELECT table_name, column_name, data_type FROM schema_columns")
        tables = set(frame['table_name'])
        assert {'obscat', 'layer__X'} <= tables or {'obscat', 'layer__x'} <= {t.lower() for t in tables}
        assert '__obscat' not in tables and '__pending_cols' not in tables and 'schema_columns' not in tables
        assert set(frame[frame['table_name'] == 'obscat']['column_name']) == {'cell_name', 'cell_type'}
        assert CANARY not in frame.to_string() and 'hidden_capdate' not in set(frame['column_name'])

    def test_refusals_point_at_schema_columns(self, dataset):
        for sql in ("SELECT column_name FROM information_schema.columns", "DESCRIBE obscat"):
            with pytest.raises(ValueError, match='schema_columns'):
                validate_sql(sql)

    @pytest.mark.security
    def test_session_overwrite_only_by_its_owner(self, dataset):
        """Issue #47: a caller-supplied session_id cannot replace another caller's session file."""
        first = analytics.run_query(str(dataset['db']), "SELECT 1 AS x", caller='user:alice')
        session = first['session_id']
        owner_file = analytics._get_tmp_dir() / f"{session}.owner"
        assert owner_file.exists() and 'alice' not in owner_file.read_text()
        with pytest.raises(ValueError, match='belongs to another'):
            analytics.run_query(str(dataset['db']), "SELECT 2 AS x", session_id=session, caller='user:mallory')
        again = analytics.run_query(str(dataset['db']), "SELECT 3 AS x", session_id=session, caller='user:alice')
        assert again['preview'] == [{'x': 3}]
        # A session written elsewhere (module_score) has no owner file: nobody may overwrite it.
        foreign = 'd' * 32
        (analytics._get_tmp_dir() / f"{foreign}.parquet").write_bytes(b'x')
        with pytest.raises(ValueError, match='belongs to another'):
            analytics.run_query(str(dataset['db']), "SELECT 1 AS x", session_id=foreign, caller='user:alice')
