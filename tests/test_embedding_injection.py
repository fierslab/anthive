"""SQL-injection regression tests for AnthiveDB.get_embedding_data.

The plot endpoints (/plot/scatter, /plot/hexbin) pass ?subset= filters and the
URL path's embedding id straight into get_embedding_data. Before 2026-09 both
were pasted into the SQL text; a crafted value could read local files through
DuckDB error messages. Values are now bound parameters, field names and the
embedding id are resolved against the schema.
"""

import duckdb
import pytest

from anthive_db import AnthiveDB


@pytest.fixture
def db(tmp_path):
    secret = tmp_path / 'secret.txt'
    secret.write_text('CANARY_SECRET_VALUE\n')
    conn = duckdb.connect(':memory:')
    conn.execute("CREATE TABLE __cells(cell_id INTEGER, cell_name VARCHAR)")
    conn.executemany("INSERT INTO __cells VALUES (?, ?)",
                     [(i, f"cell_{i}") for i in range(10)])
    conn.execute("CREATE VIEW cells AS SELECT * FROM __cells")
    conn.execute("""CREATE TABLE __obscat(cell_id INTEGER, cell_name VARCHAR,
                    cell_type VARCHAR, "Overall AD Change" VARCHAR)""")
    for i in range(10):
        conn.execute("INSERT INTO __obscat VALUES (?, ?, ?, ?)",
                     [i, f"cell_{i}", 'microglia' if i < 5 else "o'dd", 'high' if i % 2 else 'low'])
    conn.execute("CREATE VIEW obscat AS SELECT * FROM __obscat")
    conn.execute("CREATE TABLE __obsnum(cell_id INTEGER, cell_name VARCHAR, n_counts DOUBLE)")
    for i in range(10):
        conn.execute("INSERT INTO __obsnum VALUES (?, ?, ?)", [i, f"cell_{i}", float(i * 100)])
    conn.execute("CREATE VIEW obsnum AS SELECT * FROM __obsnum")
    conn.execute("CREATE TABLE __obsm_X_umap(cell_name VARCHAR, dim_0 DOUBLE, dim_1 DOUBLE)")
    for i in range(10):
        conn.execute("INSERT INTO __obsm_X_umap VALUES (?, ?, ?)", [f"cell_{i}", float(i), float(-i)])
    conn.execute("CREATE VIEW obsm__X_umap AS SELECT * FROM __obsm_X_umap")
    handle = AnthiveDB(conn=conn)
    handle._secret_path = str(secret)
    yield handle
    conn.close()


def test_plain_embedding_fetch(db):
    df = db.get_embedding_data('X_umap')
    assert list(df.columns) == ['cell_name', 'dim_0', 'dim_1']
    assert len(df) == 10


def test_categorical_filter(db):
    df = db.get_embedding_data('X_umap', filters={'cell_type': 'microglia'})
    assert len(df) == 5


def test_value_with_quote_is_literal(db):
    df = db.get_embedding_data('X_umap', filters={'cell_type': "o'dd"})
    assert len(df) == 5


def test_in_list_and_range_filters(db):
    df = db.get_embedding_data('X_umap', filters={'cell_type': ['microglia', "o'dd"],
                                                  'n_counts': (200.0, 400.0)})
    assert sorted(df['cell_name']) == ['cell_2', 'cell_3', 'cell_4']


def test_column_with_space_in_name(db):
    df = db.get_embedding_data('X_umap', filters={'Overall AD Change': 'high'})
    assert len(df) == 5


def test_numeric_string_on_categorical_column(db):
    # Categorical values are bound as strings: no Conversion Error.
    df = db.get_embedding_data('X_umap', filters={'cell_type': '3'})
    assert len(df) == 0


def test_sample_and_limit(db):
    assert len(db.get_embedding_data('X_umap', limit=3)) == 3
    assert len(db.get_embedding_data('X_umap', sample=4)) == 4


@pytest.mark.security
def test_value_injection_is_inert(db):
    payload = ("x' OR CAST((SELECT content FROM read_text('"
               + db._secret_path + "')) AS INTEGER)=1 OR 'a'='b")
    df = db.get_embedding_data('X_umap', filters={'cell_type': payload})
    assert len(df) == 0


@pytest.mark.security
def test_list_value_injection_is_inert(db):
    payload = "x') OR 1=1 OR cell_type IN ('y"
    df = db.get_embedding_data('X_umap', filters={'cell_type': [payload, 'nothing']})
    assert len(df) == 0


@pytest.mark.security
def test_numeric_value_injection_rejected(db):
    with pytest.raises(ValueError, match="not numeric"):
        db.get_embedding_data('X_umap', filters={'n_counts': "0 OR 1=1"})


@pytest.mark.security
def test_range_value_injection_rejected(db):
    with pytest.raises(ValueError):
        db.get_embedding_data('X_umap', filters={'n_counts': ("0", "1 OR 1=1")})


@pytest.mark.security
@pytest.mark.parametrize("field", [
    'cell_type" = cell_type OR "cell_type',
    'x") OR 1=1 --',
    'no_such_column',
])
def test_field_name_injection_rejected(db, field):
    with pytest.raises(ValueError, match="Unknown filter field"):
        db.get_embedding_data('X_umap', filters={field: 'microglia'})


@pytest.mark.security
@pytest.mark.parametrize("embedding_id", [
    "X_umap' UNION SELECT 1 --",
    "X_umap; DROP TABLE __cells",
    "nonexistent",
    "X_umap\" , (SELECT content FROM read_text('/etc/hostname')) --",
])
def test_bogus_embedding_id_rejected(db, embedding_id):
    with pytest.raises(ValueError, match="not found"):
        db.get_embedding_data(embedding_id)


@pytest.mark.security
def test_error_messages_never_carry_file_content(db):
    payload = ("x' OR CAST((SELECT content FROM read_text('"
               + db._secret_path + "')) AS INTEGER)=1 OR 'a'='b")
    for filters in ({'cell_type': payload}, {payload: 'x'}, {'n_counts': payload}):
        try:
            db.get_embedding_data('X_umap', filters=filters)
        except Exception as exc:  # noqa: BLE001
            assert 'CANARY_SECRET_VALUE' not in str(exc)
