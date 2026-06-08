"""Tests for the analytics security sandbox (lib/analytics.py).

These are the highest-priority tests in the suite: validate_sql and
validate_python are the security boundary that prevents arbitrary file
access, data exfiltration, and code execution via the analytics endpoints.
"""

import pytest

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
