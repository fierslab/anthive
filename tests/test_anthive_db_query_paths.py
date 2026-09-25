"""Correctness tests for the optimised AnthiveDB query paths.

Covers obs_summary (single-pass aggregates, batched top-N, memo +
invalidation), gene_stats path (c) (per-gene means aggregated on gene_id),
get_cell_table with score-sidecar columns (one read) and filters (score
columns, numeric-looking categorical values, unknown fields), score_module
zero-expression cells, and AnnotationWriter atomicity.

Every expectation is computed independently in numpy/pandas from the same
synthetic inputs, not from the code under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))

import anthive_db  # noqa: E402
from anthive_db import (  # noqa: E402
    AnnotationWriter,
    AnnotationWriterError,
    AnthiveDB,
    build_obs_view_sql,
    clear_obs_summary_memo,
    read_pending_cols,
)

N_CELLS = 40
CELL_NAMES = [f"cell_{i:03d}" for i in range(N_CELLS)]
GENE_NAMES = ['APOE', 'TREM2', 'CX3CR1', 'P2RY12', 'CTRL1', 'CTRL2', 'CTRL3', 'SILENT']

API_CONFIG = {
    'obs_rename': {'donor_raw': 'ant_donor', 'age_raw': 'ant_age'},
    'obs_hidden': ['secret'],
    'obs_value_rename': {},
    'obs_value_hidden': {},
    'obs_derive': {},
    'obs_constant': {},
}


def _layer_values() -> pd.DataFrame:
    """Sparse (cell_id, gene_id, value): deterministic, zeros omitted.

    Cell 0 has no expression at all (the score_module zero case)."""
    rng = np.random.default_rng(7)
    rows = []
    for cell_id in range(1, N_CELLS):
        for gene_id in range(len(GENE_NAMES) - 1):   # SILENT never expressed
            if rng.random() < 0.6:
                rows.append((cell_id, gene_id, float(np.round(rng.gamma(2.0, 1.0), 4))))
    return pd.DataFrame(rows, columns=['cell_id', 'gene_id', 'value'])


def _obs_frames():
    rng = np.random.default_rng(11)
    obsnum = pd.DataFrame({
        'cell_id': range(N_CELLS),
        'age_raw': rng.integers(20, 90, N_CELLS).astype(float),
        'n_counts': rng.normal(1000, 200, N_CELLS),
        'secret': rng.normal(0, 1, N_CELLS),
    })
    obsnum.loc[[3, 7, 11], 'n_counts'] = np.nan
    obscat = pd.DataFrame({
        'cell_id': range(N_CELLS),
        'donor_raw': [f"D{i % 4}" for i in range(N_CELLS)],
        # numeric-looking labels stored as text, plus a non-numeric one
        'batch': ['3' if i % 3 == 0 else ('7' if i % 3 == 1 else 'ref') for i in range(N_CELLS)],
        'tie_col': ['a', 'b'] * (N_CELLS // 2),
    })
    obscat.loc[[0, 1], 'donor_raw'] = None
    return obsnum, obscat


def _scores_frame() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    rows = []
    for source, catalog, module in [('geneset', 'Study/WGCNA', 'Blue'),
                                    ('geneset', 'Study/WGCNA', 'Green'),
                                    ('ucell', 'Other/set', 'm1')]:
        for cell_id in range(N_CELLS):
            if cell_id == 5 and module == 'Green':
                continue                          # missing row -> NaN
            rows.append((cell_id, source, catalog, module, float(rng.normal())))
    return pd.DataFrame(rows, columns=['cell_id', 'source', 'catalog', 'module', 'score'])


def build_dataset(directory: Path, with_gene_bins: bool = False) -> Path:
    path = directory / 'ds.duckdb'
    con = duckdb.connect(str(path))
    con.register('t_cells', pd.DataFrame({'cell_id': range(N_CELLS), 'cell_name': CELL_NAMES}))
    con.execute("CREATE TABLE __cells (cell_id INTEGER PRIMARY KEY, cell_name VARCHAR NOT NULL UNIQUE)")
    con.execute("INSERT INTO __cells SELECT * FROM t_cells")
    con.execute("CREATE VIEW cells AS SELECT * FROM __cells")
    con.register('t_genes', pd.DataFrame({'gene_id': range(len(GENE_NAMES)), 'gene_name': GENE_NAMES}))
    con.execute("CREATE TABLE __genes (gene_id INTEGER PRIMARY KEY, gene_name VARCHAR NOT NULL UNIQUE)")
    con.execute("INSERT INTO __genes SELECT * FROM t_genes")

    obsnum, obscat = _obs_frames()
    con.register('t_num', obsnum)
    con.execute("CREATE TABLE __obsnum AS SELECT * FROM t_num")
    con.register('t_cat', obscat)
    con.execute("CREATE TABLE __obscat AS SELECT * FROM t_cat")
    con.execute(build_obs_view_sql('obsnum', '__obsnum', list(obsnum.columns), API_CONFIG,
                                   '__obscat', list(obscat.columns)))
    con.execute(build_obs_view_sql('obscat', '__obscat', list(obscat.columns), API_CONFIG,
                                   '__obsnum', list(obsnum.columns)))

    layer = _layer_values()
    con.register('t_layer', layer)
    by_gene = directory / 'ds.layer.X.by_gene.parquet'
    con.execute(f"COPY (SELECT * FROM t_layer ORDER BY gene_id, cell_id) TO '{by_gene}' (FORMAT PARQUET)")

    if with_gene_bins:
        # module genes APOE/TREM2 in bin 0 with the CTRL genes; others bin 1
        bins = pd.DataFrame({
            'gene_id': range(len(GENE_NAMES)),
            'mean_expr': [1.0] * len(GENE_NAMES),
            'expr_bin': [0, 0, 1, 1, 0, 0, 0, 1],
        })
        con.register('t_bins', bins)
        con.execute("CREATE TABLE __gene_bins AS SELECT * FROM t_bins")

    con.register('t_scores', _scores_frame())
    scores_path = directory / 'ds.scores.parquet'
    con.execute(f"COPY t_scores TO '{scores_path}' (FORMAT PARQUET)")
    con.close()
    return path


@pytest.fixture
def dataset(tmp_path):
    clear_obs_summary_memo()
    return build_dataset(tmp_path)


@pytest.fixture
def db(dataset):
    handle = AnthiveDB(dataset)
    yield handle
    handle.close()


# ── obs_summary ───────────────────────────────────────────────────────────


def test_obs_summary_numeric_stats_are_exact(db):
    obsnum, _ = _obs_frames()
    summary = db.obs_summary()
    for public, raw in (('ant_age', 'age_raw'), ('n_counts', 'n_counts')):
        values = obsnum[raw].dropna().to_numpy()
        entry = summary[public]
        assert entry['type'] == 'num'
        assert entry['min'] == pytest.approx(values.min())
        assert entry['max'] == pytest.approx(values.max())
        assert entry['mean'] == pytest.approx(values.mean())
        for key, prob in (('q05', 0.05), ('q25', 0.25), ('median', 0.5),
                          ('q75', 0.75), ('q95', 0.95)):
            assert entry[key] == pytest.approx(np.quantile(values, prob)), key
    assert summary['n_counts']['pct_missing'] == round(100 * 3 / N_CELLS, 2)


def test_obs_summary_respects_view_rename_and_hide(db):
    summary = db.obs_summary()
    assert 'ant_donor' in summary and 'ant_age' in summary
    assert 'donor_raw' not in summary and 'age_raw' not in summary
    assert 'secret' not in summary
    assert 'cell_id' not in summary and 'cell_name' not in summary


def test_obs_summary_categorical_counts_and_top(db):
    _, obscat = _obs_frames()
    summary = db.obs_summary(top_n_categories=2)
    donor = summary['ant_donor']
    non_null = obscat['donor_raw'].dropna()
    assert donor['type'] == 'cat'
    assert donor['n_unique'] == non_null.nunique()
    assert donor['pct_missing'] == round(100 * 2 / N_CELLS, 2)
    expected = non_null.value_counts()
    expected = sorted(expected.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
    assert [(t['value'], t['count']) for t in donor['top']] == expected
    assert donor['top'][0]['pct'] == round(100 * expected[0][1] / len(non_null), 2)
    # exact tie (20 vs 20): value order decides, deterministically
    assert [t['value'] for t in summary['tie_col']['top']] == ['a', 'b']


def test_obs_summary_includes_score_sidecar(db):
    scores = _scores_frame()
    summary = db.obs_summary()
    blue = scores[(scores.module == 'Blue')]['score'].to_numpy()
    entry = summary['Study::WGCNA::Blue']
    assert entry['median'] == pytest.approx(np.median(blue))
    assert entry['q95'] == pytest.approx(np.quantile(blue, 0.95))
    assert 'ucell__Other/set__m1' in summary


def test_obs_summary_memo_hits_and_is_not_poisoned(db, monkeypatch):
    first = db.obs_summary()
    calls = []
    original = AnthiveDB._compute_obs_summary
    monkeypatch.setattr(AnthiveDB, '_compute_obs_summary',
                        lambda self, top_n: calls.append(top_n) or original(self, top_n))
    first['ant_age']['min'] = -999.0          # caller mutation must not leak
    second = db.obs_summary()
    assert calls == []
    assert second['ant_age']['min'] != -999.0
    db.obs_summary(top_n_categories=5)         # different args -> recompute
    assert calls == [5]


def test_obs_summary_memo_invalidated_by_annotation_write(dataset):
    handle = AnthiveDB(dataset)
    assert 'new_score' not in handle.obs_summary()
    handle.close()
    values = pd.Series({name: float(i) for i, name in enumerate(CELL_NAMES)})
    with AnnotationWriter(dataset, annotator='ant-score', run_id='r1') as writer:
        writer.add_numerical('new_score', values)
        writer.commit()
    handle = AnthiveDB(dataset)
    try:
        assert handle.obs_summary()['new_score']['max'] == pytest.approx(N_CELLS - 1)
    finally:
        handle.close()


# ── gene_stats path (c) ───────────────────────────────────────────────────


def test_gene_stats_full_scan_means_match_numpy(db):
    layer = _layer_values()
    stats = db.get_gene_stats(['APOE', 'TREM2', 'SILENT', 'MISSING'])
    dist = db._last_distribution
    expected_means = (layer.groupby('gene_id')['value'].sum() / N_CELLS)
    expected = {GENE_NAMES[gid]: mean for gid, mean in expected_means.items()}
    assert set(dist['means']) == set(expected)          # SILENT absent: no nonzeros
    for name, mean in expected.items():
        assert dist['means'][name] == pytest.approx(mean, rel=1e-12)
    sorted_means = np.sort(np.array(list(expected.values())))
    assert dist['q05'] == pytest.approx(np.quantile(sorted_means, 0.05))
    by_gene = {row['gene']: row for row in stats}
    assert by_gene['MISSING'] == {'gene': 'MISSING', 'error': 'not found'}
    assert by_gene['APOE']['mean_expr_layer'] == pytest.approx(round(expected['APOE'], 6))
    assert by_gene['SILENT']['n_nonzero'] == 0


# ── get_cell_table: scores + filters ──────────────────────────────────────


def test_cell_table_star_includes_all_scores_with_nan_for_missing(db):
    scores = _scores_frame()
    table = db.get_cell_table(metadata_fields=['*'])
    score_cols = ['Study::WGCNA::Blue', 'Study::WGCNA::Green', 'ucell__Other/set__m1']
    assert list(table.columns[-3:]) == score_cols      # deterministic order
    indexed = table.set_index('cell_name')
    for col, module in zip(score_cols, ['Blue', 'Green', 'm1']):
        expected = scores[scores.module == module].set_index('cell_id')['score']
        for cell_id, name in enumerate(CELL_NAMES):
            if cell_id in expected.index:
                assert indexed.at[name, col] == expected[cell_id]
            else:
                assert np.isnan(indexed.at[name, col])
    assert len(table) == N_CELLS


def test_cell_table_filter_on_score_column(db):
    scores = _scores_frame()
    blue = scores[scores.module == 'Blue'].set_index('cell_id')['score']
    table = db.get_cell_table(metadata_fields=['Study::WGCNA::Blue'],
                              filters={'Study::WGCNA::Blue': (0.0, 100.0)})
    expected_names = {CELL_NAMES[cid] for cid, v in blue.items() if 0.0 <= v <= 100.0}
    assert set(table['cell_name']) == expected_names


def test_cell_table_score_filter_combined_with_gene_and_cat_filter(db):
    table = db.get_cell_table(genes=['APOE'], metadata_fields=['ant_donor'],
                              filters={'Study::WGCNA::Green': (-100.0, 100.0), 'ant_donor': 'D2'})
    # cell 5 has no Green row -> NULL -> excluded; D2 = cells with i % 4 == 2
    expected = {CELL_NAMES[i] for i in range(N_CELLS) if i % 4 == 2 and i not in (0, 1)}
    assert set(table['cell_name']) == expected


def test_cell_table_numeric_looking_categorical_value(db):
    # /cells turns "3" into 3.0 before calling get_cell_table
    table = db.get_cell_table(metadata_fields=['batch'], filters={'batch': 3.0})
    assert set(table['batch']) == {'3'}
    assert len(table) == len([i for i in range(N_CELLS) if i % 3 == 0])
    both = db.get_cell_table(metadata_fields=['batch'], filters={'batch': (3.0, 7.0)})
    assert set(both['batch']) == {'3', '7'}


def test_cell_table_cell_name_list_filter(db):
    # plot endpoints pass the sampled embedding cells as filters['cell_name']
    wanted = CELL_NAMES[5:12]
    table = db.get_cell_table(genes=['APOE'], metadata_fields=['batch'],
                              filters={'cell_name': wanted, 'batch': 'ref'})
    expected = {CELL_NAMES[i] for i in range(5, 12) if i % 3 == 2}
    assert set(table['cell_name']) == expected
    single = db.get_cell_table(metadata_fields=['batch'], filters={'cell_name': 'cell_004'})
    assert list(single['cell_name']) == ['cell_004']


def test_cell_table_unknown_filter_field_raises(db):
    with pytest.raises(ValueError, match='Unknown filter field'):
        db.get_cell_table(metadata_fields=['batch'], filters={'no_such_col': 'x'})


def test_cell_table_non_numeric_value_on_numeric_field_raises(db):
    with pytest.raises(ValueError, match='not a number'):
        db.get_cell_table(metadata_fields=['n_counts'], filters={'n_counts': 'abc'})


def test_cell_table_numeric_string_on_numeric_field(db):
    obsnum, _ = _obs_frames()
    target = float(obsnum.loc[4, 'age_raw'])
    table = db.get_cell_table(metadata_fields=['ant_age'], filters={'ant_age': str(target)})
    assert set(table['cell_name']) == set(
        np.array(CELL_NAMES)[obsnum['age_raw'].to_numpy() == target])


# ── score_module ──────────────────────────────────────────────────────────


def test_score_module_scores_every_cell_including_zero_expression(tmp_path):
    clear_obs_summary_memo()
    path = build_dataset(tmp_path, with_gene_bins=True)
    handle = AnthiveDB(path)
    try:
        scores, meta = handle.score_module(['APOE', 'TREM2'], n_control=10)
    finally:
        handle.close()
    assert len(scores) == N_CELLS
    by_name = scores.set_index('cell_name')['module_score']
    assert by_name['cell_000'] == 0.0            # no expression at all
    layer = _layer_values()
    module_ids, control_ids = [0, 1], [4, 5, 6]    # same bin, excluding module genes
    assert meta['n_control_genes'] == len(control_ids)
    for cell_id in (1, 2, 3):
        cell_rows = layer[layer.cell_id == cell_id]
        module_mean = cell_rows[cell_rows.gene_id.isin(module_ids)]['value'].sum() / 2
        control_mean = cell_rows[cell_rows.gene_id.isin(control_ids)]['value'].sum() / 3
        assert by_name[CELL_NAMES[cell_id]] == pytest.approx(module_mean - control_mean)


# ── AnnotationWriter atomicity ────────────────────────────────────────────


def _snapshot(path: Path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        columns = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name IN ('__obsnum', '__obscat') ORDER BY ALL").fetchall()
        values = con.execute("SELECT * FROM __obsnum ORDER BY cell_id").fetchdf()
        views = con.execute(
            "SELECT view_name, sql FROM duckdb_views() WHERE NOT internal ORDER BY 1").fetchall()
        pending = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = '__pending_cols'"
        ).fetchone()[0]
        pending_rows = (con.execute("SELECT * FROM __pending_cols ORDER BY col_name").fetchall()
                        if pending else [])
    finally:
        con.close()
    return columns, values, views, pending_rows


def test_annotation_writer_failure_restores_overwritten_values_and_schema(dataset):
    first = pd.Series({name: 1.0 for name in CELL_NAMES})
    with AnnotationWriter(dataset, annotator='ant-score', run_id='r1') as writer:
        writer.add_numerical('existing_score', first)
        writer.commit()
    before = _snapshot(dataset)

    second = pd.Series({name: 2.0 for name in CELL_NAMES})
    brand_new = pd.Series({name: 3.0 for name in CELL_NAMES})
    with pytest.raises(RuntimeError, match='mid-block'):
        with AnnotationWriter(dataset, annotator='ant-score', run_id='r2') as writer:
            writer.add_numerical('existing_score', second, on_conflict='overwrite')
            writer.add_numerical('brand_new', brand_new)
            raise RuntimeError('mid-block failure')

    after = _snapshot(dataset)
    assert after[0] == before[0]                       # schema
    pd.testing.assert_frame_equal(after[1], before[1])  # values incl. overwritten col
    assert after[2] == before[2]                       # view definitions
    assert after[3] == before[3]                       # __pending_cols (run_id r1)
    assert set(after[1]['existing_score']) == {1.0}


def test_annotation_writer_error_inside_writer_rolls_back_earlier_adds(dataset):
    before = _snapshot(dataset)
    high_cardinality = pd.Series({name: name for name in CELL_NAMES})
    with pytest.raises(AnnotationWriterError):
        with AnnotationWriter(dataset, annotator='ant-annotate', run_id='r3') as writer:
            writer.add_numerical('ok_col', pd.Series({n: 1.0 for n in CELL_NAMES}))
            writer.add_categorical('too_many', high_cardinality, max_cardinality=5)
            writer.commit()
    after = _snapshot(dataset)
    assert after[0] == before[0]
    pd.testing.assert_frame_equal(after[1], before[1])
    assert after[2] == before[2]
    assert len(read_pending_cols(dataset)) == 0


def test_annotation_writer_commit_persists_everything(dataset):
    values = pd.Series({name: float(i) for i, name in enumerate(CELL_NAMES)})
    with AnnotationWriter(dataset, annotator='ant-score', run_id='r4', catalog='C') as writer:
        writer.add_numerical('persisted', values)
        writer.add_categorical('persisted_cat', pd.Series({n: 'x' for n in CELL_NAMES}))
        writer.commit()
        writer.commit()                                 # idempotent
    con = duckdb.connect(str(dataset), read_only=True)
    try:
        assert con.execute("SELECT max(persisted) FROM obsnum").fetchone()[0] == N_CELLS - 1
        assert con.execute("SELECT count(*) FROM obscat WHERE persisted_cat = 'x'").fetchone()[0] == N_CELLS
    finally:
        con.close()
    assert set(read_pending_cols(dataset)['col_name']) == {'persisted', 'persisted_cat'}


def test_annotation_writer_add_after_commit_refused(dataset):
    with AnnotationWriter(dataset, annotator='ant-score', run_id='r5') as writer:
        writer.commit()
        with pytest.raises(AnnotationWriterError, match='already committed'):
            writer.add_numerical('late', pd.Series({n: 1.0 for n in CELL_NAMES}))
