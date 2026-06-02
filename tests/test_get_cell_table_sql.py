"""Verify the parameterized get_cell_table query is correct AND safe.

After the 2026-06 plot-endpoint hardening, gene names and filter values are
bound via DuckDB `?` placeholders instead of f-string interpolation. These
tests exercise that path with values that would have broken or injected
SQL under the old implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))

from anthive_db import AnthiveDB  # noqa: E402

CELL_NAMES = [f"cell_{i:03d}" for i in range(10)]
GENE_NAMES = ['TREM2', 'APOE', 'CX3CR1', "O'reilly", 'GENE,WITH,COMMAS']


def _build_db(path: Path) -> None:
    """Synthetic duckdb mirroring ant-convert's layout, including some
    gene names that would defeat naive f-string interpolation."""
    con = duckdb.connect(str(path), read_only=False)

    # cells
    con.execute("CREATE TABLE __cells (cell_id INTEGER PRIMARY KEY, cell_name TEXT NOT NULL UNIQUE)")
    cells_df = pd.DataFrame({'cell_id': range(len(CELL_NAMES)), 'cell_name': CELL_NAMES})
    con.register('temp_cells', cells_df); con.execute("INSERT INTO __cells SELECT * FROM temp_cells")
    con.execute("CREATE VIEW cells AS SELECT * FROM __cells")

    # genes
    con.execute("CREATE TABLE __genes (gene_id INTEGER PRIMARY KEY, gene_name TEXT NOT NULL UNIQUE)")
    # NOTE: safe_ident only accepts a bare-identifier-ish character set, so
    # gene names with quotes / commas can't be aliased and won't be requested
    # via plot endpoints in practice. We still store them to verify the
    # WHERE-clause binding path treats them as values (not SQL).
    genes_df = pd.DataFrame({'gene_id': range(len(GENE_NAMES)), 'gene_name': GENE_NAMES})
    con.register('temp_genes', genes_df); con.execute("INSERT INTO __genes SELECT * FROM temp_genes")
    con.execute("CREATE VIEW genes AS SELECT * FROM __genes")

    # one sparse layer
    con.execute("CREATE TABLE __layer__X (cell_id INTEGER, gene_id INTEGER, value FLOAT, "
                "PRIMARY KEY (cell_id, gene_id))")
    # populate sparsely: cell i × gene g → value = i*10 + g
    rows = [(i, g, float(i * 10 + g)) for i in range(len(CELL_NAMES))
            for g in range(len(GENE_NAMES))]
    layer_df = pd.DataFrame(rows, columns=['cell_id', 'gene_id', 'value'])
    con.register('temp_layer', layer_df); con.execute("INSERT INTO __layer__X SELECT * FROM temp_layer")
    con.execute("CREATE VIEW layer__X AS SELECT c.cell_name, g.gene_name, l.value "
                "FROM __layer__X l JOIN __cells c USING (cell_id) JOIN __genes g USING (gene_id)")

    # obs tables
    con.execute("CREATE TABLE __obsnum (cell_id INTEGER PRIMARY KEY, n_counts DOUBLE)")
    nums_df = pd.DataFrame({
        'cell_id': range(len(CELL_NAMES)),
        'n_counts': [100.0 + i for i in range(len(CELL_NAMES))],
    })
    con.register('t_num', nums_df); con.execute("INSERT INTO __obsnum SELECT * FROM t_num")
    con.execute("CREATE VIEW obsnum AS SELECT c.cell_name, o.* FROM __obsnum o JOIN __cells c USING (cell_id)")

    con.execute("CREATE TABLE __obscat (cell_id INTEGER PRIMARY KEY, sample VARCHAR, condition VARCHAR)")
    cats_df = pd.DataFrame({
        'cell_id': range(len(CELL_NAMES)),
        'sample': [f"S{i % 3}" for i in range(len(CELL_NAMES))],
        # one row with a single-quote so the OLD f-string code path would
        # have generated invalid SQL or, worse, allowed injection.
        'condition': ["it's ok" if i == 0 else f"cond_{i % 2}" for i in range(len(CELL_NAMES))],
    })
    con.register('t_cat', cats_df); con.execute("INSERT INTO __obscat SELECT * FROM t_cat")
    con.execute("CREATE VIEW obscat AS SELECT c.cell_name, o.* FROM __obscat o JOIN __cells c USING (cell_id)")

    con.close()


@pytest.fixture
def db(tmp_path) -> AnthiveDB:
    path = tmp_path / "test.duckdb"
    _build_db(path)
    handle = AnthiveDB(path)
    yield handle
    handle.close()


# ──────────────────────────────────────────────────────────────────────────

def test_get_cell_table_no_genes_no_filters(db):
    df = db.get_cell_table()
    assert len(df) == len(CELL_NAMES)
    assert list(df.columns) == ['cell_name']


def test_get_cell_table_with_gene(db):
    df = db.get_cell_table(genes=['TREM2'])
    assert 'TREM2' in df.columns
    # TREM2 has gene_id=0, value should equal i*10
    trem2 = dict(zip(df['cell_name'], df['TREM2']))
    assert trem2['cell_000'] == 0.0
    assert trem2['cell_005'] == 50.0


def test_get_cell_table_with_multiple_genes(db):
    df = db.get_cell_table(genes=['TREM2', 'APOE', 'CX3CR1'])
    assert {'TREM2', 'APOE', 'CX3CR1'}.issubset(df.columns)


def test_get_cell_table_gene_with_single_quote_in_value(db):
    """O'Reilly-style gene name — value carries a quote. Old code would
    have built `gene_name = 'O'reilly'` → SQL syntax error or injection.
    Parameterized binding handles it as a literal value."""
    # Note: safe_ident rejects this name for the SELECT alias (no quotes
    # allowed in identifiers), so calling get_cell_table with it directly
    # would raise. Instead, verify search_genes (which uses bound params)
    # finds it as a row.
    hits = db.search_genes("O'reilly", exact=True, limit=1, case_sensitive=False)
    assert hits == ["O'reilly"]


def test_get_cell_table_categorical_filter_exact(db):
    df = db.get_cell_table(metadata_fields=['sample'], filters={'sample': 'S1'})
    assert (df['sample'] == 'S1').all()
    # 10 cells, 1/3 are S1 → roughly 3 or 4
    assert 2 <= len(df) <= 5


def test_get_cell_table_filter_with_quote_in_value(db):
    """Filter value contains a single quote — must round-trip as a literal."""
    df = db.get_cell_table(
        metadata_fields=['condition'],
        filters={'condition': "it's ok"},
    )
    assert len(df) == 1
    assert df['condition'].iloc[0] == "it's ok"


def test_get_cell_table_filter_in_list(db):
    df = db.get_cell_table(
        metadata_fields=['sample'],
        filters={'sample': ['S0', 'S2']},
    )
    assert set(df['sample'].unique()) <= {'S0', 'S2'}
    assert len(df) > 0


def test_get_cell_table_filter_range(db):
    df = db.get_cell_table(
        metadata_fields=['n_counts'],
        filters={'n_counts': (102.0, 105.0)},
    )
    assert df['n_counts'].between(102.0, 105.0).all()
    assert len(df) == 4  # 102, 103, 104, 105


def test_get_cell_table_sample(db):
    df = db.get_cell_table(metadata_fields=['sample'], sample=5)
    assert len(df) == 5


def test_get_cell_table_injection_attempt_in_filter_value_is_neutralised(db):
    """A naive `f'{prefix}."{field}" = \\'{value}\\''` would interpret a
    classical injection payload as SQL; with parameterized binding it's
    just a string that doesn't match any row."""
    payload = "S1' OR 1=1 --"
    df = db.get_cell_table(
        metadata_fields=['sample'],
        filters={'sample': payload},
    )
    # The literal value never matches → empty result, no execution of OR 1=1
    assert len(df) == 0
