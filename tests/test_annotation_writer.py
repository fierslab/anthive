"""Smoke tests for AnnotationWriter against a synthetic duckdb.

Builds a tiny in-file duckdb with __cells / __genes / __obsnum / __obscat
matching the schema produced by ant-convert, then exercises the writer.

Run with: uv run pytest tests/test_annotation_writer.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))

from anthive_db import (  # noqa: E402
    AnnotationWriter,
    AnnotationWriterError,
    read_pending_cols,
    duckdb_obs_columns,
    pending_state_diagnosis,
    _validate_anno_col_name,
)

CELL_NAMES = [f"cell_{i:04d}" for i in range(20)]
GENE_NAMES = [f"GENE{i}" for i in range(5)]


def build_synthetic_duckdb(path: Path, with_obs_tables: bool = True) -> None:
    """Recreate the minimal subset of the ant-convert schema."""
    con = duckdb.connect(str(path), read_only=False)
    con.execute(
        "CREATE TABLE __cells (cell_id INTEGER PRIMARY KEY, cell_name TEXT NOT NULL UNIQUE)"
    )
    cells_df = pd.DataFrame({'cell_id': range(len(CELL_NAMES)), 'cell_name': CELL_NAMES})
    con.register('temp_cells', cells_df)
    con.execute("INSERT INTO __cells SELECT * FROM temp_cells")
    con.unregister('temp_cells')

    con.execute(
        "CREATE TABLE __genes (gene_id INTEGER PRIMARY KEY, gene_name TEXT NOT NULL UNIQUE)"
    )
    genes_df = pd.DataFrame({'gene_id': range(len(GENE_NAMES)), 'gene_name': GENE_NAMES})
    con.register('temp_genes', genes_df)
    con.execute("INSERT INTO __genes SELECT * FROM temp_genes")
    con.unregister('temp_genes')

    if with_obs_tables:
        # Pre-existing obsnum + obscat (like a dataset that already had some obs)
        con.execute(
            "CREATE TABLE __obsnum (cell_id INTEGER PRIMARY KEY, n_counts DOUBLE)"
        )
        df_num = pd.DataFrame({
            'cell_id': range(len(CELL_NAMES)),
            'n_counts': [100.0 + i for i in range(len(CELL_NAMES))],
        })
        con.register('t_num', df_num)
        con.execute("INSERT INTO __obsnum SELECT * FROM t_num")
        con.unregister('t_num')
        con.execute(
            "CREATE VIEW obsnum AS SELECT c.cell_name, o.* FROM __obsnum o "
            "JOIN __cells c ON o.cell_id = c.cell_id"
        )

        con.execute(
            "CREATE TABLE __obscat (cell_id INTEGER PRIMARY KEY, sample VARCHAR)"
        )
        df_cat = pd.DataFrame({
            'cell_id': range(len(CELL_NAMES)),
            'sample': [f"S{i % 3}" for i in range(len(CELL_NAMES))],
        })
        con.register('t_cat', df_cat)
        con.execute("INSERT INTO __obscat SELECT * FROM t_cat")
        con.unregister('t_cat')
        con.execute(
            "CREATE VIEW obscat AS SELECT c.cell_name, o.* FROM __obscat o "
            "JOIN __cells c ON o.cell_id = c.cell_id"
        )

    con.close()


# ---------- validators ----------

def test_bare_identifier_accepted():
    _validate_anno_col_name("starcat__MICROGLIA_V1__Microglia_DAM")
    _validate_anno_col_name("module_score_M3")
    _validate_anno_col_name("_private")


def test_invalid_identifiers_rejected():
    bad = [
        "starcat::MICROGLIA.V1::DAM",  # double-colon and dot
        "1leading_digit",
        "has space",
        'has"quote',
        "",
        "cell_name",       # reserved
        "Cell_ID",          # reserved (case-insensitive)
    ]
    for name in bad:
        with pytest.raises(ValueError):
            _validate_anno_col_name(name)


# ---------- writer happy-path ----------

def test_add_numerical_to_existing_obsnum(tmp_path):
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    values = pd.Series(
        {name: float(i) for i, name in enumerate(CELL_NAMES)},
        name='module_score',
    )

    with AnnotationWriter(path, annotator='ant-score', run_id='ws-1',
                          catalog='WGCNA') as w:
        w.add_numerical('module_score_M3', values, source_hash='abc123')
        w.commit()

    # Verify column exists in __obsnum
    cols = duckdb_obs_columns(path)
    assert 'module_score_M3' in cols['obsnum'], cols

    # Verify values present via the view
    con = duckdb.connect(str(path), read_only=True)
    try:
        result = con.execute(
            "SELECT cell_name, module_score_M3 FROM obsnum ORDER BY cell_name"
        ).fetchdf()
    finally:
        con.close()
    assert len(result) == len(CELL_NAMES)
    assert result['module_score_M3'].iloc[0] == 0.0
    assert result['module_score_M3'].iloc[19] == 19.0

    # __pending_cols entry recorded
    pending = read_pending_cols(path)
    assert len(pending) == 1
    assert pending['col_name'].iloc[0] == 'module_score_M3'
    assert pending['kind'].iloc[0] == 'num'
    assert pending['catalog'].iloc[0] == 'WGCNA'
    assert pending['source_hash'].iloc[0] == 'abc123'


def test_add_categorical_to_existing_obscat(tmp_path):
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    values = pd.Series(
        {name: ('A' if i < 10 else 'B') for i, name in enumerate(CELL_NAMES)},
        name='cluster',
    )

    with AnnotationWriter(path, annotator='ant-starcat', run_id='sc-1',
                          catalog='MICROGLIA_V1') as w:
        w.add_categorical('starcat__MICROGLIA_V1__Identity_Label', values)
        w.commit()

    cols = duckdb_obs_columns(path)
    assert 'starcat__MICROGLIA_V1__Identity_Label' in cols['obscat']

    con = duckdb.connect(str(path), read_only=True)
    try:
        result = con.execute(
            'SELECT "starcat__MICROGLIA_V1__Identity_Label" AS lbl, '
            'COUNT(*) AS n FROM obscat GROUP BY lbl ORDER BY lbl'
        ).fetchdf()
    finally:
        con.close()
    assert dict(zip(result['lbl'], result['n'])) == {'A': 10, 'B': 10}


def test_add_to_dataset_without_obs_tables(tmp_path):
    """When __obsnum doesn't exist yet, the writer creates it."""
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path, with_obs_tables=False)

    values = pd.Series(
        {name: float(i) for i, name in enumerate(CELL_NAMES)},
        name='first_score',
    )
    with AnnotationWriter(path, annotator='ant-score', run_id='ws-2') as w:
        w.add_numerical('first_score', values)
        w.commit()

    cols = duckdb_obs_columns(path)
    assert cols['obsnum'] == ['first_score']

    con = duckdb.connect(str(path), read_only=True)
    try:
        n_rows = con.execute("SELECT COUNT(*) FROM obsnum").fetchone()[0]
    finally:
        con.close()
    assert n_rows == len(CELL_NAMES)


