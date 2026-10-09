"""Chunked UCell scoring (``bin/annot/ant-ucell``): same scores for any chunk size, read from the duckdb.

Builds a small synthetic store with the layout of ``test_celltype_scope`` (legacy in-duckdb
layer table), plus a by-cell parquet sidecar variant, and compares the tool with a plain
per-cell reference implementation of UCell.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
from scipy.stats import rankdata

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_celltype_scope import N_GENES, build_dataset, build_genesetstore, load_tool, scan  # noqa: E402

from celltype_scope import cell_type_groups  # noqa: E402
from scores_parquet import read_scores  # noqa: E402

MODULE_GENES = [[0, 1, 2, 3, 4, 5], [3, 7, 9, 11, 13], [20, 21, 22, 23, 24, 25]]
TOP_RANK = 1500


def reference_ucell(expression: np.ndarray, gene_columns: list[int]) -> np.ndarray:
    """UCell of every cell, one cell at a time (maxRank 1500, average ties, descending ranks)."""
    scores = []
    for cell_expression in expression.astype('float32'):
        ranks = np.minimum(rankdata(-cell_expression, method='average'), TOP_RANK + 1)
        gene_count = len(gene_columns)
        rank_sum = ranks[gene_columns].sum()
        scores.append(1.0 - (rank_sum - gene_count * (gene_count + 1) / 2.0) / (gene_count * TOP_RANK))
    return np.array(scores)


@pytest.fixture
def tool():
    return load_tool('ant-ucell')


def scored(tool, entry, module_columns, chunk_cells):
    source = tool.ExpressionSource(entry.duckdb_path, 'X')
    try:
        blocks = list(tool._run_ucell(source, np.arange(40), [np.array(c) for c in module_columns], chunk_cells))
    finally:
        source.close()
    return np.concatenate([ids for ids, _ in blocks]), np.vstack([block for _, block in blocks])


def test_scores_match_the_per_cell_reference(tool, tmp_path):
    _store, entry, _names, expression = build_dataset(tmp_path)
    ids, scores = scored(tool, entry, MODULE_GENES, 40)
    assert ids.tolist() == list(range(40))
    for position, columns in enumerate(MODULE_GENES):
        np.testing.assert_allclose(scores[:, position], reference_ucell(expression, columns), rtol=1e-6)


@pytest.mark.parametrize('chunk_cells', [1, 3, 7, 39, 40, 41, 1000])
def test_scores_are_identical_for_every_chunk_size(tool, tmp_path, chunk_cells):
    _store, entry, _names, _expression = build_dataset(tmp_path)
    _ids, reference = scored(tool, entry, MODULE_GENES, 40)
    ids, chunked = scored(tool, entry, MODULE_GENES, chunk_cells)
    assert ids.tolist() == list(range(40))
    np.testing.assert_array_equal(chunked, reference)


def test_a_chunk_never_exceeds_the_requested_size(tool, tmp_path):
    _store, entry, _names, _expression = build_dataset(tmp_path)
    source = tool.ExpressionSource(entry.duckdb_path, 'X')
    sizes = [len(ids) for ids, _ in tool._run_ucell(source, np.arange(40), [np.array([0, 1, 2])], 15)]
    source.close()
    assert sizes == [15, 15, 10]


def test_scattered_cells_are_read_by_id_not_by_range(tool, tmp_path):
    _store, entry, _names, expression = build_dataset(tmp_path)
    wanted = np.array([2, 5, 17, 33])
    source = tool.ExpressionSource(entry.duckdb_path, 'X')
    (ids, scores), = tool._run_ucell(source, wanted, [np.array(MODULE_GENES[0])], 100)
    source.close()
    assert ids.tolist() == wanted.tolist()
    np.testing.assert_allclose(scores[:, 0], reference_ucell(expression[wanted], MODULE_GENES[0]), rtol=1e-6)



def test_a_cell_without_expression_scores_as_all_ties(tool, tmp_path):
    _store, entry, _names, expression = build_dataset(tmp_path)
    conn = duckdb.connect(str(entry.duckdb_path))
    conn.execute('DELETE FROM __layer__X WHERE cell_id = 4')
    conn.close()
    expression = expression.copy()
    expression[4] = 0
    _ids, scores = scored(tool, entry, [MODULE_GENES[0]], 9)
    np.testing.assert_allclose(scores[:, 0], reference_ucell(expression, MODULE_GENES[0]), rtol=1e-6)


def test_the_by_cell_parquet_sidecar_gives_the_same_scores(tool, tmp_path):
    _store, entry, _names, _expression = build_dataset(tmp_path)
    _ids, from_table = scored(tool, entry, MODULE_GENES, 11)
    conn = duckdb.connect(str(entry.duckdb_path))
    sidecar = entry.duckdb_path.with_name('Syn.layer.X.by_cell.parquet')
    conn.execute(f"COPY (SELECT * FROM __layer__X ORDER BY cell_id, gene_id) TO '{sidecar}' (FORMAT PARQUET)")
    conn.execute('DROP TABLE __layer__X')
    conn.close()
    _ids, from_parquet = scored(tool, entry, MODULE_GENES, 11)
    np.testing.assert_array_equal(from_parquet, from_table)


def test_resolve_genes_maps_present_symbols_to_columns(tool, tmp_path):
    _store, entry, _names, _expression = build_dataset(tmp_path)
    source = tool.ExpressionSource(entry.duckdb_path, 'X')
    columns = source.resolve_genes(['GENE3', 'GENE0', 'NOT_A_GENE', 'GENE3'])
    source.close()
    assert columns == {'GENE3': 3, 'GENE0': 0} and source.n_genes == N_GENES


def test_long_scores_layout(tool):
    blocks = [(np.array([4, 9]), np.array([[0.1, 0.2], [0.3, 0.4]]), ['a', 'b']),
              (np.array([12]), np.array([[0.5, 0.6]]), ['a', 'b'])]
    long = tool._long_scores(blocks).sort_values(['module', 'cell_id']).reset_index(drop=True)
    assert long['cell_id'].tolist() == [4, 9, 12, 4, 9, 12]
    assert long['module'].tolist() == ['a'] * 3 + ['b'] * 3
    assert long['score'].tolist() == [0.1, 0.3, 0.5, 0.2, 0.4, 0.6]


def test_experiments_write_each_catalog_once_and_match_unchunked(tool, tmp_path, monkeypatch):
    store, entry, _names, _expression = build_dataset(tmp_path)
    registry = scan(build_genesetstore(tmp_path))
    cells = cell_type_groups(entry, store)
    experiments = [(registry.get_experiment(name), [g for g in registry.list_genesets()
                                                    if g.experiment.experiment_id == name])
                   for name in ('Syn/Micro', 'Syn/Everywhere', 'Syn/Panel')]
    writes = []
    original = tool.upsert_scores
    monkeypatch.setattr(tool, 'upsert_scores', lambda **kwargs: writes.append(kwargs['catalog']) or original(**kwargs))
    source = tool.ExpressionSource(entry.duckdb_path, 'X')
    results = tool._ucell_experiments(source, experiments, entry, 3, True, False, cells, chunk_cells=6)
    assert [r['status'] for r in results] == ['ok'] * 3
    assert sorted(writes) == ['Syn/Everywhere', 'Syn/Micro', 'Syn/Panel']
    chunked = read_scores(entry.duckdb_path, source='ucell').sort_values(['catalog', 'module', 'cell_id'])
    tool._ucell_experiments(source, experiments, entry, 3, False, False, cells, chunk_cells=1000)
    source.close()
    whole = read_scores(entry.duckdb_path, source='ucell').sort_values(['catalog', 'module', 'cell_id'])
    pd.testing.assert_frame_equal(chunked.reset_index(drop=True), whole.reset_index(drop=True))


def test_dry_run_writes_nothing_and_modules_below_min_n_are_an_error(tool, tmp_path):
    store, entry, _names, _expression = build_dataset(tmp_path)
    registry = scan(build_genesetstore(tmp_path))
    cells = cell_type_groups(entry, store)
    members = [g for g in registry.list_genesets() if g.experiment.experiment_id == 'Syn/Everywhere']
    source = tool.ExpressionSource(entry.duckdb_path, 'X')
    experiment = registry.get_experiment('Syn/Everywhere')
    dry = tool._ucell_experiments(source, [(experiment, members)], entry, 3, False, True, cells)[0]
    too_strict = tool._ucell_experiments(source, [(experiment, members)], entry, 99, False, False, cells)[0]
    source.close()
    assert dry['status'] == 'dry-run' and dry['n_rows'] == 40
    assert too_strict['status'] == 'error' and 'min_n' in too_strict['error']
    assert read_scores(entry.duckdb_path).empty


def test_main_rejects_a_chunk_size_below_one(tool, tmp_path, monkeypatch):
    store, _entry, _names, _expression = build_dataset(tmp_path)
    geneset_root = build_genesetstore(tmp_path)
    monkeypatch.setattr(tool, '_load_geneset_registry', lambda: scan(geneset_root))
    monkeypatch.setattr(tool, '_h5adstore_root', lambda: store)
    monkeypatch.setattr(sys, 'argv', ['ant-ucell', 'SY1/SYN', 'Syn/Everywhere', '--chunk-cells', '0'])
    with pytest.raises(SystemExit, match='at least 1'):
        tool.main()


def test_main_end_to_end_with_small_chunks(tool, tmp_path, monkeypatch, capsys):
    store, entry, _names, _expression = build_dataset(tmp_path)
    geneset_root = build_genesetstore(tmp_path)
    monkeypatch.setattr(tool, '_load_geneset_registry', lambda: scan(geneset_root))
    monkeypatch.setattr(tool, '_h5adstore_root', lambda: store)
    monkeypatch.setattr(sys, 'argv', ['ant-ucell', 'SY1/SYN', 'Syn/Micro', 'Syn/Everywhere', '--min-n', '3',
                                      '--chunk-cells', '9', '--overwrite'])
    assert tool.main() == 0
    rows = read_scores(entry.duckdb_path, source='ucell')
    assert set(rows['catalog']) == {'Syn/Micro', 'Syn/Everywhere'} and len(rows) == 20 + 40
    assert 'chunks of 9 cells' in capsys.readouterr().out
