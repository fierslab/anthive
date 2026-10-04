"""Tests for the analytics security sandbox (lib/analytics.py).

These are the highest-priority tests in the suite: validate_sql and
validate_python are the security boundary that prevents arbitrary file
access, data exfiltration, and code execution via the analytics endpoints.
"""

import duckdb
import pytest

import analytics

from analytics import (
    _BLOCKED_BUILTINS,
    _BLOCKED_DATA_LOADERS,
    _BLOCKED_MODULES,
    _build_viz_script,
    validate_python,
    validate_sql,
)


# ── SQL validation: allowed queries ───────────────────────────────────────────

class TestValidateSqlAllowed:

    def test_simple_select(self):
        validate_sql("SELECT * FROM cells")

    def test_select_with_where(self):
        validate_sql("SELECT cell_name FROM cells WHERE x > 0")

    def test_with_cte(self):
        validate_sql("WITH t AS (SELECT 1 AS x) SELECT * FROM t")

    def test_multiple_ctes(self):
        validate_sql(
            "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b"
        )

    def test_select_with_aggregation(self):
        validate_sql("SELECT gene_name, AVG(value) FROM layer__X GROUP BY 1")

    def test_select_with_subquery(self):
        validate_sql("SELECT * FROM (SELECT 1 AS x) sub")

    def test_select_with_join(self):
        validate_sql("SELECT * FROM cells c JOIN obscat o USING (cell_name)")

    def test_select_with_window_function(self):
        validate_sql(
            "SELECT cell_name, ROW_NUMBER() OVER (ORDER BY x) FROM cells"
        )

    def test_select_with_using_sample(self):
        validate_sql(
            "SELECT * FROM cells USING SAMPLE 1000 ROWS (reservoir)"
        )

    def test_comment_hiding_write_statement(self):
        # The DROP is inside a comment so it should be stripped and pass
        validate_sql("SELECT 1 -- DROP TABLE cells")

    def test_block_comment_hiding_write(self):
        validate_sql("SELECT /* DROP TABLE */ 1")


# ── SQL validation: blocked statements ────────────────────────────────────────

class TestValidateSqlBlocked:

    @pytest.mark.security
    @pytest.mark.parametrize("keyword", [
        "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
        "TRUNCATE", "REPLACE", "MERGE", "ATTACH", "DETACH",
        "COPY", "EXPORT", "IMPORT", "PRAGMA", "CALL",
    ])
    def test_ddl_dml_blocked(self, keyword):
        # The security contract is rejection (a raised ValueError), not a
        # specific message.  Depending on the keyword, validate_sql rejects
        # either via the statement-type gate ("…are permitted.") or as an
        # unparseable statement ("Could not parse SQL: …") — both are secure.
        with pytest.raises(ValueError):
            validate_sql(f"{keyword} INTO cells VALUES (1)")

    @pytest.mark.security
    @pytest.mark.parametrize("keyword", [
        "insert", "update", "delete", "drop", "create",
    ])
    def test_case_insensitive_blocking(self, keyword):
        with pytest.raises(ValueError):
            validate_sql(f"{keyword} into cells values (1)")

    @pytest.mark.security
    @pytest.mark.parametrize("func", [
        "read_csv", "read_parquet", "read_json", "read_ndjson", "read_text",
        "read_arrow", "scan_csv", "scan_parquet", "scan_arrow",
        "scan_ndjson", "scan_json", "glob", "httpfs_get", "httpfs_post",
        "load_spatial", "parquet_metadata", "csv_scan", "parquet_scan",
    ])
    def test_file_reading_functions_blocked(self, func):
        with pytest.raises(ValueError, match="not allowed"):
            validate_sql(f"SELECT * FROM {func}('some_file')")

    @pytest.mark.security
    def test_write_after_comment_newline(self):
        # DROP on a new line after a line comment is a second statement and
        # must still be caught by the statement-type gate.
        with pytest.raises(ValueError):
            validate_sql("SELECT 1;\n-- harmless\nDROP TABLE cells")

    @pytest.mark.security
    def test_keyword_in_string_literal_allowed(self):
        """A SQL keyword appearing inside a string literal is not a statement.
        The parser/tokenizer-based validator (unlike the older regex scanner)
        distinguishes them correctly, so this legitimate SELECT is allowed —
        no false-positive block."""
        validate_sql("SELECT 'INSERT' AS x")  # must not raise

    @pytest.mark.security
    def test_pragma_blocked(self):
        # DuckDB parses read-only pragmas (e.g. PRAGMA database_list) as
        # StatementType.SELECT, so they slip the statement-type gate; the
        # token-level PRAGMA check is what rejects them.
        with pytest.raises(ValueError, match="PRAGMA"):
            validate_sql("PRAGMA database_list")


