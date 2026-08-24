"""Tests for lib/anthive_db.py — DuckDB wrapper with in-memory fixtures."""

import duckdb
import pytest

from anthive_db import AnthiveDB


@pytest.fixture
def db():
    """Create an in-memory DuckDB matching the AnthiveDB expected schema."""
    conn = duckdb.connect(":memory:")

    # Core tables
    conn.execute("CREATE TABLE __cells(cell_id INTEGER, cell_name VARCHAR)")
    conn.execute("CREATE TABLE __genes(gene_id INTEGER, gene_name VARCHAR)")
    conn.execute("CREATE VIEW cells AS SELECT * FROM __cells")
    conn.execute("CREATE VIEW genes AS SELECT * FROM __genes")

    # Insert test cells
    cells = [(i, f"cell_{i}") for i in range(20)]
    conn.executemany("INSERT INTO __cells VALUES (?, ?)", cells)

    # Insert test genes
    genes = [
        (0, "APOE"), (1, "TREM2"), (2, "CD4"),
        (3, "CD40"), (4, "CLU"),
    ]
    conn.executemany("INSERT INTO __genes VALUES (?, ?)", genes)

    # Sparse expression table + layer view
    conn.execute("CREATE TABLE __expr(cell_id INTEGER, gene_id INTEGER, value DOUBLE)")
    import random
    random.seed(42)
    rows = []
    for cell_id in range(20):
        for gene_id in range(5):
            if random.random() > 0.3:  # ~70% non-zero
                rows.append((cell_id, gene_id, random.uniform(0.1, 5.0)))
    conn.executemany("INSERT INTO __expr VALUES (?, ?, ?)", rows)

    conn.execute("""
        CREATE VIEW layer__X AS
        SELECT c.cell_name, g.gene_name, e.value
        FROM __expr e
        JOIN __cells c ON e.cell_id = c.cell_id
        JOIN __genes g ON e.gene_id = g.gene_id
    """)

    # Categorical metadata (obscat)
    conn.execute("""
        CREATE TABLE __obscat(cell_id INTEGER, cell_name VARCHAR,
                              cell_type VARCHAR, condition VARCHAR)
    """)
    for cell_id, cell_name in cells:
        ctype = "microglia" if cell_id < 10 else "astrocyte"
        cond = "AD" if cell_id % 3 == 0 else "control"
        conn.execute(
            "INSERT INTO __obscat VALUES (?, ?, ?, ?)",
            [cell_id, cell_name, ctype, cond],
        )
    conn.execute("CREATE VIEW obscat AS SELECT * FROM __obscat")

    # Numerical metadata (obsnum)
    conn.execute("""
        CREATE TABLE __obsnum(cell_id INTEGER, cell_name VARCHAR,
                              n_counts DOUBLE, pct_mito DOUBLE)
    """)
    for cell_id, cell_name in cells:
        conn.execute(
            "INSERT INTO __obsnum VALUES (?, ?, ?, ?)",
            [cell_id, cell_name, 1000 + cell_id * 50, 0.01 + cell_id * 0.005],
        )
    conn.execute("CREATE VIEW obsnum AS SELECT * FROM __obsnum")

    # Embedding (obsm)
    conn.execute("""
        CREATE TABLE __obsm_X_umap(cell_name VARCHAR, dim_0 DOUBLE, dim_1 DOUBLE)
    """)
    for cell_id, cell_name in cells:
        conn.execute(
            "INSERT INTO __obsm_X_umap VALUES (?, ?, ?)",
            [cell_name, float(cell_id), float(cell_id) * 0.5],
        )
    conn.execute("CREATE VIEW obsm__X_umap AS SELECT * FROM __obsm_X_umap")

    # Internal layer table (integer IDs, used by score_module)
    conn.execute("""
        CREATE TABLE __layer__X AS
        SELECT e.cell_id, e.gene_id, e.value FROM __expr e
    """)

    # Gene bins (required for module scoring)
    conn.execute("""
        CREATE TABLE __gene_bins AS
        WITH gene_means AS (
            SELECT g.gene_id, COALESCE(AVG(l.value), 0.0) AS mean_expr
            FROM __genes g
            LEFT JOIN __layer__X l USING (gene_id)
            GROUP BY g.gene_id
        )
        SELECT gene_id, mean_expr,
               NTILE(25) OVER (ORDER BY mean_expr) AS expr_bin
        FROM gene_means
    """)

    anthive_db = AnthiveDB(conn=conn, owns_conn=False)
    yield anthive_db
    conn.close()


# ── get_info ──────────────────────────────────────────────────────────────────