# ---------- writer rollback ----------

def test_rollback_on_exception(tmp_path):
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    values = pd.Series({name: float(i) for i, name in enumerate(CELL_NAMES)})

    with pytest.raises(RuntimeError, match="boom"):
        with AnnotationWriter(path, annotator='ant-score', run_id='ws-3') as w:
            w.add_numerical('will_be_rolled_back', values)
            raise RuntimeError("boom")

    cols = duckdb_obs_columns(path)
    assert 'will_be_rolled_back' not in cols['obsnum']
    pending = read_pending_cols(path)
    assert len(pending) == 0


def test_rollback_when_commit_not_called(tmp_path):
    """Exit without commit() also rolls back."""
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    values = pd.Series({name: float(i) for i, name in enumerate(CELL_NAMES)})

    with AnnotationWriter(path, annotator='ant-score', run_id='ws-4') as w:
        w.add_numerical('also_rolled_back', values)
        # No commit!

    cols = duckdb_obs_columns(path)
    assert 'also_rolled_back' not in cols['obsnum']


# ---------- conflict handling ----------

def test_conflict_fail_default(tmp_path):
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    values = pd.Series({name: float(i) for i, name in enumerate(CELL_NAMES)})

    with AnnotationWriter(path, annotator='a', run_id='r1') as w:
        w.add_numerical('foo', values)
        w.commit()

    # Second writer hits an existing column
    with pytest.raises(AnnotationWriterError, match="already exists"):
        with AnnotationWriter(path, annotator='a', run_id='r2') as w:
            w.add_numerical('foo', values)  # on_conflict='fail' default
            w.commit()


def test_conflict_overwrite(tmp_path):
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    v1 = pd.Series({name: 1.0 for name in CELL_NAMES})
    v2 = pd.Series({name: 2.0 for name in CELL_NAMES})

    with AnnotationWriter(path, annotator='a', run_id='r1') as w:
        w.add_numerical('foo', v1)
        w.commit()
    with AnnotationWriter(path, annotator='a', run_id='r2') as w:
        w.add_numerical('foo', v2, on_conflict='overwrite')
        w.commit()

    con = duckdb.connect(str(path), read_only=True)
    try:
        vals = con.execute('SELECT foo FROM obsnum').fetchdf()['foo'].tolist()
    finally:
        con.close()
    assert set(vals) == {2.0}