# ── Python validation: allowed code ───────────────────────────────────────────

class TestValidatePythonAllowed:

    def test_matplotlib_plot(self):
        validate_python("import matplotlib.pyplot as plt\nfig, ax = plt.subplots()")

    def test_seaborn(self):
        validate_python("import seaborn as sns\nsns.heatmap(df)")

    def test_numpy_ops(self):
        validate_python("import numpy as np\nx = np.mean([1,2,3])")

    def test_pandas_ops(self):
        validate_python("df.head()\ndf.groupby('x').mean()")

    def test_savefig(self):
        validate_python("plt.savefig(OUTPUT_PATH, bbox_inches='tight', dpi=150)")

    def test_scipy(self):
        validate_python("from scipy import stats\nstats.ttest_ind(a, b)")

    def test_sklearn(self):
        validate_python("from sklearn.decomposition import PCA\npca = PCA(n_components=2)")

    def test_dict_get(self):
        validate_python("d = {'a': 1}\nd.get('a')")

    def test_dataframe_get(self):
        validate_python("df.get('column_name')")


# ── Python validation: blocked imports ────────────────────────────────────────

class TestValidatePythonBlockedImports:

    @pytest.mark.security
    @pytest.mark.parametrize("module", sorted(_BLOCKED_MODULES))
    def test_import_blocked(self, module):
        with pytest.raises(ValueError, match="not permitted"):
            validate_python(f"import {module}")

    @pytest.mark.security
    @pytest.mark.parametrize("module", sorted(_BLOCKED_MODULES))
    def test_from_import_blocked(self, module):
        with pytest.raises(ValueError, match="not permitted"):
            validate_python(f"from {module} import something")

    @pytest.mark.security
    def test_dotted_import_blocked_by_top_module(self):
        with pytest.raises(ValueError, match="not permitted"):
            validate_python("import os.path")

    @pytest.mark.security
    def test_from_dotted_import_blocked_by_top_module(self):
        with pytest.raises(ValueError, match="not permitted"):
            validate_python("from os.path import join")


# ── Python validation: blocked builtins ───────────────────────────────────────

class TestValidatePythonBlockedBuiltins:

    @pytest.mark.security
    @pytest.mark.parametrize("builtin", sorted(_BLOCKED_BUILTINS))
    def test_builtin_call_blocked(self, builtin):
        with pytest.raises(ValueError, match="not permitted"):
            validate_python(f"{builtin}('something')")

    @pytest.mark.security
    @pytest.mark.parametrize("builtin", sorted(_BLOCKED_BUILTINS))
    def test_builtin_attribute_call_blocked(self, builtin):
        """Attribute-style calls like x.eval() should also be blocked."""
        with pytest.raises(ValueError, match="not permitted"):
            validate_python(f"obj.{builtin}('something')")


# ── Python validation: blocked data loaders ───────────────────────────────────

class TestValidatePythonBlockedDataLoaders:

    @pytest.mark.security
    @pytest.mark.parametrize("loader", sorted(_BLOCKED_DATA_LOADERS))
    def test_attribute_data_loader_blocked(self, loader):
        with pytest.raises(ValueError, match="not permitted"):
            validate_python(f"pd.{loader}('file.csv')")

    @pytest.mark.security
    def test_numpy_load_blocked(self):
        with pytest.raises(ValueError, match="not permitted"):
            validate_python("np.load('weights.npy')")

    @pytest.mark.security
    def test_numpy_loadtxt_blocked(self):
        with pytest.raises(ValueError, match="not permitted"):
            validate_python("np.loadtxt('data.txt')")


# ── Python validation: syntax errors ─────────────────────────────────────────

class TestValidatePythonSyntax:

    def test_syntax_error_raises(self):
        with pytest.raises(ValueError, match="Syntax error"):
            validate_python("def f(\n")

    def test_empty_code_passes(self):
        validate_python("")