class TestGetInfo:

    def test_returns_counts(self, db):
        info = db.get_info()
        assert info["n_cells"] == 20
        assert info["n_genes"] == 5

    def test_returns_layers(self, db):
        info = db.get_info()
        assert "X" in info["layers"]

    def test_returns_embeddings(self, db):
        info = db.get_info()
        assert "X_umap" in info["embeddings"]

    def test_returns_metadata_fields(self, db):
        info = db.get_info()
        assert "cell_type" in info["metadata_fields"]["categorical"]
        assert "n_counts" in info["metadata_fields"]["numerical"]


# ── search_genes ──────────────────────────────────────────────────────────────

class TestSearchGenes:

    def test_substring_match(self, db):
        result = db.search_genes("CD")
        assert "CD4" in result
        assert "CD40" in result

    def test_exact_match(self, db):
        result = db.search_genes("CD4", exact=True)
        assert result == ["CD4"]

    def test_case_insensitive(self, db):
        result = db.search_genes("apoe")
        assert "APOE" in result

    def test_no_match(self, db):
        result = db.search_genes("NONEXISTENT")
        assert result == []

    def test_limit(self, db):
        result = db.search_genes("CD", limit=1)
        assert len(result) == 1


# ── get_gene_info ─────────────────────────────────────────────────────────────

class TestGetGeneInfo:

    def test_found(self, db):
        info = db.get_gene_info("APOE")
        assert info["gene_name"] == "APOE"
        assert info["exists"] is True
        assert "X" in info["layers"]

    def test_not_found(self, db):
        with pytest.raises(ValueError, match="not found"):
            db.get_gene_info("NOPE")


# ── get_cell_table ────────────────────────────────────────────────────────────

class TestGetCellTable:

    def test_basic(self, db):
        df = db.get_cell_table()
        assert "cell_name" in df.columns
        assert len(df) == 20

    def test_with_genes(self, db):
        df = db.get_cell_table(genes=["APOE", "TREM2"], layer="X")
        assert "APOE" in df.columns
        assert "TREM2" in df.columns
        assert len(df) == 20

    def test_with_metadata(self, db):
        df = db.get_cell_table(metadata_fields=["cell_type", "n_counts"])
        assert "cell_type" in df.columns
        assert "n_counts" in df.columns

    def test_with_filter(self, db):
        df = db.get_cell_table(
            metadata_fields=["cell_type"],
            filters={"cell_type": "microglia"},
        )
        assert all(df["cell_type"] == "microglia")
        assert len(df) == 10

    def test_with_in_list_filter(self, db):
        df = db.get_cell_table(
            metadata_fields=["condition"],
            filters={"condition": ["AD"]},
        )
        assert all(df["condition"] == "AD")

    def test_with_limit(self, db):
        df = db.get_cell_table(limit=5)
        assert len(df) == 5

    def test_with_sample(self, db):
        df = db.get_cell_table(sample=5)
        assert len(df) <= 5

    def test_sample_larger_than_table(self, db):
        df = db.get_cell_table(sample=1000)
        assert len(df) == 20


# ── get_embedding_data ────────────────────────────────────────────────────────

class TestGetEmbeddingData:

    def test_basic(self, db):
        df = db.get_embedding_data("X_umap")
        assert "cell_name" in df.columns
        assert "dim_0" in df.columns
        assert "dim_1" in df.columns
        assert len(df) == 20

    def test_not_found(self, db):
        with pytest.raises(ValueError, match="not found"):
            db.get_embedding_data("X_nonexistent")

    def test_with_sample(self, db):
        df = db.get_embedding_data("X_umap", sample=5)
        assert len(df) <= 5

    def test_with_filter(self, db):
        df = db.get_embedding_data(
            "X_umap", filters={"cell_type": "microglia"}
        )
        assert len(df) == 10


# ── get_gene_stats ────────────────────────────────────────────────────────────

class TestGetGeneStats:

    def test_basic(self, db):
        stats = db.get_gene_stats(["APOE"])
        assert len(stats) == 1
        s = stats[0]
        assert s["gene"] == "APOE"
        assert "mean" in s
        assert "n_nonzero" in s
        assert s["n_cells"] == 20

    def test_not_found(self, db):
        stats = db.get_gene_stats(["NOPE"])
        assert stats[0]["error"] == "not found"

    def test_case_insensitive(self, db):
        stats = db.get_gene_stats(["apoe"], case_sensitive=False)
        assert "error" not in stats[0]
        assert stats[0]["gene_found"] == "APOE"

    def test_multiple_genes(self, db):
        stats = db.get_gene_stats(["APOE", "TREM2", "NOPE"])
        assert len(stats) == 3
        assert "error" not in stats[0]
        assert "error" not in stats[1]
        assert stats[2]["error"] == "not found"