# ---------- alignment checks ----------

def test_unknown_cell_names_fail(tmp_path):
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    values = pd.Series({'not_a_real_cell': 1.0, 'cell_0000': 2.0})

    with pytest.raises(AnnotationWriterError, match="not in __cells"):
        with AnnotationWriter(path, annotator='a', run_id='r1') as w:
            w.add_numerical('foo', values)


def test_partial_cells_fills_null(tmp_path):
    """Annotator writes only some cells — others should be NULL."""
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    values = pd.Series({CELL_NAMES[0]: 42.0, CELL_NAMES[1]: 43.0})

    with AnnotationWriter(path, annotator='a', run_id='r1') as w:
        w.add_numerical('partial', values)
        w.commit()

    con = duckdb.connect(str(path), read_only=True)
    try:
        df = con.execute(
            'SELECT cell_name, partial FROM obsnum ORDER BY cell_name'
        ).fetchdf()
    finally:
        con.close()
    nulls = df['partial'].isna().sum()
    assert nulls == 18
    assert df.loc[df['cell_name'] == CELL_NAMES[0], 'partial'].iloc[0] == 42.0


# ---------- categorical guards ----------

def test_high_cardinality_categorical_refused(tmp_path):
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    # 20 unique values × 20 cells, but max_cardinality=5 to force the check
    values = pd.Series({name: f"u{i}" for i, name in enumerate(CELL_NAMES)})
    with pytest.raises(AnnotationWriterError, match="cell-id-like"):
        with AnnotationWriter(path, annotator='a', run_id='r1') as w:
            w.add_categorical('barcodes', values, max_cardinality=5)


# ---------- lock ----------

def test_locking_blocks_concurrent_writer(tmp_path):
    """Second concurrent writer fails fast."""
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)
    AnnotationWriter.LOCK_TIMEOUT_SECONDS = 1  # speed up the test

    values = pd.Series({name: 1.0 for name in CELL_NAMES})

    with AnnotationWriter(path, annotator='a', run_id='r1') as w1:
        w1.add_numerical('first', values)
        # Second writer must fail while w1 is open
        with pytest.raises(AnnotationWriterError, match="Could not acquire lock"):
            AnnotationWriter(path, annotator='a', run_id='r2').__enter__()
        w1.commit()


# ---------- pending state diagnosis (used by ant-convert guard) ----------

def test_diagnosis_clean(tmp_path):
    """Fresh duckdb with no pending cols and all duckdb obs cols present in h5ad."""
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    # h5ad has the same obs cols as the duckdb (sample, n_counts) — clean
    diag = pending_state_diagnosis(path, h5ad_obs_keys={'sample', 'n_counts'})
    assert diag['clean']
    assert diag['pending_table'].empty
    assert diag['only_in_duckdb'] == []
    assert diag['unrecorded'] == []


def test_diagnosis_dirty_recorded(tmp_path):
    """Column added via AnnotationWriter → in __pending_cols AND in column diff."""
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    values = pd.Series({name: float(i) for i, name in enumerate(CELL_NAMES)})
    with AnnotationWriter(path, annotator='a', run_id='r1') as w:
        w.add_numerical('new_score', values)
        w.commit()

    diag = pending_state_diagnosis(path, h5ad_obs_keys={'sample', 'n_counts'})
    assert not diag['clean']
    assert 'new_score' in diag['pending_table']['col_name'].tolist()
    assert diag['only_in_duckdb'] == ['new_score']
    assert diag['unrecorded'] == []  # recorded in __pending_cols → not unrecorded


def test_diagnosis_unrecorded_divergence(tmp_path):
    """Column added directly via raw SQL (bypassing AnnotationWriter) → flagged
    as unrecorded — this is the belt-and-suspenders case for ant-convert."""
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)

    con = duckdb.connect(str(path), read_only=False)
    con.execute('ALTER TABLE __obsnum ADD COLUMN sneaky_col DOUBLE')
    con.close()

    diag = pending_state_diagnosis(path, h5ad_obs_keys={'sample', 'n_counts'})
    assert not diag['clean']
    assert diag['only_in_duckdb'] == ['sneaky_col']
    assert diag['unrecorded'] == ['sneaky_col']
    assert diag['pending_table'].empty


def test_refuses_when_rebuilding_sentinel_present(tmp_path):
    path = tmp_path / "test.duckdb"
    build_synthetic_duckdb(path)
    sentinel = path.with_suffix(path.suffix + '.rebuilding')
    sentinel.write_text('rebuilding...')

    with pytest.raises(AnnotationWriterError, match="being rebuilt"):
        AnnotationWriter(path, annotator='a', run_id='r1').__enter__()