# ── Viz script builder ────────────────────────────────────────────────────────

class TestBuildVizScript:

    def test_data_path_injected(self):
        script = _build_viz_script("print('hi')", "/tmp/data.parquet", "/tmp/out.png")
        assert "DATA_PATH = '/tmp/data.parquet'" in script

    def test_output_path_injected(self):
        script = _build_viz_script("print('hi')", "/tmp/data.parquet", "/tmp/out.png")
        assert "OUTPUT_PATH = '/tmp/out.png'" in script

    def test_user_code_appended(self):
        script = _build_viz_script("my_custom_code()", "/a", "/b")
        assert script.endswith("my_custom_code()")

    def test_preamble_imports(self):
        script = _build_viz_script("", "/a", "/b")
        assert "import pandas as pd" in script
        assert "import matplotlib" in script
        assert "import seaborn as sns" in script
        assert "import numpy as np" in script


# ── SQL validation: bypasses found in the 2026-09 review ──────────────────────
#
# Each of these passed the old FROM-only token check. validate_sql now rejects
# a blocked function name followed by `(` anywhere in the statement.

class TestValidateSqlBypasses:

    @pytest.mark.security
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM obscat, read_text('/etc/hostname')",
        "SELECT * FROM obscat JOIN read_text('/etc/hostname') ON true",
        "SELECT * FROM obscat CROSS JOIN read_csv('/etc/hostname')",
        "SELECT * FROM obscat, LATERAL read_csv('/etc/hostname')",
        "SELECT * FROM main.read_text('/etc/hostname')",
        'SELECT * FROM "read_text"(\'/etc/hostname\')',
        'SELECT * FROM main."read_text"(\'/etc/hostname\')',
        "SELECT (SELECT content FROM read_text('/etc/hostname'))",
        "SELECT * FROM obscat WHERE cell_name IN (SELECT content FROM read_text('/etc/hostname'))",
        "SELECT * FROM sniff_csv('/etc/hostname')",
        "SELECT * FROM query('SELECT 1')",
        "SELECT * FROM query_table('obscat')",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM duckdb_secrets()",
        "SELECT current_setting('memory_limit')",
        "WITH x AS (SELECT * FROM read_text('/etc/hostname')) SELECT * FROM x",
        "SELECT * FROM READ_TEXT('/etc/hostname')",
        "SELECT * FROM read_text /* comment */ ('/etc/hostname')",
        "SELECT * FROM obscat JOIN '/etc/hostname' ON true",
    ])
    def test_bypass_blocked(self, sql):
        with pytest.raises(ValueError):
            validate_sql(sql)

    @pytest.mark.parametrize("sql", [
        "SELECT query FROM obscat",                 # column named like a function
        "SELECT read_text FROM obscat",
        "SELECT * FROM obscat o JOIN obsnum n USING (cell_name)",
        "SELECT * FROM obscat, obsnum",
        "SELECT 'read_text(x)' AS s",               # name inside a string literal
    ])
    def test_legitimate_queries_still_allowed(self, sql):
        validate_sql(sql)


# ── Session id validation ─────────────────────────────────────────────────────

class TestSessionIdValidation:

    @pytest.mark.security
    @pytest.mark.parametrize("sid", [
        "../../etc/passwd", "../x", "abc", "A" * 32, "g" * 32,
        "0" * 31, "0" * 33, "0" * 32 + "'", "", None, 123,
    ])
    def test_bad_session_ids_rejected(self, sid):
        from analytics import validate_session_id
        with pytest.raises(ValueError):
            validate_session_id(sid)

    def test_uuid_hex_accepted(self):
        import uuid
        from analytics import validate_session_id
        sid = uuid.uuid4().hex
        assert validate_session_id(sid) == sid

    @pytest.mark.security
    def test_run_query_rejects_traversal_session_id(self, tmp_path):
        from analytics import run_query
        with pytest.raises(ValueError):
            run_query(str(tmp_path / "x.duckdb"), "SELECT 1", session_id="../../x")

    @pytest.mark.security
    def test_run_query_rejects_quote_in_attach_sessions(self, tmp_path):
        from analytics import run_query
        with pytest.raises(ValueError):
            run_query(str(tmp_path / "x.duckdb"), "SELECT 1",
                      attach_sessions=["x') UNION SELECT * FROM read_text('/etc/hostname"])

    @pytest.mark.security
    def test_run_viz_rejects_traversal_session_id(self):
        from analytics import run_viz
        with pytest.raises(ValueError):
            run_viz("../../tmp/x", "plt.savefig(OUTPUT_PATH)")


