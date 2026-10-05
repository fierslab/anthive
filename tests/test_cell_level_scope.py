"""Tests for ``lib/cell_level_scope.py`` and the donor/bulk handling of
``bin/annot/ant-gene-variance-precompute``.

Run with:
    uv run --with pytest --with duckdb --with pandas --with numpy \
           --with pyarrow --with pyyaml --with tqdm --no-project \
           env PYTHONPATH=lib python -m pytest tests/test_cell_level_scope.py -q
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))

import cell_level_scope  # noqa: E402
import mus  # noqa: E402


def _load_gvp():
    """Load the uv-script ``ant-gene-variance-precompute`` as a module."""
    path = REPO_ROOT / 'bin' / 'annot' / 'ant-gene-variance-precompute'
    loader = importlib.machinery.SourceFileLoader('ant_gene_variance_precompute', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


gvp = _load_gvp()


# --------------------------------------------------------------------------- #
# lib/cell_level_scope.py                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('column,expected', [
    ('ant_donor', True), ('ant_donor_uid', True), ('donor', True), ('Donor_ID', True),
    ('ant_patient', True), ('subject_id', True), ('ant_author_donor', True),
    ('ant_donor_age', False), ('ant_sex', False), ('ant_braak', False), ('', False),
])
def test_is_donor_like(column, expected):
    assert cell_level_scope.is_donor_like(column) is expected


@pytest.mark.parametrize('annotation,expected', [
    ({'technology': 'bulk_RNA-seq'}, True),
    ({'technology': ['10x_chromium_3p_v3', 'Bulk RNA-seq']}, True),
    ({'technology_category': 'bulk transcriptomics'}, True),
    ({'technology': '10x_chromium_3p_v3'}, False),
    ({'technology_category': 'single-cell/nucleus transcriptomics'}, False),
    ({}, False),
])
def test_bulk_technology(annotation, expected):
    assert bool(cell_level_scope.bulk_technology(annotation)) is expected


@pytest.mark.parametrize('n_obs,n_donors,skipped', [
    (7, 7, True),          # H26/EBP
    (49, 49, True),
    (15, 10, True),        # exactly 1.5 per donor
    (16, 10, False),       # just above
    (130000, 31, False),   # single cell
    (50, None, False),     # no donor column: the counts cannot decide
    (None, 5, False),
])
def test_one_observation_per_donor_boundaries(n_obs, n_donors, skipped):
    reason = cell_level_scope.scope_skip_reason({}, n_obs, n_donors)
    assert (reason is not None) is skipped
    if skipped:
        assert 'about one observation per donor' in reason


def test_scope_skip_reason_prefers_technology():
    reason = cell_level_scope.scope_skip_reason({'technology': 'bulk_RNA-seq'}, 100000, 30)
    assert reason.startswith('bulk data (technology bulk_RNA-seq)')


def test_skip_summary_merge_keeps_unprocessed_and_clears_resolved(tmp_path):
    path = cell_level_scope.skip_summary_path(tmp_path / 'agg.parquet')
    assert path.name == 'agg.skipped.json'
    assert cell_level_scope.read_skip_summary(path) == {}
    cell_level_scope.merge_skip_summary(path, ['A/1', 'B/2'], {'A/1': 'bulk', 'B/2': 'bulk too'})
    merged = cell_level_scope.merge_skip_summary(path, ['A/1', 'C/3'], {'C/3': 'new'})
    assert merged == {'B/2': 'bulk too', 'C/3': 'new'}     # A/1 re-evaluated and in scope again
    assert json.loads(path.read_text()) == merged


def test_read_skip_summary_tolerates_garbage(tmp_path):
    path = tmp_path / 'x.skipped.json'
    path.write_text('not json')
    assert cell_level_scope.read_skip_summary(path) == {}
    path.write_text('[1, 2]')
    assert cell_level_scope.read_skip_summary(path) == {}


# --------------------------------------------------------------------------- #
# ant-gene-variance-precompute                                                #
# --------------------------------------------------------------------------- #

N_CELLS = 24


def _build_dataset(directory: Path, donor_values, donor_in_obsnum: bool,
                   age_values=None) -> Path:
    """Tiny duckdb with __cells, obs views, __genes and a per-gene layer table."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'sample.duckdb'
    conn = duckdb.connect(str(path))
    try:
        cells = pd.DataFrame({'cell_id': range(N_CELLS), 'cell_name': [f'c{i}' for i in range(N_CELLS)]})
        conn.register('t_cells', cells)
        conn.execute('CREATE TABLE __cells AS SELECT * FROM t_cells')
        numeric = {'ant_age': age_values or [float(i) for i in range(N_CELLS)]}
        categorical = {'ant_sex': ['m' if i % 2 else 'f' for i in range(N_CELLS)]}
        (numeric if donor_in_obsnum else categorical)['ant_donor'] = list(donor_values)
        for view, columns, sql_type in (('obsnum', numeric, 'DOUBLE'), ('obscat', categorical, 'VARCHAR')):
            frame = pd.DataFrame({'cell_id': range(N_CELLS), **columns})
            conn.register('t_obs', frame)
            conn.execute(f'CREATE TABLE __{view} AS SELECT * FROM t_obs')
            conn.unregister('t_obs')
            conn.execute(f'CREATE VIEW {view} AS SELECT c.cell_name, o.* EXCLUDE (cell_id) '
                         f'FROM __{view} o JOIN __cells c ON c.cell_id = o.cell_id')
        conn.execute("CREATE TABLE __genes (gene_name VARCHAR, gene_id INTEGER)")
        conn.execute("INSERT INTO __genes VALUES ('G1', 0)")
        layer = pd.DataFrame({'cell_id': range(N_CELLS), 'gene_id': 0,
                              'value': [float(i % 5) for i in range(N_CELLS)]})
        conn.register('t_layer', layer)
        conn.execute('CREATE TABLE "__layer__X" AS SELECT * FROM t_layer')
    finally:
        conn.close()
    return path