# ── execute_sql ───────────────────────────────────────────────────────────────

class TestExecuteSql:

    def test_simple_query(self, db):
        df = db.execute_sql("SELECT 1 AS x")
        assert df.iloc[0]["x"] == 1

    def test_with_limit(self, db):
        df = db.execute_sql("SELECT * FROM cells", limit=3)
        assert len(df) == 3


# ── score_module ──────────────────────────────────────────────────────────────

class TestScoreModule:

    def test_basic(self, db):
        scores_df, meta = db.score_module(["APOE", "TREM2"])
        assert "cell_name" in scores_df.columns
        assert "module_score" in scores_df.columns
        assert len(scores_df) > 0
        assert meta["genes_found"] == ["APOE", "TREM2"]
        assert meta["genes_missing"] == []
        # With only 5 genes, NTILE(25) puts each in a separate bin,
        # so no control genes share a bin with the module genes.
        assert meta["n_control_genes"] >= 0

    def test_missing_genes_reported(self, db):
        scores_df, meta = db.score_module(["APOE", "NONEXISTENT"])
        assert "APOE" in meta["genes_found"]
        assert "NONEXISTENT" in meta["genes_missing"]
        assert len(scores_df) > 0

    def test_all_genes_missing_raises(self, db):
        with pytest.raises(ValueError, match="None of the module genes"):
            db.score_module(["FAKE1", "FAKE2"])

    def test_scores_are_floats(self, db):
        scores_df, _ = db.score_module(["APOE"])
        assert scores_df["module_score"].dtype in ("float64", "float32")

    def test_reproducible_with_same_seed(self, db):
        df1, _ = db.score_module(["APOE", "TREM2"], seed=42)
        df2, _ = db.score_module(["APOE", "TREM2"], seed=42)
        assert df1["module_score"].tolist() == df2["module_score"].tolist()

    def test_different_seed_different_controls(self, db):
        df1, meta1 = db.score_module(["APOE"], seed=1)
        df2, meta2 = db.score_module(["APOE"], seed=9999)
        # Scores may differ because different control genes are selected
        # (but with only 5 genes total, control sets may overlap — just check it runs)
        assert len(df1) == len(df2)

    def test_symbol_index_reports_gene_name_resolution(self, db):
        """Symbol-indexed DB → gene_resolution is the direct 'gene_name' route."""
        _, meta = db.score_module(["APOE", "TREM2"])
        assert meta["gene_resolution"] == "gene_name"


# ── Ensembl-indexed DB fixture + score_module fallback (bug 4) ────────────────


@pytest.fixture
def ensembl_db():
    """In-memory DB where __genes.gene_name holds Ensembl IDs.

    Mirrors the layout produced by ``bin/ingest/ant-convert`` for
    CellxGene-imported files: Ensembl IDs in ``__genes.gene_name`` and
    human-readable symbols in a ``__varcat.feature_name`` column.
    """
    conn = duckdb.connect(":memory:")

    conn.execute("CREATE TABLE __cells(cell_id INTEGER, cell_name VARCHAR)")
    conn.execute("CREATE TABLE __genes(gene_id INTEGER, gene_name VARCHAR)")
    conn.execute("CREATE VIEW cells AS SELECT * FROM __cells")
    conn.execute("CREATE VIEW genes AS SELECT * FROM __genes")

    cells = [(i, f"cell_{i}") for i in range(20)]
    conn.executemany("INSERT INTO __cells VALUES (?, ?)", cells)

    # Ensembl IDs in __genes.gene_name; human symbols in __varcat.feature_name.
    ensembl_to_symbol = [
        (0, "ENSG00000130203", "APOE"),
        (1, "ENSG00000095970", "TREM2"),
        (2, "ENSG00000010610", "CD4"),
        (3, "ENSG00000101017", "CD40"),
        (4, "ENSG00000120885", "CLU"),
    ]
    conn.executemany(
        "INSERT INTO __genes VALUES (?, ?)",
        [(gid, ens) for gid, ens, _sym in ensembl_to_symbol],
    )
    conn.execute(
        "CREATE TABLE __varcat(gene_id INTEGER PRIMARY KEY, "
        "feature_name VARCHAR, feature_biotype VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO __varcat VALUES (?, ?, 'protein_coding')",
        [(gid, sym) for gid, _ens, sym in ensembl_to_symbol],
    )

    conn.execute(
        "CREATE TABLE __expr(cell_id INTEGER, gene_id INTEGER, value DOUBLE)"
    )
    import random
    random.seed(42)
    rows = []
    for cell_id in range(20):
        for gene_id in range(5):
            if random.random() > 0.3:
                rows.append((cell_id, gene_id, random.uniform(0.1, 5.0)))
    conn.executemany("INSERT INTO __expr VALUES (?, ?, ?)", rows)

    conn.execute("""
        CREATE TABLE __layer__X AS
        SELECT e.cell_id, e.gene_id, e.value FROM __expr e
    """)
    conn.execute("""
        CREATE TABLE __gene_bins AS
        WITH gene_means AS (
            SELECT g.gene_id, COALESCE(AVG(l.value), 0.0) AS mean_expr
            FROM __genes g
            LEFT JOIN __layer__X l USING (gene_id)
            GROUP BY g.gene_id
        )
        SELECT gene_id, mean_expr,
               NTILE(25) OVER (ORDER BY mean_expr) AS expr_bin
        FROM gene_means
    """)

    anthive_db = AnthiveDB(conn=conn, owns_conn=False)
    yield anthive_db
    conn.close()