# ── Engine-level lockdown (the real boundary) ─────────────────────────────────
#
# These call _open_analytics_connection directly and execute SQL WITHOUT
# validate_sql, proving that file access is refused by DuckDB itself even if
# the token check is bypassed again in future.

@pytest.fixture
def analytics_dataset(tmp_path, monkeypatch):
    import duckdb
    monkeypatch.setenv('ANTHIVE_ANALYTICS_TMP', str(tmp_path / 'analytics'))
    secret = tmp_path / 'anthive.yaml'
    secret.write_text('secret_access_key: CANARY_SECRET_VALUE\n')
    db_path = tmp_path / 'ds.duckdb'
    sidecar = tmp_path / 'ds.layer.X.by_cell.parquet'
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE __cells AS SELECT range AS cell_id, 'c' || range AS cell_name FROM range(5)")
    conn.execute(f"COPY (SELECT range AS cell_id, 0 AS gene_id, range * 1.5 AS value FROM range(5)) TO '{sidecar}' (FORMAT parquet)")
    conn.execute(f"CREATE VIEW layer__X AS SELECT * FROM read_parquet('{sidecar}')")
    conn.close()
    return {'db': db_path, 'secret': secret, 'tmp': tmp_path}


class TestEngineLockdown:

    @pytest.mark.security
    @pytest.mark.parametrize("template", [
        "SELECT * FROM read_text('{secret}')",
        "SELECT * FROM read_csv('{secret}')",
        "SELECT * FROM __cells, read_text('{secret}')",
        "SELECT * FROM sniff_csv('{secret}')",
        "SELECT * FROM '{secret}'",
        "SELECT * FROM glob('{dir}/*')",
    ])
    def test_file_reads_refused_by_engine(self, analytics_dataset, template):
        import duckdb
        from analytics import _open_analytics_connection
        conn = _open_analytics_connection(str(analytics_dataset['db']))
        try:
            sql = template.format(secret=analytics_dataset['secret'],
                                  dir=analytics_dataset['tmp'])
            with pytest.raises(duckdb.Error) as excinfo:
                conn.execute(sql).fetchall()
            assert 'CANARY_SECRET_VALUE' not in str(excinfo.value)
        finally:
            conn.close()

    @pytest.mark.security
    @pytest.mark.parametrize("stmt", [
        "SET enable_external_access = true",
        "SET allowed_directories = ['/']",
        "SET lock_configuration = false",
        "ATTACH '/tmp/other.duckdb' AS other",
    ])
    def test_config_cannot_be_reopened(self, analytics_dataset, stmt):
        import duckdb
        from analytics import _open_analytics_connection
        conn = _open_analytics_connection(str(analytics_dataset['db']))
        try:
            with pytest.raises(duckdb.Error):
                conn.execute(stmt)
        finally:
            conn.close()

    def test_dataset_and_sidecar_views_still_work(self, analytics_dataset):
        from analytics import _open_analytics_connection
        conn = _open_analytics_connection(str(analytics_dataset['db']))
        try:
            assert conn.execute("SELECT COUNT(*) FROM __cells").fetchone()[0] == 5
            assert conn.execute("SELECT SUM(value) FROM layer__X").fetchone()[0] == pytest.approx(15.0)
        finally:
            conn.close()

    def test_attach_sessions_and_extra_views_work(self, analytics_dataset):
        pytest.importorskip('pyarrow')
        import uuid
        import pandas as pd
        from analytics import _get_tmp_dir, _open_analytics_connection
        sid = uuid.uuid4().hex
        pd.DataFrame({'cell_name': ['c0', 'c1'], 'module_score': [0.5, 1.5]}).to_parquet(
            _get_tmp_dir() / f"{sid}.parquet", index=False)
        extra = analytics_dataset['tmp'] / 'module_variability.parquet'
        pd.DataFrame({'x': [1, 2, 3]}).to_parquet(extra, index=False)
        conn = _open_analytics_connection(
            str(analytics_dataset['db']), attach_sessions=[sid],
            extra_parquet_views={'module_variability': str(extra)})
        try:
            assert conn.execute("SELECT SUM(module_score) FROM scores").fetchone()[0] == pytest.approx(2.0)
            assert conn.execute("SELECT COUNT(*) FROM module_variability").fetchone()[0] == 3
        finally:
            conn.close()

    def test_run_query_end_to_end(self, analytics_dataset):
        pytest.importorskip('pyarrow')
        from analytics import run_query
        result = run_query(str(analytics_dataset['db']),
                           "SELECT cell_name FROM __cells ORDER BY cell_id")
        assert result['row_count'] == 5
        assert result['preview'][0]['cell_name'] == 'c0'

    def test_pool_style_connection_on_same_file_not_locked(self, analytics_dataset):
        """Lockdown runs on a private in-memory instance; a pooled read-only
        connection to the same file must stay configurable."""
        import duckdb
        from analytics import _open_analytics_connection
        pooled = duckdb.connect(str(analytics_dataset['db']), read_only=True)
        locked = _open_analytics_connection(str(analytics_dataset['db']))
        try:
            pooled.execute("SET memory_limit = '1GB'")
            assert pooled.execute("SELECT current_setting('enable_external_access')").fetchone()[0] is True
        finally:
            locked.close()
            pooled.close()

    @pytest.mark.security
    def test_timeout_is_clamped_and_interrupts(self, analytics_dataset, monkeypatch):
        import time
        import analytics
        monkeypatch.setattr(analytics, 'QUERY_TIMEOUT', 1)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="1s"):
            analytics.run_query(
                str(analytics_dataset['db']),
                "SELECT COUNT(*) FROM range(100000000000) a",
                timeout=10_000)
        assert time.monotonic() - started < 15