def _inputs(duckdb_path: Path, with_h5ad: bool = True):
    return gvp.DatasetInputs(
        db_id='TST/T1', duckdb_path=duckdb_path, md_path=duckdb_path.with_suffix('.md'),
        layer='X', by_gene_parquet=duckdb_path.with_name('missing.parquet'), source_mtime=0.0,
        h5ad_path=duckdb_path.with_suffix('.h5ad') if with_h5ad else None, store_root=duckdb_path.parent)


def test_numeric_donor_column_is_categorical_not_quintiles(tmp_path):
    donors = [float(i % 6 + 1) for i in range(N_CELLS)]
    path = _build_dataset(tmp_path / 'ds', donors, donor_in_obsnum=True)
    conn = duckdb.connect(str(path), read_only=True)
    try:
        facets = {facet[0]: facet for facet in gvp._discover_facets(conn, 50)}
    finally:
        conn.close()
    assert facets['ant_donor'][1:] == ('categorical', N_CELLS, 6, 'obsnum')
    assert facets['ant_age'][1] == 'numerical' and facets['ant_age'][3] == 5   # real continuous: still binned
    rows = gvp._compute_rows_for_dataset(_inputs(path), [facets['ant_donor']], ['G1'], 50)
    assert len(rows) == 1
    assert rows[0]['facet_type'] == 'categorical' and rows[0]['n_groups'] == 6
    groups = set(json.loads(rows[0]['group_means_json']))
    assert groups == {'1', '2', '3', '4', '5', '6'}        # no q1..q5, no '1.0'


def test_high_cardinality_donor_column_is_still_capped(tmp_path):
    path = _build_dataset(tmp_path / 'ds', [float(i) for i in range(N_CELLS)], donor_in_obsnum=True)
    conn = duckdb.connect(str(path), read_only=True)
    try:
        names = [facet[0] for facet in gvp._discover_facets(conn, max_cardinality=10)]
    finally:
        conn.close()
    assert 'ant_donor' not in names and 'ant_age' in names


def test_text_donor_column_unchanged(tmp_path):
    path = _build_dataset(tmp_path / 'ds', [f'D{i % 4}' for i in range(N_CELLS)], donor_in_obsnum=False)
    conn = duckdb.connect(str(path), read_only=True)
    try:
        facets = {facet[0]: facet for facet in gvp._discover_facets(conn, 50)}
    finally:
        conn.close()
    assert facets['ant_donor'][1:] == ('categorical', N_CELLS, 4, 'obscat')


def test_bulk_dataset_is_skipped_by_technology(tmp_path):
    path = _build_dataset(tmp_path / 'ds', [f'D{i % 3}' for i in range(N_CELLS)], donor_in_obsnum=False)
    mus.write_mus_file(path.with_suffix('.h5ad.mus'), {'technology': 'bulk_RNA-seq'})
    assert 'bulk' in gvp.dataset_skip_reason(_inputs(path))


def test_one_observation_per_donor_dataset_is_skipped(tmp_path):
    path = _build_dataset(tmp_path / 'ds', [f'D{i}' for i in range(N_CELLS)], donor_in_obsnum=False)
    assert 'about one observation per donor' in gvp.dataset_skip_reason(_inputs(path))


def test_single_cell_dataset_is_in_scope(tmp_path):
    path = _build_dataset(tmp_path / 'ds', [f'D{i % 3}' for i in range(N_CELLS)], donor_in_obsnum=False)
    assert gvp.dataset_skip_reason(_inputs(path)) is None


def test_process_dataset_writes_empty_cache_for_skipped_dataset(tmp_path):
    path = _build_dataset(tmp_path / 'ds', [f'D{i}' for i in range(N_CELLS)], donor_in_obsnum=False)
    cache_path = tmp_path / 'cache' / 'genevar.parquet'
    job = gvp.DatasetJob(inputs=_inputs(path), hot_genes=('G1',), cache_path=cache_path,
                         max_facet_cardinality=50)
    result = gvp._process_dataset(job, force=False)
    assert result.error is None and result.n_rows == 0
    assert result.skipped_reason and 'about one observation per donor' in result.skipped_reason
    assert len(pd.read_parquet(cache_path)) == 0


def test_process_dataset_computes_in_scope_dataset(tmp_path):
    path = _build_dataset(tmp_path / 'ds', [float(i % 3 + 1) for i in range(N_CELLS)], donor_in_obsnum=True)
    cache_path = tmp_path / 'cache' / 'genevar.parquet'
    job = gvp.DatasetJob(inputs=_inputs(path), hot_genes=('G1',), cache_path=cache_path,
                         max_facet_cardinality=50)
    result = gvp._process_dataset(job, force=True)
    assert result.error is None and result.skipped_reason is None
    frame = pd.read_parquet(cache_path)
    assert set(frame['facet']) == {'ant_age', 'ant_donor', 'ant_sex'}
    assert frame.loc[frame['facet'] == 'ant_donor', 'facet_type'].iloc[0] == 'categorical'