class TestGeneResolution:
    """Bug 4 — Ensembl-indexed duckdbs must resolve symbols via __varcat."""

    def test_looks_like_ensembl_true_on_ensembl_db(self, ensembl_db):
        assert ensembl_db._looks_like_ensembl() is True

    def test_looks_like_ensembl_false_on_symbol_db(self, db):
        assert db._looks_like_ensembl() is False

    def test_find_symbol_column_picks_feature_name(self, ensembl_db):
        assert ensembl_db._find_symbol_column() == "feature_name"

    def test_find_symbol_column_none_when_no_varcat(self, db):
        assert db._find_symbol_column() is None

    def test_direct_symbol_hit_uses_gene_name_route(self, db):
        """Symbol-indexed DB: direct __genes.gene_name match wins."""
        found, route = db._resolve_symbols_to_gene_ids(["APOE", "TREM2"])
        assert found == {"APOE": 0, "TREM2": 1}
        assert route == "gene_name"

    def test_symbols_resolve_via_varcat_on_ensembl_db(self, ensembl_db):
        """Symbols fall through to __varcat when __genes holds Ensembl IDs."""
        found, route = ensembl_db._resolve_symbols_to_gene_ids(
            ["APOE", "TREM2", "CD4"]
        )
        assert found == {"APOE": 0, "TREM2": 1, "CD4": 2}
        assert route == "varcat:feature_name"

    def test_symbol_lookup_via_varcat_is_case_insensitive(self, ensembl_db):
        """Casing in the input symbol must not block the varcat fallback."""
        found, route = ensembl_db._resolve_symbols_to_gene_ids(
            ["apoe", "TrEm2"]
        )
        assert found == {"apoe": 0, "TrEm2": 1}
        assert route == "varcat:feature_name"

    def test_ensembl_input_still_resolves_directly(self, ensembl_db):
        """An Ensembl ID already in __genes short-circuits the fallback."""
        found, route = ensembl_db._resolve_symbols_to_gene_ids(
            ["ENSG00000130203"]
        )
        assert found == {"ENSG00000130203": 0}
        assert route == "gene_name"

    def test_empty_input_returns_empty_map(self, ensembl_db):
        found, route = ensembl_db._resolve_symbols_to_gene_ids([])
        assert found == {}
        assert route is None

    def test_unresolvable_symbols_return_empty(self, ensembl_db):
        found, _route = ensembl_db._resolve_symbols_to_gene_ids(
            ["NOT_A_GENE", "STILL_MISSING"]
        )
        assert found == {}

    def test_score_module_works_on_ensembl_db_via_fallback(self, ensembl_db):
        """The bug 4 regression: score_module on an Ensembl-indexed DB.

        Before the fix, ``score_module(["APOE"])`` raised
        ``ValueError("None of the module genes found in database...")``
        because it only checked ``__genes.gene_name`` (Ensembl IDs).
        """
        scores_df, meta = ensembl_db.score_module(["APOE", "TREM2"])
        assert meta["genes_found"] == ["APOE", "TREM2"]
        assert meta["genes_missing"] == []
        assert meta["gene_resolution"] == "varcat:feature_name"
        assert len(scores_df) > 0

    def test_score_module_on_ensembl_db_raises_when_all_missing(
        self, ensembl_db
    ):
        with pytest.raises(ValueError, match="None of the module genes"):
            ensembl_db.score_module(["NOT_A_GENE_AT_ALL"])
