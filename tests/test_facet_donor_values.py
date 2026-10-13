"""Donor-level values and plots behind a facet cell (API 2.45): ``lib/facet_donor_values.py``, the sidecars the test
tools write, the ``/facets/{facet}/donor-values`` and ``donor-plot`` endpoints, the ``links`` of the gene and gene set
cards and the MCP tool.

Sidecars are small synthetic files in the layout ``ant-facet-test`` writes. One test runs the real R pipeline on the
synthetic dataset of ``test_facet_test.py`` (skipped without limma and edgeR) and checks that the values drawn are the
exact voom input: their mean per gene is the stored ``ave_expr``.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import facet_donor_values as dv  # noqa: E402
import facet_results as fr  # noqa: E402
import plotting  # noqa: E402
from test_facet_results import (  # noqa: E402,F401  (fixtures shared with the base facet tests)
    DATASETS, GENES, MCP_PATH, _gene_manifest, _gene_rows, _unit, build_serve, results)
from test_facet_views_api import _module_frame, views  # noqa: E402,F401

CELL_TYPE = 'microglial cell'
PLANTED_GENE = 'PTPRG'
GENE_ID = GENES.index(PLANTED_GENE)
PLANTED_SLOPE = 0.5          # log2 fold change per Braak stage, the fixture's stored logFC of PTPRG in braak
POISON_DONOR = 'ROSMAP_donor_0042'


def write_gene_sidecars(root: Path, group: str, stem: str, facet: str, n_samples: int = 18, seed: int = 3,
                        first_sample: int = 0, poison: bool = False) -> Dict[str, Any]:
    """Write the gene sidecars of one unit; returns the true counts, library sizes and facet values for checks."""
    rng = np.random.default_rng(seed)
    sample_idx = np.arange(first_sample, first_sample + n_samples)
    if facet == 'braak':
        facet_value = rng.integers(0, 7, n_samples).astype(float)
    else:
        facet_value = (np.arange(n_samples) % 2).astype(float)
    library_size = rng.uniform(1.0e6, 2.0e6, n_samples)
    norm_factor = rng.uniform(0.9, 1.1, n_samples)
    samples = pd.DataFrame({
        'facet': facet, 'cell_type': CELL_TYPE, 'celltype_source': 'constant_all_cells',
        'sample_idx': sample_idx, 'facet_value': facet_value,
        'cov_sex': (np.arange(n_samples) % 3 == 0).astype(float),
        'cov_age': rng.normal(0, 1, n_samples), 'cov_pmi': rng.normal(0, 1, n_samples), 'cov_path': np.nan,
        'batch_code': -1, 'n_cells': 20, 'lib_size': library_size, 'norm_factor': norm_factor})
    if poison:
        samples['donor'] = POISON_DONOR
    expected_rate = 2.0 ** (PLANTED_SLOPE * facet_value if facet == 'braak' else 0.5 * facet_value)
    counts = np.round(100 * expected_rate * (library_size * norm_factor / 1.5e6)).astype(int)
    cell_rows = []
    count_rows = []
    for position, sample in enumerate(sample_idx):
        for half in (0, 1):
            cell_id = int(sample) * 2 + half
            cell_rows.append({'sample_idx': int(sample), 'cell_id': cell_id, 'file_index': 0})
            count_rows.append({'cell_id': cell_id, 'gene_id': GENE_ID,
                               'value': float(counts[position] // 2 + (counts[position] % 2 if half == 0 else 0))})
            count_rows.append({'cell_id': cell_id, 'gene_id': 0, 'value': 5.0})
    folder = root / group
    folder.mkdir(parents=True, exist_ok=True)
    samples.to_parquet(folder / f'{stem}{dv.GENE_SAMPLES_SUFFIX}', index=False)
    pd.DataFrame(cell_rows).to_parquet(folder / f'{stem}{dv.GENE_SAMPLE_CELLS_SUFFIX}', index=False)
    return {'counts': counts, 'library_size': library_size, 'norm_factor': norm_factor, 'facet_value': facet_value,
            'count_rows': pd.DataFrame(count_rows)}


def make_pooled_db(count_rows_by_db: Dict[str, pd.DataFrame]):
    """A stand-in for ``_pooled_db``: per dataset an in-memory duckdb with ``__genes`` and a counts table."""
    connections = {}
    for db_id, count_rows in count_rows_by_db.items():
        connection = duckdb.connect()
        connection.execute('CREATE TABLE __genes (gene_id INTEGER, gene_name VARCHAR)')
        for gene_id, name in enumerate(GENES):
            connection.execute('INSERT INTO __genes VALUES (?, ?)', [gene_id, name])
        connection.register('count_rows', count_rows)
        connection.execute('CREATE TABLE counts_layer AS SELECT * FROM count_rows')
        connections[db_id] = connection

    @contextlib.contextmanager
    def pooled(db_id: str):
        class Database:
            _conn = connections[db_id]

            @staticmethod
            def _layer_source_for_gene_filter(layer: str) -> str:
                return 'counts_layer'

        yield Database

    return pooled


def fetcher_from(count_rows: pd.DataFrame):
    def fetch(file_index: int, gene: str) -> Optional[pd.DataFrame]:
        if gene.upper() != PLANTED_GENE:
            return None
        return count_rows[count_rows['gene_id'] == GENE_ID][['cell_id', 'value']].reset_index(drop=True)
    return fetch


def expected_values(truth: Dict[str, Any]) -> np.ndarray:
    """The voom input of the planted gene as the formula gives it, in label order (facet value, library size)."""
    order = np.lexsort((truth['library_size'], truth['facet_value']))
    return np.log2((truth['counts'][order] + 0.5) / (truth['library_size'][order] * truth['norm_factor'][order] + 1) * 1e6)


# ── pure functions ───────────────────────────────────────────────────────────

def test_voom_log2_cpm_is_the_voom_formula():
    values = dv.voom_log2_cpm(np.array([0, 10, 1000]), np.array([1e6, 2e6, 1e6]), np.array([1.0, 0.5, 2.0]))
    assert values == pytest.approx([np.log2(0.5 / (1e6 + 1) * 1e6), np.log2(10.5 / (1e6 + 1) * 1e6),
                                    np.log2(1000.5 / (2e6 + 1) * 1e6)])


def test_adjust_for_covariates_removes_only_the_covariate_part():
    rng = np.random.default_rng(0)
    facet = np.tile([0.0, 1.0], 10)
    covariate = rng.normal(0, 1, 20)
    values = 1.0 + 0.8 * facet + 2.0 * covariate + rng.normal(0, 0.01, 20)
    adjusted = dv.adjust_for_covariates(values, facet, covariate[:, None])
    refit, *_ = np.linalg.lstsq(np.column_stack([np.ones(20), facet, covariate]), adjusted, rcond=None)
    assert refit[2] == pytest.approx(0.0, abs=1e-9)                       # no covariate effect left
    assert adjusted[facet == 1].mean() - adjusted[facet == 0].mean() == pytest.approx(0.8, abs=0.05)
    assert adjusted.mean() == pytest.approx(values.mean())
    assert dv.adjust_for_covariates(values, facet, np.zeros((20, 0))) == pytest.approx(values)


def test_fitted_line_has_the_reported_slope_through_the_centre():
    donors = [{'facet_value': 0.0, 'value': 1.0, 'value_adjusted': 1.0},
              {'facet_value': 4.0, 'value': 3.0, 'value_adjusted': 5.0}]
    line = dv.fitted_line(donors, 'braak', 0.5)
    assert line == {'slope': 0.5, 'intercept': pytest.approx(2.0 - 0.5 * 2.0)}
    assert dv.fitted_line(donors, 'braak', 0.5, adjusted=True)['intercept'] == pytest.approx(3.0 - 1.0)
    age = [{'facet_value': 60.0, 'value': 1.0, 'value_adjusted': 1.0}, {'facet_value': 80.0, 'value': 2.0, 'value_adjusted': 2.0}]
    assert dv.fitted_line(age, 'age', 0.3)['slope'] == pytest.approx(0.03)      # per decade -> per year
    assert dv.fitted_line(donors, 'braak', None) is None and dv.fitted_line([], 'braak', 0.5) is None


def test_facet_kind_and_scale_names():
    assert dv.facet_kind('braak') == 'numeric' and dv.facet_kind('apoe4_dose') == 'numeric'
    assert dv.facet_kind('sex') == 'two_group' and dv.facet_kind('braak_threshold') == 'two_group'
    assert 'UCell' in dv.module_scale('cell_mean', 'ucell')
    assert 'pseudobulk' in dv.module_scale('pseudobulk', 'gene_list')
    assert 'score_genes' in dv.module_scale('cell_mean', 'geneset')


# ── the reader ───────────────────────────────────────────────────────────────

def test_gene_unit_values_are_the_voom_input_and_anonymous(results):
    truth = write_gene_sidecars(results, 'G1', 'ds1', 'braak')
    reader = dv.DonorValues(results)
    answer = reader.gene_unit('G1', 'ds1', 'braak', CELL_TYPE, 'constant_all_cells', PLANTED_GENE,
                              fetcher_from(truth['count_rows']))
    assert answer['available'] and answer['facet_kind'] == 'numeric' and answer['n_donors'] == 18
    assert [d['label'] for d in answer['donors']] == [f'D{i:02d}' for i in range(1, 19)]
    assert [d['value'] for d in answer['donors']] == pytest.approx(list(expected_values(truth)))
    assert [d['facet_value'] for d in answer['donors']] == sorted(truth['facet_value'])
    assert answer['covariates'] == ['sex', 'age', 'pmi']
    assert all('group' not in d for d in answer['donors'])
    assert set(answer['donors'][0]) == {'label', 'facet_value', 'value', 'value_adjusted', 'n_cells'}
    assert answer['counts_total'] == int(truth['counts'].sum())


def test_planted_slope_is_recovered_from_the_stored_scale(results):
    """The unadjusted values carry the planted effect: the OLS slope per Braak stage matches the stored logFC (0.5)."""
    truth = write_gene_sidecars(results, 'G1', 'ds1', 'braak', n_samples=40, seed=11)
    answer = dv.DonorValues(results).gene_unit('G1', 'ds1', 'braak', CELL_TYPE, 'constant_all_cells', PLANTED_GENE,
                                               fetcher_from(truth['count_rows']))
    stages = np.array([d['facet_value'] for d in answer['donors']])
    slope = np.polyfit(stages, [d['value'] for d in answer['donors']], 1)[0]
    assert np.sign(slope) == np.sign(PLANTED_SLOPE) and slope == pytest.approx(PLANTED_SLOPE, abs=0.1)


def test_two_group_unit_has_group_names_and_adjusted_values(results):
    truth = write_gene_sidecars(results, 'G1', 'ds1', 'sex')
    answer = dv.DonorValues(results).gene_unit('G1', 'ds1', 'sex', CELL_TYPE, 'constant_all_cells', PLANTED_GENE,
                                               fetcher_from(truth['count_rows']), ('f', 'm'))
    groups = [d['group'] for d in answer['donors']]
    assert set(groups) == {'f', 'm'} and groups.count('f') == 9
    assert any(d['value'] != d['value_adjusted'] for d in answer['donors'])
    assert answer['facet_kind'] == 'two_group'


def test_age_is_shown_in_years(results):
    truth = write_gene_sidecars(results, 'G1', 'ds1', 'braak')
    samples = pd.read_parquet(results / 'G1' / f'ds1{dv.GENE_SAMPLES_SUFFIX}')
    samples['facet'] = 'age'
    samples['facet_value'] = np.linspace(6.0, 9.5, len(samples))
    samples.to_parquet(results / 'G1' / f'ds1{dv.GENE_SAMPLES_SUFFIX}', index=False)
    answer = dv.DonorValues(results).gene_unit('G1', 'ds1', 'age', CELL_TYPE, 'constant_all_cells', PLANTED_GENE,
                                               fetcher_from(truth['count_rows']))
    assert answer['donors'][0]['facet_value'] == pytest.approx(60.0) and answer['donors'][-1]['facet_value'] == pytest.approx(95.0)


def test_unavailable_without_sidecar_unit_or_gene(results):
    reader = dv.DonorValues(results)
    no_file = reader.gene_unit('G2', 'ds2', 'braak', CELL_TYPE, 'constant_all_cells', PLANTED_GENE, lambda *_: None)
    assert no_file['available'] is False and 'backfill' in no_file['reason'] and no_file['donors'] == []
    truth = write_gene_sidecars(results, 'G1', 'ds1', 'braak')
    other_unit = reader.gene_unit('G1', 'ds1', 'sex', CELL_TYPE, 'constant_all_cells', PLANTED_GENE, lambda *_: None)
    assert other_unit['available'] is False
    missing_gene = reader.gene_unit('G1', 'ds1', 'braak', CELL_TYPE, 'constant_all_cells', 'NOPE', fetcher_from(truth['count_rows']))
    assert missing_gene['available'] is False and 'not in the counts' in missing_gene['reason']
    assert reader.has_unit('G1', 'ds1', 'braak', CELL_TYPE, 'constant_all_cells')
    assert not reader.has_unit('G1', 'ds1', 'sex', CELL_TYPE, 'constant_all_cells')


def test_answers_are_cached_per_file_stamp(results):
    truth = write_gene_sidecars(results, 'G1', 'ds1', 'braak')
    calls = []

    def counting_fetch(file_index, gene):
        calls.append(gene)
        return fetcher_from(truth['count_rows'])(file_index, gene)

    reader = dv.DonorValues(results)
    first = reader.gene_unit('G1', 'ds1', 'braak', CELL_TYPE, 'constant_all_cells', PLANTED_GENE, counting_fetch)
    again = reader.gene_unit('G1', 'ds1', 'braak', CELL_TYPE, 'constant_all_cells', PLANTED_GENE.lower(), counting_fetch)
    assert again is first and len(calls) == 1
    write_gene_sidecars(results, 'G1', 'ds1', 'braak', seed=99)           # the files change: the cache is not served
    changed = reader.gene_unit('G1', 'ds1', 'braak', CELL_TYPE, 'constant_all_cells', PLANTED_GENE, counting_fetch)
    assert changed is not first and len(calls) == 2


def test_partial_sidecar_counts_as_not_available(results):
    (results / 'G1').mkdir(exist_ok=True)
    (results / 'G1' / f'ds1{dv.GENE_SAMPLES_SUFFIX}').write_bytes(b'PAR1 truncated')
    (results / 'G1' / f'ds1{dv.GENE_SAMPLE_CELLS_SUFFIX}').write_bytes(b'PAR1 truncated')
    answer = dv.DonorValues(results).gene_unit('G1', 'ds1', 'braak', CELL_TYPE, 'constant_all_cells', PLANTED_GENE,
                                               lambda *_: None)
    assert answer['available'] is False


def write_module_sidecars(results: Path, group: str, stem: str, facet: str = 'sex') -> Dict[str, Any]:
    rng = np.random.default_rng(8)
    n = 12
    facet_value = (np.arange(n) % 2).astype(float)
    samples = pd.DataFrame({
        'facet': facet, 'cell_type': CELL_TYPE, 'celltype_source': 'constant_all_cells', 'sample_idx': np.arange(n),
        'facet_value': facet_value, 'cov_sex': np.nan, 'cov_age': rng.normal(0, 1, n), 'cov_pmi': np.nan,
        'cov_path': np.nan, 'batch_code': -1, 'n_cells': 30})
    scores = pd.DataFrame({'sample_idx': np.tile(np.arange(n), 2), 'method': ['cell_mean'] * n + ['pseudobulk'] * n,
                           'source': ['ucell'] * n + ['gene_list'] * n, 'catalog': 'Study/Exp', 'module': 'modA',
                           'score': np.concatenate([0.2 + 0.1 * facet_value, 1.0 + 0.5 * facet_value])})
    (results / group).mkdir(exist_ok=True)
    samples.to_parquet(results / group / f'{stem}{dv.MODULE_SAMPLES_SUFFIX}', index=False)
    scores.to_parquet(results / group / f'{stem}{dv.MODULE_SCORES_SUFFIX}', index=False)
    return {'facet_value': facet_value}


def test_module_unit_returns_the_variant_scores(results):
    write_module_sidecars(results, 'G1', 'ds1')
    reader = dv.DonorValues(results)
    pseudobulk = reader.module_unit('G1', 'ds1', 'sex', CELL_TYPE, 'constant_all_cells', 'Study/Exp', 'modA',
                                    'pseudobulk', 'gene_list', ('f', 'm'))
    assert pseudobulk['available'] and pseudobulk['n_donors'] == 12 and 'pseudobulk' in pseudobulk['scale']
    by_group = {g: np.mean([d['value'] for d in pseudobulk['donors'] if d['group'] == g]) for g in ('f', 'm')}
    assert by_group == pytest.approx({'f': 1.0, 'm': 1.5})
    ucell = reader.module_unit('G1', 'ds1', 'sex', CELL_TYPE, 'constant_all_cells', 'Study/Exp', 'modA',
                               'cell_mean', 'ucell', ('f', 'm'))
    assert ucell['donors'][0]['value'] == pytest.approx(0.2) and 'UCell' in ucell['scale']
    absent = reader.module_unit('G1', 'ds1', 'sex', CELL_TYPE, 'constant_all_cells', 'Study/Exp', 'other',
                                'pseudobulk', 'gene_list')
    assert absent['available'] is False and 'no replicate scores' in absent['reason']
    nothing = reader.module_unit('G2', 'ds2', 'sex', CELL_TYPE, 'constant_all_cells', 'Study/Exp', 'modA',
                                 'pseudobulk', 'gene_list')
    assert nothing['available'] is False


# ── the plot geom ────────────────────────────────────────────────────────────

def _panels(kind: str) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(1)
    panels = []
    for title in ('Cohort A (n = 12)', 'Cohort B (n = 12)'):
        if kind == 'numeric':
            x = rng.integers(0, 7, 12).astype(float)
            panels.append({'title': title, 'values': list(1.0 + 0.3 * x + rng.normal(0, 0.1, 12)), 'x': list(x),
                           'line': {'slope': 0.3, 'intercept': 1.0}})
        else:
            groups = ['f', 'm'] * 6
            panels.append({'title': title, 'values': list(rng.normal(3, 1, 12)), 'groups': groups})
    return panels


@pytest.mark.parametrize('kind', ['two_group', 'numeric'])
@pytest.mark.parametrize('output_format, signature', [('png', b'\x89PNG'), ('svg', b'<?xml'), ('pdf', b'%PDF')])
def test_plot_donor_values_renders_every_format(kind, output_format, signature):
    image = plotting.plot_donor_values(_panels(kind), kind=kind, y_label='log2 CPM', x_label='Braak stage',
                                       group_order=['f', 'm'], output_format=output_format, dpi=72)
    assert image.startswith(signature) and len(image) > 1000


def test_plot_never_draws_the_caption():
    """The caption travels in metadata only: the pixels are identical with and without it."""
    from matplotlib import image as mpl_image
    kwargs = dict(kind='two_group', y_label='log2 CPM', group_order=['f', 'm'], dpi=72)
    with_caption = plotting.plot_donor_values(_panels('two_group'), caption='effect 0.512, q = 0.0003', **kwargs)
    without = plotting.plot_donor_values(_panels('two_group'), caption=None, **kwargs)
    assert with_caption != without                                    # the PNG text chunk carries the caption
    assert b'q = 0.0003' in with_caption
    assert (mpl_image.imread(io.BytesIO(with_caption)) == mpl_image.imread(io.BytesIO(without))).all()


def test_plot_with_no_panels_and_panel_cap():
    assert plotting.plot_donor_values([], kind='numeric', y_label='x').startswith(b'\x89PNG')
    many = _panels('two_group') * (plotting.MAX_DONOR_PANELS // 2 + 3)
    assert plotting.plot_donor_values(many, kind='two_group', y_label='x', group_order=['f', 'm'], dpi=50).startswith(b'\x89PNG')


# ── endpoints ────────────────────────────────────────────────────────────────

def _braak_serve(results: Path, poison: bool = False):
    truth_a = write_gene_sidecars(results, 'G1', 'ds1', 'braak', poison=poison)
    truth_b = write_gene_sidecars(results, 'G2', 'ds2', 'braak', n_samples=30, seed=4, poison=poison)
    serve = build_serve(results, pooled_db=make_pooled_db({'G1/ds1': truth_a['count_rows'],
                                                           'G2/ds2': truth_b['count_rows']}))
    return serve, truth_a, truth_b


def test_donor_values_shape_scale_and_cohorts(results):
    serve, truth_a, truth_b = _braak_serve(results)
    body = serve.get('/facets/braak/donor-values', params={'gene': PLANTED_GENE, 'cell_type': 'microglia'}).json()
    assert body['facet'] == 'braak' and body['scope'] == 'human_postmortem' and body['facet_kind'] == 'numeric'
    assert body['subject'] == {'gene': PLANTED_GENE} and body['adjusted'] is False
    assert body['x_label'] == 'Braak stage' and 'log2 CPM' in body['scale']
    assert 'unadjusted' in body['note'] and 'sex, age, pmi' in body['note']
    assert {u['cohort_display'] for u in body['units']} == {'CohortA', 'CohortB'} and body['n_counted'] == 2
    assert 'donor-plots' in body['help_topics']
    unit_a = next(u for u in body['units'] if u['db_id'] == 'G1/ds1')
    assert unit_a['available'] and unit_a['counted'] and unit_a['n_donors'] == 18
    assert unit_a['covariates'] == ['sex', 'age', 'pmi'] and unit_a['replicate_unit'] == 'donor'
    assert unit_a['model']['estimate'] == pytest.approx(0.5) and unit_a['model']['q_value'] == pytest.approx(0.001)
    assert unit_a['model']['unit'] == 'per Braak stage'
    # the values are the voom input of the stored scale: the formula on the planted counts
    assert [d['value'] for d in unit_a['donors']] == pytest.approx(list(np.round(expected_values(truth_a), 5)))
    assert unit_a['line']['slope'] == pytest.approx(0.5)
    unit_b = next(u for u in body['units'] if u['db_id'] == 'G2/ds2')
    assert unit_b['n_donors'] == 30


def test_donor_values_slope_agrees_in_sign_and_size_with_the_stored_effect(results):
    serve, *_ = _braak_serve(results)
    body = serve.get('/facets/braak/donor-values', params={'gene': PLANTED_GENE, 'cell_type': 'microglia'}).json()
    for unit in body['units']:
        stage = [d['facet_value'] for d in unit['donors']]
        slope = np.polyfit(stage, [d['value'] for d in unit['donors']], 1)[0]
        assert np.sign(slope) == np.sign(unit['model']['estimate'])
        assert slope == pytest.approx(unit['model']['estimate'], abs=0.15)


def test_adjusted_values_change_and_keep_the_scale(results):
    serve, *_ = _braak_serve(results)
    params = {'gene': PLANTED_GENE, 'cell_type': 'microglia'}
    plain = serve.get('/facets/braak/donor-values', params=params).json()
    adjusted = serve.get('/facets/braak/donor-values', params={**params, 'adjusted': True}).json()
    assert adjusted['adjusted'] is True and 'covariate-adjusted' in adjusted['note']
    first_plain, first_adjusted = plain['units'][0]['donors'], adjusted['units'][0]['donors']
    assert [d['value'] for d in first_plain] != [d['value'] for d in first_adjusted]
    assert np.mean([d['value'] for d in first_adjusted]) == pytest.approx(np.mean([d['value'] for d in first_plain]), abs=1e-3)


def test_include_uncounted_lists_the_other_units_with_their_reason(results):
    serve, *_ = _braak_serve(results)
    params = {'gene': PLANTED_GENE, 'cell_type': 'microglia', 'include_uncounted': True}
    body = serve.get('/facets/braak/donor-values', params=params).json()
    assert body['n_units'] >= body['n_counted']
    assert all(u['counted'] or u['not_counted_reason'] for u in body['units'])


def test_donor_values_of_one_dataset_keep_only_its_units(results):
    serve, *_ = _braak_serve(results)
    params = {'gene': PLANTED_GENE, 'cell_type': 'microglia', 'db_id': 'G2/ds2'}
    body = serve.get('/facets/braak/donor-values', params=params).json()
    assert [u['db_id'] for u in body['units']] == ['G2/ds2'] and body['units'][0]['n_donors'] == 30
    assert serve.get('/facets/braak/donor-values', params={**params, 'db_id': 'G9/none'}).status_code == 404
    plot = serve.get('/facets/braak/donor-plot.png', params=params)
    assert plot.status_code == 200 and plot.headers['content-type'] == 'image/png'


def test_donor_values_errors(results):
    serve, *_ = _braak_serve(results)
    assert serve.get('/facets/braak/donor-values', params={'cell_type': 'microglia'}).status_code == 400
    assert serve.get('/facets/braak/donor-values',
                     params={'gene': 'A', 'module': 'S/E/m', 'cell_type': 'microglia'}).status_code == 400
    assert serve.get('/facets/nonsense/donor-values', params={'gene': 'A', 'cell_type': 'microglia'}).status_code == 404
    assert serve.get('/facets/braak/donor-values', params={'gene': 'NOSUCHGENE', 'cell_type': 'microglia'}).status_code == 404
    assert serve.get('/facets/braak/donor-values', params={'gene': 'A', 'cell_type': 'microglia', 'scope': 'moon'}).status_code == 400
    assert serve.get('/facets/braak/donor-values', params={'module': 'nocatalog', 'cell_type': 'microglia'}).status_code == 400
    assert serve.get('/facets/braak/donor-values', params={'cell_type': 'microglia'}).status_code == 400


def test_unavailable_units_say_why_until_backfilled(results):
    serve = build_serve(results)                       # no sidecars at all
    body = serve.get('/facets/braak/donor-values', params={'gene': PLANTED_GENE, 'cell_type': 'microglia'}).json()
    assert body['n_counted'] == 2 and all(u['available'] is False and 'backfill' in u['reason'] for u in body['units'])
    plot = serve.get('/facets/braak/donor-plot.png', params={'gene': PLANTED_GENE, 'cell_type': 'microglia'})
    assert plot.status_code == 404 and 'backfill' in plot.json()['detail']


def test_no_controlled_id_leaks_even_if_a_sidecar_carries_one(results):
    """Responses are built from a whitelist of fields: a donor id in a sidecar (never written by the tools) cannot leak."""
    serve, *_ = _braak_serve(results, poison=True)
    response = serve.get('/facets/braak/donor-values', params={'gene': PLANTED_GENE, 'cell_type': 'microglia',
                                                                'include_uncounted': True})
    assert response.status_code == 200
    assert POISON_DONOR not in response.text and 'ROSMAP' not in response.text
    assert 'donor' not in {key for unit in response.json()['units'] for d in unit['donors'] for key in d}
    plot = serve.get('/facets/braak/donor-plot.png', params={'gene': PLANTED_GENE, 'cell_type': 'microglia'})
    assert POISON_DONOR.encode() not in plot.content and POISON_DONOR not in str(plot.headers)


def test_donor_plot_png_svg_headers_and_clean_image(results):
    serve, *_ = _braak_serve(results)
    params = {'gene': PLANTED_GENE, 'cell_type': 'microglia'}
    png = serve.get('/facets/braak/donor-plot.png', params=params)
    assert png.status_code == 200 and png.headers['content-type'] == 'image/png' and png.content.startswith(b'\x89PNG')
    caption = png.headers['x-plot-caption']
    assert 'PTPRG' in caption and 'q = 0.001' in caption and 'effect 0.500' in caption and 'unadjusted' in caption
    assert 'CohortA' in caption and 'One dot per donor' in caption
    # the caption is in the image metadata, not on the picture: same pixels as the bare plot
    from matplotlib import image as mpl_image
    assert b'q = 0.001' in png.content
    pixels = mpl_image.imread(io.BytesIO(png.content))
    assert pixels.shape[0] > 100 and pixels.shape[1] > 200
    svg = serve.get('/facets/braak/donor-plot.svg', params=params)
    assert svg.status_code == 200 and svg.headers['content-type'].startswith('image/svg+xml')
    assert serve.get('/facets/braak/donor-plot.gif', params=params).status_code == 400
    cached = serve.get('/facets/braak/donor-plot.png', params=params)
    assert cached.content == png.content


def test_donor_plot_two_group_facet_and_adjusted(results):
    truth = write_gene_sidecars(results, 'G1', 'ds1', 'sex')
    serve = build_serve(results, pooled_db=make_pooled_db({'G1/ds1': truth['count_rows']}))
    params = {'gene': PLANTED_GENE, 'cell_type': 'microglia', 'adjusted': True}
    body = serve.get('/facets/sex/donor-values', params=params).json()
    unit = body['units'][0]
    assert body['facet_kind'] == 'two_group' and body['levels'] == ['f', 'm'] and body['x_label'] is None
    assert {d['group'] for d in unit['donors']} == {'f', 'm'} and unit['line'] is None
    plot = serve.get('/facets/sex/donor-plot.png', params=params)
    assert plot.status_code == 200 and 'covariate-adjusted' in plot.headers['x-plot-caption']


def test_gene_card_replication_entries_carry_links(results):
    serve, *_ = _braak_serve(results)
    metadata = serve.registry.metadata_cache
    metadata['G1/ds1']['obs'] = {'categorical': ['ant_cell_type'], 'numerical': ['ant_braak']}
    card = serve.get('/facets/braak/effects', params={'gene': PLANTED_GENE}).json()
    entry = next(e for e in card['replication'] if e['celltype_group'] == 'microglia')
    links = entry['links']
    assert links['donor_plot'] == '/facets/braak/donor-plot.png?gene=PTPRG&scope=human_postmortem&cell_type=microglia'
    assert links['donor_plot_svg'].endswith('.svg?gene=PTPRG&scope=human_postmortem&cell_type=microglia')
    assert links['donor_values'] == '/facets/braak/donor-values?gene=PTPRG&scope=human_postmortem&cell_type=microglia'
    assert links['donor_values_available'] is True
    assert links['donor_plot_adjusted'] == links['donor_plot'] + '&adjusted=true'
    per_cell = {item['db_id']: item for item in links['anchors']}
    assert per_cell['G1/ds1']['label'] == 'per-cell, descriptive'
    assert per_cell['G1/ds1']['url'] == '/databases/G1/ds1/plot/scatter?x=ant_braak&y=PTPRG&regline=global'
    assert 'G2/ds2' not in per_cell                    # no facet column in this dataset's metadata
    # the link works
    assert serve.get(links['donor_values']).status_code == 200
    again = serve.get('/genes/PTPRG/facet-effects', params={'facet': 'braak'}).json()
    assert again['facets']['braak']['replication'][0]['links']['donor_plot'].startswith('/facets/braak/donor-plot.png')


def test_links_say_when_values_are_not_available_yet(results):
    serve = build_serve(results)
    card = serve.get('/facets/braak/effects', params={'gene': PLANTED_GENE}).json()
    links = next(e for e in card['replication'] if e['celltype_group'] == 'microglia')['links']
    assert links['donor_values_available'] is False and links['anchors'] == []


def test_per_cell_link_rules(results):
    serve = build_serve(results)
    link = serve.namespace['_per_cell_link']
    meta = serve.registry.metadata_cache
    meta['G1/ds1']['obs'] = {'categorical': ['ant_sex', 'ant_cell_type', 'ant_author_cell_type'], 'numerical': ['ant_braak']}
    assert link('G1/ds1', 'sex', 'XIST', 'constant_all_cells', 'microglial cell') == '/databases/G1/ds1/plot/violin?x=ant_sex&y=XIST'
    assert link('G1/ds1', 'sex', 'XIST', 'ant_cell_type', 'Mic P2RY12') == \
        '/databases/G1/ds1/plot/violin?x=ant_sex&y=XIST&subset=ant_cell_type%3AMic+P2RY12'
    assert link('G1/ds1', 'sex', 'XIST', 'ant_cell_type', 'Mic, P2RY12') is None          # a comma would split the subset
    assert link('G1/ds1', 'sex', 'XIST', 'group_pool', 'microglia') is None
    assert link('G1/ds1', 'sex', 'XIST', 'ant_cell_type@early', 'x') is None
    assert link('G1/ds1', 'sex', 'XIST', 'ant_missing', 'x') is None
    assert link('G1/ds1', 'disease', 'XIST', 'constant_all_cells', 'x') is None            # no such column
    assert link('G1/ds1', 'braak', 'XIST', 'constant_all_cells', 'x') == \
        '/databases/G1/ds1/plot/scatter?x=ant_braak&y=XIST&regline=global'
    assert link('G9/none', 'sex', 'XIST', 'constant_all_cells', 'x') is None


# ── gene sets ────────────────────────────────────────────────────────────────

def test_module_values_endpoint_and_links(views):
    results = views
    _module_frame('G1/ds1', 'braak', donors=18).to_parquet(results / 'G1' / 'ds1.facet_module_tests.parquet')
    write_module_sidecars(results, 'G1', 'ds1', facet='braak')
    serve = build_serve(results)
    # sex samples were written for facet braak so the module unit exists as (braak, microglial cell)
    body = serve.get('/facets/braak/donor-values',
                     params={'module': 'Study/Exp/modA', 'cell_type': 'microglia', 'method': 'pseudobulk',
                             'source': 'gene_list'}).json()
    assert body['subject'] == {'module': 'Study/Exp/modA', 'method': 'pseudobulk', 'source': 'gene_list'}
    unit = next(u for u in body['units'] if u['db_id'] == 'G1/ds1')
    assert unit['available'] and unit['n_donors'] == 12 and 'pseudobulk' in unit['scale']
    assert unit['model']['q_value'] == pytest.approx(0.001) and unit['model']['unit'] == 'per Braak stage'
    default = serve.get('/facets/braak/donor-values', params={'module': 'Study/Exp/modA', 'cell_type': 'microglia'}).json()
    assert default['subject']['method'] == 'pseudobulk'               # the card's primary variant
    plot = serve.get('/facets/braak/donor-plot.png', params={'module': 'Study/Exp/modA', 'cell_type': 'microglia'})
    assert plot.status_code == 200 and 'Study/Exp/modA' in plot.headers['x-plot-caption']
    assert serve.get('/facets/braak/donor-values', params={'module': 'Study/Exp/none', 'cell_type': 'microglia'}).status_code == 404


def test_module_card_entries_and_variants_carry_links(views):
    results = views
    _module_frame('G1/ds1', 'braak', donors=18).to_parquet(results / 'G1' / 'ds1.facet_module_tests.parquet')
    _module_frame('G2/ds2', 'braak', donors=30).to_parquet(results / 'G2' / 'ds2.facet_module_tests.parquet')
    write_module_sidecars(results, 'G1', 'ds1', facet='braak')
    write_module_sidecars(results, 'G2', 'ds2', facet='braak')
    serve = build_serve(results)
    card = serve.get('/genesets/Study/Exp/modA/facet-effects', params={'facet': 'braak', 'scope': 'human_postmortem'}).json()
    entry = next(e for e in card['facets']['braak']['module']['replication'] if e['celltype_group'] == 'microglia')
    assert entry['links']['donor_plot'].startswith('/facets/braak/donor-plot.png?module=Study%2FExp%2FmodA&method=')
    assert entry['links']['donor_values_available'] is True
    assert 'anchors' not in entry['links']
    for variant in entry['variants']:
        assert variant['links']['donor_values'].count(f"method={variant['method']}") == 1


# ── MCP ──────────────────────────────────────────────────────────────────────

def test_mcp_tool_and_help_topic_exist():
    source = MCP_PATH.read_text()
    assert 'def facet_donor_values' in source and '/donor-values' in source
    import help_content  # noqa: PLC0415
    assert help_content.get_topic('donor-plots') is not None