# ── Viz subprocess environment ────────────────────────────────────────────────

class TestVizEnvironment:

    @pytest.mark.security
    def test_viz_child_does_not_inherit_env(self, tmp_path, monkeypatch):
        pytest.importorskip('matplotlib')
        pytest.importorskip('seaborn')
        pytest.importorskip('pyarrow')
        import uuid
        import pandas as pd
        import analytics
        monkeypatch.setenv('ANTHIVE_ANALYTICS_TMP', str(tmp_path))
        monkeypatch.setenv('SECRET_CANARY', 'leaked')
        sid = uuid.uuid4().hex
        pd.DataFrame({'x': [1, 2]}).to_parquet(tmp_path / f"{sid}.parquet", index=False)
        code = (
            "assert pd.io.common.os.environ.get('SECRET_CANARY') is None, 'env leaked'\n"
            "plt.plot(df['x']); plt.savefig(OUTPUT_PATH)\n"
        )
        figure_b64, fmt = analytics.run_viz(sid, code)
        assert fmt == 'png' and len(figure_b64) > 100
        assert not list(tmp_path.glob('_viz_*')), "scratch dir not cleaned up"


@pytest.mark.parametrize('sql', [
    'SELECT capdate FROM __obscat',
    'select * from main.__obscat',
    'SELECT * FROM "__obsnum"',
    'SELECT x FROM (FROM __OBSCAT)',
    'SELECT * FROM main."__obscat"',
    'SELECT * FROM anthive.main.__obscat',
])
def test_raw_obs_tables_are_not_queryable(sql):
    """The physical obs tables carry columns and values that api.obs_hidden /
    obs_value_hidden keep out of the public views."""
    with pytest.raises(ValueError, match='internal'):
        analytics.validate_sql(sql)


def test_views_and_other_tables_stay_queryable():
    analytics.validate_sql('SELECT ant_sex, count(*) FROM obscat GROUP BY 1')
    analytics.validate_sql('SELECT o.* FROM obscat o JOIN __cells c USING (cell_name)')


def test_schema_leaves_out_raw_obs_tables():
    conn = duckdb.connect()
    conn.execute("CREATE TABLE __obscat (cell_id INT, capdate VARCHAR)")
    conn.execute("INSERT INTO __obscat VALUES (1, '2019-06-13')")
    conn.execute("CREATE VIEW obscat AS SELECT cell_id FROM __obscat")
    tables = {t['table'] for t in analytics.get_schema(conn)}
    assert 'obscat' in tables and '__obscat' not in tables
