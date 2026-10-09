"""API 2.45: the data behind the new gene, gene set and facet views.

Forest rows with limma intervals, the calls a view draws, module (gene set) replication across cohorts, member gene
counts, the facet overview, uncapped paging, the low-detection pill and the modality tag. The facet result files are
the small fixtures of ``test_facet_results.py``, extended with module results for two cohorts, a supplementary unit
of one cohort and a dataset outside the cohort table.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import facet_module_results as fm  # noqa: E402
import facet_results as fr  # noqa: E402
import gene_classes  # noqa: E402
import gene_detection  # noqa: E402
import modality  # noqa: E402
from test_facet_results import (  # noqa: E402,F401  (fixtures and helpers shared with the base facet tests)
    DATASETS, MCP_PATH, _gene_manifest, _gene_rows, _unit, build_serve, results)

VIEW_DATASETS = DATASETS + [{'db_id': 'G5/ds5', 'group_folder': 'G5', 'stem': 'ds5', 'title': 'Dataset five'},
                            {'db_id': 'G6/ds6', 'group_folder': 'G6', 'stem': 'ds6', 'title': 'Dataset six'}]


def _module_frame(db_id: str, facet: str, cell_type: str = 'microglial cell', donors: int = 18,
                  descriptive: bool = False) -> pd.DataFrame:
    """Module rows of one unit: modA replicates in both variants, modB goes up in A and down in B, modC is noise."""
    plan = {
        ('cell_mean', 'geneset'): {'modA': (0.3, 0.6, 0.001), 'modB': (0.3, 0.6, 0.001), 'modC': (0.01, 0.05, 0.5)},
        ('pseudobulk', 'gene_list'): {'modA': (0.3, 0.6, 0.001), 'modB': (-0.3, -0.6, 0.001), 'modC': (0.02, 0.1, 0.4)},
    }
    rows = []
    for (method, source), modules in plan.items():
        for module, (estimate, std_effect, p_perm) in modules.items():
            rows.append({
                'db_id': db_id, 'dataset': db_id.split('/')[1], 'source': source, 'catalog': 'Study/Exp',
                'module': module, 'estimate': estimate, 'se': 0.05, 't': 3.0, 'df': 15.0,
                'p_param': None if descriptive else p_perm, 'ci_low': None if descriptive else estimate - 0.1,
                'ci_high': None if descriptive else estimate + 0.1, 'std_effect': std_effect,
                'p_perm': None if descriptive else p_perm, 'n_perm_effective': 1000,
                'q_param': None if descriptive else p_perm, 'q_perm': None if descriptive else p_perm,
                'method': method, 'facet': facet, 'cell_type': cell_type, 'celltype_source': 'constant_all_cells',
                'contrast': 'braak_per_unit', 'level_ref': None, 'level_test': None, 'n_donors': donors,
                'n_lt5': descriptive, 'small_sample': donors < 5, 'descriptive_only': descriptive,
                'n_genes_requested': 20, 'n_genes_found': 18, 'no_counts': False, 'facet_scope': 'human_postmortem'})
    return pd.DataFrame(rows)


@pytest.fixture
def views(results: Path) -> Path:
    """``results`` plus module results for braak in both cohorts, a supplementary unit of cohort A (G5/ds5)
    and a dataset outside the cohort table (G6/ds6)."""
    root = results
    _module_frame('G1/ds1', 'braak', donors=18).to_parquet(root / 'G1' / 'ds1.facet_module_tests.parquet')
    _module_frame('G2/ds2', 'braak', donors=30).to_parquet(root / 'G2' / 'ds2.facet_module_tests.parquet')
    for group, stem, effects in (('G5', 'ds5', {'PTPRG': (0.45, 0.002)}), ('G6', 'ds6', {'PTPRG': (0.9, 0.0001)})):
        (root / group).mkdir()
        db_id = f'{group}/{stem}'
        pd.DataFrame(_gene_rows(db_id, 'braak', 'microglial cell', effects, donors=(40, 0))).to_parquet(
            root / group / f'{stem}.facet_tests.parquet')
        (root / group / f'{stem}.facet_run_manifest.json').write_text(json.dumps(_gene_manifest(
            db_id, group, stem, [_unit('braak', 'microglial cell', n_donors_analysed=40,
                                       level_counts={'n_donors': 40, 'braak_min': 0, 'braak_max': 6, 'n_distinct': 6})])))
    table = root / fr.COHORT_TABLE_NAME
    table.write_text(table.read_text()
                     + 'DRAFT\tCohortA (shared donors)\tmicroglia\tsupplement, one region\tG5/ds5\tsupplement\n')
    return root


@pytest.fixture
def reader(views: Path) -> fr.FacetResults:
    return fr.FacetResults(views)


@pytest.fixture
def modules(reader: fr.FacetResults) -> fm.ModuleResults:
    return fm.ModuleResults(reader)


def _replication(card: Dict[str, Any], group: str = 'microglia') -> Dict[str, Any]:
    return next(entry for entry in card['replication'] if entry['celltype_group'] == group)


# ── modality ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('suspension, technology, scope, expected, note', [
    ('nucleus', '10x_chromium', 'human_postmortem', 'nuclei', None),
    ('cell', '10x_chromium_3p_v3', 'human_postmortem', 'cells', None),
    ('cell', '10x_chromium_3p_v3', 'xenograft_human_in_mouse', 'cells', 'sorted live cells'),
    ('nucleus', '10x_chromium', 'xenograft_human_in_mouse', 'nuclei', None),
    ('na', 'snRNA-seq', 'human_postmortem', 'nuclei', None),
    (None, 'SnRNA-seq_SPLiT-seq', None, 'nuclei', None),
    ('na', 'bulk_RNA-seq', None, 'unknown', None),
    (None, None, None, 'unknown', None),
])
def test_modality_from_suspension_type_then_technology(suspension, technology, scope, expected, note):
    tag = modality.modality_of(suspension, technology, scope)
    assert (tag['modality'], tag['note']) == (expected, note)
    assert tag['label']


def test_modality_of_metadata_reads_the_dataset_dict():
    tag = modality.modality_of_metadata({'suspension_type': 'cell', 'facet_scope': 'xenograft_human_in_mouse'})
    assert tag == {'modality': 'cells', 'label': 'cells', 'note': 'sorted live cells'}
    assert modality.modality_of_metadata(None)['modality'] == 'unknown'


# ── gene detection ───────────────────────────────────────────────────────────

CANDIDATE_META = {
    'H/mic1': {'facet_scope': 'human_postmortem', 'cell_type': 'microglial cell', 'n_cells': 5000, 'title': 'mic1',
               'suspension_type': 'nucleus'},
    'H/mic2': {'facet_scope': 'human_postmortem', 'cell_type': 'microglial cell', 'n_cells': 3000, 'title': 'mic2',
               'suspension_type': 'cell'},
    'H/astro': {'facet_scope': 'human_postmortem', 'cell_type': 'astrocyte', 'n_cells': 900, 'title': 'astro'},
    'H/mixed': {'facet_scope': 'human_postmortem', 'cell_type': 'mixed', 'n_cells': 9000, 'title': 'mixed'},
    'H/list': {'facet_scope': 'human_postmortem', 'cell_type': ['microglial cell', 'astrocyte'], 'n_cells': 9000},
    'H/tiny': {'facet_scope': 'human_postmortem', 'cell_type': 'microglial cell', 'n_cells': 10, 'title': 'tiny'},
    'H/secondary': {'facet_scope': 'human_postmortem', 'cell_type': 'microglial cell', 'n_cells': 5000,
                    'is_secondary': True},
    'M/none': {'facet_scope': None, 'cell_type': 'microglial cell', 'n_cells': 5000},
    'X/mic': {'facet_scope': 'xenograft_human_in_mouse', 'cell_type': 'microglial cell', 'n_cells': 4000,
              'title': 'xeno', 'suspension_type': 'cell'},
}


def test_dataset_group_maps_single_cell_types_only():
    assert gene_detection.dataset_group('microglial cell') == 'microglia'
    assert gene_detection.dataset_group('mixed') is None
    assert gene_detection.dataset_group(['microglial cell', 'astrocyte']) is None
    assert gene_detection.dataset_group(None) is None


def test_candidates_leave_out_secondaries_and_datasets_without_scope():
    candidates = gene_detection.candidate_datasets(CANDIDATE_META)
    assert 'H/secondary' not in candidates and 'M/none' not in candidates
    assert candidates['H/mic1']['modality'] == 'nuclei' and candidates['X/mic']['modality_note'] == 'sorted live cells'
    assert candidates['H/list']['group'] is None


def _stats(values: Dict[str, float]):
    def lookup(db_id: str, gene: str):
        return ({'pct_nonzero': values[db_id], 'n_cells': CANDIDATE_META[db_id]['n_cells'], 'mean': 1.23456,
                 'q10_nonzero': 0.5, 'q90_nonzero': 2.5} if db_id in values else None)
    return lookup


def test_gene_detection_summarises_per_scope_and_group_and_flags_low():
    candidates = gene_detection.candidate_datasets(CANDIDATE_META)
    result = gene_detection.gene_detection('MEIS1', candidates, _stats(
        {'H/mic1': 2.0, 'H/mic2': 4.0, 'H/astro': 30.0, 'H/mixed': 50.0, 'H/tiny': 0.1, 'X/mic': 60.0}))
    summary = {(row['scope'], row['celltype_group']): row for row in result['summary']}
    microglia = summary[('human_postmortem', 'microglia')]
    assert microglia['n_datasets'] == 2 and microglia['pct_median'] == 3.0 and microglia['low_detection'] is True
    assert summary[('human_postmortem', 'astrocyte')]['low_detection'] is False
    assert summary[('xenograft_human_in_mouse', 'microglia')]['pct_median'] == 60.0   # scopes never mix
    assert ('human_postmortem', 'mixed / other') not in summary
    listed = {entry['db_id']: entry for entry in result['datasets']}
    assert listed['H/mixed']['celltype_group'] == 'mixed / other' and listed['H/mic2']['modality'] == 'cells'
    assert listed['H/mic1']['q10_nonzero'] == 0.5 and listed['H/mic1']['q90_nonzero'] == 2.5
    assert listed['H/mic1']['mean'] == 1.235 and listed['H/mic1']['rank_mean'] is None
    assert result['n_missing'] == len(candidates) - len(listed) and result['low_detection_pct'] == 5.0


def test_detection_table_and_pill():
    frame = pd.DataFrame({'dataset': ['H/mic1', 'H/mic2', 'H/mic1', 'H/astro', 'H/tiny'],
                          'gene': ['MEIS1', 'MEIS1', 'APOE', 'MEIS1', 'MEIS1'],
                          'n_cells': [5000, 3000, 5000, 900, 10], 'pct_nonzero': [2.0, 4.0, 80.0, 30.0, 0.0]})
    candidates = gene_detection.candidate_datasets(CANDIDATE_META)
    tables = gene_detection.detection_table(frame, candidates, fresh=lambda db_id: db_id != 'H/astro')
    assert set(tables) == {('human_postmortem', 'microglia')}                  # stale, tiny and mixed are out
    pill = gene_detection.pill(tables[('human_postmortem', 'microglia')], 'meis1')
    assert pill == {'pct_median': 3.0, 'n_datasets': 2, 'low_detection': True}
    assert gene_detection.pill(tables[('human_postmortem', 'microglia')], 'APOE')['low_detection'] is False
    assert gene_detection.pill(tables[('human_postmortem', 'microglia')], 'NOPE') is None
    assert gene_detection.pill(None, 'APOE') is None
    assert gene_detection.detection_table(None, candidates) == {}
    assert gene_detection.summarise([]) == []


# ── gene cards: forest, calls, small cohorts ─────────────────────────────────

def test_gene_card_forest_has_one_row_per_cohort_with_limma_intervals(reader):
    card = reader.gene_effects('braak', 'PTPRG', VIEW_DATASETS)
    forest = _replication(card)['forest']
    counted = [row for row in forest if row['counted']]
    assert [row['cohort_label'] for row in counted] == ['CohortA', 'CohortB']
    anchor = counted[0]['anchor']
    assert anchor['anchor_counted'] is True and anchor['ci_low'] == pytest.approx(0.3)
    assert anchor['ci_high'] == pytest.approx(0.7) and anchor['logFC'] == pytest.approx(0.5)
    assert anchor['not_counted_reason'] is None and anchor['modality'] is None   # no modality given in this fixture
    assert [row['in_m'] for row in counted] == [True, True]
    assert sum(row['in_m'] for row in forest) == _replication(card)['m']          # visible rows agree with m


def test_forest_folds_other_units_of_a_cohort_and_greys_cohorts_that_do_not_count(reader):
    forest = _replication(reader.gene_effects('braak', 'PTPRG', VIEW_DATASETS))['forest']
    cohort_a = next(row for row in forest if row['cohort_label'] == 'CohortA')
    assert cohort_a['n_more_units'] == 1
    folded = cohort_a['more_units'][0]
    assert folded['db_id'] == 'G5/ds5' and folded['anchor_counted'] is False
    assert 'supplementary' in folded['not_counted_reason'] or 'role' in folded['not_counted_reason']
    unassigned = forest[-1]
    assert unassigned['cohort_display'] == 'unassigned' and unassigned['counted'] is False
    assert unassigned['anchor']['db_id'] == 'G6/ds6' and unassigned['anchor']['not_counted_reason']
    assert [row['counted'] for row in forest] == [True, True, False]               # counted cohorts first


def test_forest_rows_are_slim_and_the_legacy_anchors_keep_their_fields(reader):
    entry = _replication(reader.gene_effects('braak', 'PTPRG', VIEW_DATASETS))
    point = entry['forest'][0]['anchor']
    assert 'cohort' not in point and 'independence' not in point and 'p_treat' not in point   # slim rows
    assert point['lambda_gc'] is None or isinstance(point['lambda_gc'], float)
    legacy = entry['anchors'][0]
    assert {'cohort', 'cohort_display', 'p_treat', 'q_treat', 'independence', 'disease_basis'} <= set(legacy)
    assert legacy['ci_low'] == pytest.approx(0.3) and legacy['anchor_counted'] is True


def test_forest_view_modes_copy_the_cached_forest(reader):
    forest = _replication(reader.gene_effects('braak', 'PTPRG', VIEW_DATASETS))['forest']
    assert fr.forest_view(forest, 'full') is forest
    assert fr.forest_view(forest, 'none') == []
    anchors = fr.forest_view(forest, 'anchors')
    assert all(row['more_units'] == [] for row in anchors) and anchors[0]['anchor'] == forest[0]['anchor']
    assert anchors[0]['n_more_units'] == forest[0]['n_more_units'] == 1 and forest[0]['more_units']   # original intact
    with pytest.raises(fr.FacetResultsError):
        fr.forest_view(forest, 'some')


def test_module_card_forest_modes(modules):
    card = modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak')
    trimmed = fm.module_card_forest(card, 'none')
    assert all(variant['forest'] == [] for entry in trimmed['replication'] for variant in entry['variants'])
    assert _replication(card)['variants'][0]['forest']                                # the cached card is untouched
    assert fm.module_card_forest(card, 'full')['replication'][0]['variants'][0]['forest']


def test_unit_cards_flag_the_anchor_and_name_the_cohort(reader):
    units = reader.gene_effects('braak', 'PTPRG', VIEW_DATASETS)['units']
    by_db = {unit['db_id']: unit for unit in units}
    assert by_db['G1/ds1']['anchor_counted'] is True and by_db['G5/ds5']['anchor_counted'] is False
    assert by_db['G6/ds6']['cohort_display'] == 'unassigned' and by_db['G1/ds1']['cohort_display'] == 'CohortA'
    assert by_db['G1/ds1']['small_sample'] is True and by_db['G2/ds2']['small_sample'] is False


def test_replication_entry_carries_the_call_the_pills_and_small_cohorts(reader):
    entry = _replication(reader.gene_effects('braak', 'PTPRG', VIEW_DATASETS))
    assert entry['call'] == 'replicated' and entry['gate'] is True and entry['strict'] is True
    assert entry['k_over_m'] == '2/2' and entry['cohorts_label'] == '2/2 cohorts, 1 small'
    assert entry['small_cohorts'] == ['CohortA'] and entry['n_small'] == 1
    assert entry['nt_label'] is None and entry['r_cohorts']
    assert 'small' not in entry['call']                                            # no "small effect" call exists


def test_single_cohort_is_not_tested_and_says_how_many(reader):
    entry = _replication(reader.gene_effects('sex', 'XIST', VIEW_DATASETS))
    assert entry['call'] == 'not_tested' and entry['nt_label'] == '1 cohort'
    assert entry['gate'] is False and entry['strict'] is False
    none = fr.FacetResults._call_fields({'m': 0, 'cohorts_tested': 0, 'k': None, 'n_small': 0})
    assert none['call'] == 'not_tested' and none['nt_label'] == 'not tested' and none['k_over_m'] is None
    assert none['cohorts_label'] == '0 cohorts'


def test_not_replicated_call_when_the_cohorts_disagree(reader):
    entry = _replication(reader.gene_effects('braak', 'APOE', VIEW_DATASETS))
    assert entry['call'] == 'not_replicated' and entry['gate'] is False


def test_overview_rows_carry_modality_and_cohort_display(reader):
    datasets = [{**dataset, 'modality': 'nuclei', 'modality_note': None} for dataset in VIEW_DATASETS]
    body = reader.facet_overview('braak', datasets)
    assert {row['modality'] for row in body['rows']} == {'nuclei'}
    assert {row['db_id']: row['cohort_display'] for row in body['rows']}['G6/ds6'] == 'unassigned'
    card = reader.gene_effects('braak', 'PTPRG', datasets)
    assert _replication(card)['forest'][0]['anchor']['modality'] == 'nuclei'


# ── top genes: paging, filters, headline, small cohorts ──────────────────────

def test_recurring_genes_page_and_report_what_matches(reader):
    everything = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia', limit=100)
    assert everything['n_matching'] == everything['n_returned'] == len(everything['rows']) and not everything['has_more']
    first = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia', limit=1)
    assert first['n_returned'] == 1 and first['offset'] == 0
    if everything['n_matching'] > 1:
        second = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia', limit=1, offset=1)
        assert first['has_more'] and second['rows'][0]['gene'] == everything['rows'][1]['gene']
    beyond = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia', limit=5, offset=500)
    assert beyond['rows'] == [] and beyond['has_more'] is False and beyond['n_matching'] == everything['n_matching']


def test_recurring_genes_gate_and_strict_filters_and_headline(reader):
    full = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia')
    gated = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia', effect_gate=True)
    strict = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia', strict=True)
    assert gated['n_matching'] <= full['n_matching'] and strict['n_matching'] <= gated['n_matching']
    assert all(row['effect_gate_passed'] for row in gated['rows'])
    assert all(row['replicated_effect'] and row['effect_gate_passed'] for row in strict['rows'])
    headline = full['headline']
    assert headline['n_effect_gate'] == gated['n_matching'] and headline['n_strict'] == strict['n_matching']
    assert 'PTPRG' in headline['strict_genes'] and headline['cohorts'] == 2
    assert headline['effect_gate_unit'] == '10% over three stages'


def test_recurring_rows_name_the_small_cohorts_and_options_carry_counts(reader):
    body = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia')
    row = next(r for r in body['rows'] if r['gene'] == 'PTPRG')
    assert row['n_small'] == 1 and row['small_cohorts'] == ['CohortA']
    option = next(o for o in body['cell_types'] if o['celltype_group'] == 'microglia')
    assert {'n_genes_in_family', 'n_replicated', 'n_effect_gate', 'n_strict'} <= set(option)
    assert option['n_replicated'] == body['n_replicated']
    assert fr.family_counts(None) == {'n_genes_in_family': 0, 'n_replicated': 0, 'n_effect_gate': 0, 'n_strict': 0}


def test_effects_overview_shows_each_gene_once_and_lists_quiet_groups(reader):
    body = reader.effects_overview('braak', VIEW_DATASETS)
    assert body['scope'] == 'human_postmortem' and body['headline']['celltype_group'] == 'microglia'
    listed = [gene['gene'] for card in body['cell_types'] for gene in card['genes']]
    assert len(listed) == len(set(listed)) == body['n_genes_unique'] and 'PTPRG' in listed
    card = body['cell_types'][0]
    assert card['n_assigned'] >= 1 and card['genes'][0]['also_in'] == []
    assert body['quiet_groups'] == []
    limited = reader.effects_overview('braak', VIEW_DATASETS, n_per_group=1)
    assert len(limited['cell_types'][0]['genes']) == 1
    empty = reader.effects_overview('apoe4_carrier', VIEW_DATASETS)
    assert empty['cell_types'] == [] and empty['n_genes_unique'] == 0


def test_member_replication_counts_names_and_directions(reader):
    body = reader.member_replication('braak', ['ptprg', 'APOE', 'NOT_A_GENE', ''], VIEW_DATASETS)
    group = body['groups']['microglia']
    assert body['n_members'] == 3 and group['n_members'] == 3 and group['n_in_data'] == 2
    assert group['n_replicated'] == 1 and group['genes_up'] == ['PTPRG'] and group['genes_down'] == []
    assert group['n_effect_gate'] == 1 and group['n_strict'] == 1 and group['genes_gate'] == ['PTPRG']
    gene = group['genes'][0]
    assert gene['gene'] == 'PTPRG' and gene['direction'] == 'up' and gene['m'] == 2 and gene['strict'] is True
    assert reader.member_replication('braak', [], VIEW_DATASETS)['groups']['microglia']['n_replicated'] == 0


def test_member_replication_can_leave_out_sex_chromosome_members(reader, monkeypatch):
    monkeypatch.setattr(fr.gene_classes, 'gtf_path', lambda: Path('annotation.gtf'))
    monkeypatch.setattr(fr.gene_classes, '_load', lambda path: {'PTPRG': ('chr3', 1000), 'APOE': ('chrX', 1000)})
    kept = reader.member_replication('sex', ['PTPRG', 'APOE'], VIEW_DATASETS, exclude_sex_chromosome=True)
    assert kept['n_members'] == 1 and kept['n_sex_chromosome_excluded'] == 1
    plain = reader.member_replication('sex', ['PTPRG', 'APOE'], VIEW_DATASETS)
    assert plain['n_members'] == 2 and plain['n_sex_chromosome_excluded'] == 0


def test_recurring_gene_rows_carry_the_smallest_and_largest_cohort_effect(reader):
    row = reader.recurring_genes('braak', VIEW_DATASETS, cell_type='microglia')['rows'][0]
    assert row['min_logFC'] <= row['median_logFC'] <= row['max_logFC']


# ── modules: replication across cohorts ──────────────────────────────────────

def test_module_tables_hold_both_cohorts_per_facet_and_scope(modules):
    tables = modules.tables(VIEW_DATASETS)
    rows, units = tables[('braak', 'human_postmortem')]
    assert set(units['db_id']) == {'G1/ds1', 'G2/ds2'} and set(rows['module_key']) == {
        'Study/Exp/modA', 'Study/Exp/modB', 'Study/Exp/modC'}
    assert set(tables) == {('braak', 'human_postmortem')}
    assert modules.scopes_with_module_results('braak', VIEW_DATASETS) == ['human_postmortem']


def test_module_families_replicate_per_variant(modules):
    families = modules.families('braak', VIEW_DATASETS)
    assert set(families) == {('microglia', 'cell_mean', 'geneset'), ('microglia', 'pseudobulk', 'gene_list')}
    frame = families[('microglia', 'pseudobulk', 'gene_list')]['frame']
    assert bool(frame.loc['Study/Exp/modA', 'replicated']) and not bool(frame.loc['Study/Exp/modC', 'replicated'])
    assert frame.loc['Study/Exp/modA', 'm'] == 2 and frame.loc['Study/Exp/modA', 'r_direction'] == 'up'
    assert frame.loc['Study/Exp/modB', 'r_direction'] == 'down'
    assert bool(frame.loc['Study/Exp/modA', 'effect_gate_passed']) and not bool(frame.loc['Study/Exp/modC', 'effect_gate_passed'])
    assert frame.loc['Study/Exp/modA', 'median_std_effect'] == pytest.approx(0.6)
    assert sorted(frame.loc['Study/Exp/modA', 'r_cohorts']) == ['CohortA (shared donors)', 'CohortB']


def test_module_card_prefers_pseudobulk_flags_ab_disagreement_and_has_a_forest(modules):
    card = modules.module_effects('Study/Exp', 'modB', VIEW_DATASETS, 'braak')
    entry = _replication(card)
    assert entry['primary'] == {'method': 'pseudobulk', 'source': 'gene_list', 'label': fm.variant_label('pseudobulk', 'gene_list')}
    assert [v['method'] for v in entry['variants']] == ['pseudobulk', 'cell_mean']
    assert entry['ab']['disagree'] is True and 'opposite' in entry['ab']['reason']
    pseudobulk = entry['variants'][0]
    assert pseudobulk['call'] == 'replicated' and pseudobulk['r_direction'] == 'down'
    assert pseudobulk['strict'] is None and pseudobulk['r_strong'] is None            # modules have no strict tier
    assert pseudobulk['cohorts_label'] == '2/2 cohorts, 1 small'
    point = pseudobulk['forest'][0]['anchor']
    assert point['estimate'] == pytest.approx(-0.3) and point['ci_low'] == pytest.approx(-0.4)
    assert point['std_effect'] == pytest.approx(-0.6) and point['p_perm'] == pytest.approx(0.001)
    assert card['units'] and card['n_units'] == len(card['units'])
    quiet = _replication(modules.module_effects('study/exp', 'MODA', VIEW_DATASETS, 'braak'))
    assert quiet['ab']['disagree'] is False                                          # lookup is case-insensitive


def test_module_card_for_an_untested_module_and_other_scopes_is_empty(modules):
    card = modules.module_effects('Study/Exp', 'nothing', VIEW_DATASETS, 'braak')
    assert card['replication'] == [] and card['units'] == []
    xeno = modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak', scope='xenograft_human_in_mouse')
    assert xeno['replication'] == []
    one = modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak', cell_type='microglia')
    assert [r['celltype_group'] for r in one['replication']] == ['microglia']
    other = modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak', cell_type='astrocyte')
    assert other['replication'] == []


def test_top_modules_rank_filter_and_page(modules):
    body = modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia')
    assert [row['module'] for row in body['rows']] == ['modA', 'modB'] or {row['module'] for row in body['rows']} == {'modA', 'modB'}
    assert body['n_replicated'] == 2 and body['n_effect_gate'] == 2 and body['n_modules_in_family'] == 3
    row = next(r for r in body['rows'] if r['module'] == 'modB')
    assert row['ab']['disagree'] is True and row['other_variants'][0]['method'] == 'cell_mean'
    assert row['geneset_id'] == 'Study/Exp/modB' and row['strict'] is None
    everything = modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia', include_not_replicated=True)
    assert everything['n_matching'] == 3 and everything['rows'][-1]['module'] == 'modC'
    page = modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia', include_not_replicated=True, limit=1, offset=2)
    assert page['n_returned'] == 1 and page['has_more'] is False and page['rows'][0]['module'] == 'modC'
    assert modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia', catalog='Other/Cat')['n_matching'] == 0
    default = modules.top_modules('braak', VIEW_DATASETS)
    assert default['celltype_group'] == 'microglia' and default['cohorts_in_cell_type'] == 2
    assert modules.top_modules('braak', VIEW_DATASETS, cell_type='astrocyte')['rows'] == []
    assert modules.top_modules('apoe4_carrier', VIEW_DATASETS)['cell_type'] is None


def test_scored_in_names_the_datasets_a_module_was_tested_in(modules):
    scored = modules.scored_in('Study/Exp', 'modA', VIEW_DATASETS)
    assert [entry['db_id'] for entry in scored] == ['G1/ds1', 'G2/ds2']
    first = scored[0]
    assert first['has_counts'] is True and first['variants'] == ['cell_mean/geneset', 'pseudobulk/gene_list']
    assert first['facets'] == ['braak'] and first['groups'] == ['microglia'] and first['counted'] is True
    assert modules.scored_in('Study/Exp', 'nothing', VIEW_DATASETS) == []


def test_module_tables_are_cached_until_a_file_changes(modules, views):
    first = modules.tables(VIEW_DATASETS)
    assert modules.tables(VIEW_DATASETS) is first
    families = modules.families('braak', VIEW_DATASETS)
    assert modules.families('braak', VIEW_DATASETS) is families
    _module_frame('G2/ds2', 'braak', donors=31).to_parquet(views / 'G2' / 'ds2.facet_module_tests.parquet')
    assert modules.tables(VIEW_DATASETS) is not first


def test_warm_builds_every_module_family(modules):
    assert modules.warm(VIEW_DATASETS) == 1
    assert modules.families('braak', VIEW_DATASETS) is modules.families('braak', VIEW_DATASETS)


def test_descriptive_only_module_units_do_not_count(views):
    _module_frame('G2/ds2', 'braak', donors=3, descriptive=True).to_parquet(views / 'G2' / 'ds2.facet_module_tests.parquet')
    modules = fm.ModuleResults(fr.FacetResults(views))
    assert modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia')['rows'] == []
    card = modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak')
    assert _replication(card)['variants'][0]['call'] == 'not_tested'
    assert _replication(card)['variants'][0]['nt_label'] == '1 cohort'


def test_required_donor_exclusion_must_be_recorded_by_the_module_run(views):
    table = views / fr.COHORT_TABLE_NAME
    lines = table.read_text().splitlines()
    header = lines[0].split('\t') + ['donor_exclusion']
    rows = [line.split('\t') + ['donors of CohortB'] if '\tG2/ds2\t' in line else line.split('\t') + ['']
            for line in lines[1:]]
    table.write_text('\n'.join('\t'.join(parts) for parts in [header] + rows) + '\n')
    modules = fm.ModuleResults(fr.FacetResults(views))
    assert modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia')['rows'] == []
    manifest = views / 'G2' / 'ds2.facet_module_run_manifest.json'
    manifest.write_text(json.dumps({'status': 'done', 'donor_exclusion': {'counted': True}}))
    modules = fm.ModuleResults(fr.FacetResults(views))
    assert {row['module'] for row in modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia')['rows']} == {
        'modA', 'modB'}


@pytest.mark.parametrize('a, b, disagree', [
    ({'median_std_effect': 0.5, 'call': 'replicated'}, {'median_std_effect': -0.1, 'call': 'not_replicated'}, True),
    ({'median_std_effect': 0.3, 'call': 'not_replicated'}, {'median_std_effect': -0.25, 'call': 'not_replicated'}, True),
    ({'median_std_effect': 0.1, 'call': 'not_replicated'}, {'median_std_effect': -0.05, 'call': 'not_replicated'}, False),
    ({'median_std_effect': 0.5, 'call': 'replicated'}, {'median_std_effect': 0.4, 'call': 'replicated'}, False),
    ({'median_std_effect': None, 'call': 'not_tested'}, {'median_std_effect': 0.4, 'call': 'replicated'}, False),
])
def test_ab_disagreement_needs_opposite_signs_and_a_real_signal(a, b, disagree):
    verdict = fm.ab_disagreement(a, b)
    assert verdict['disagree'] is disagree and (verdict['reason'] is not None) is disagree


def test_ab_disagreement_is_absent_without_both_variants():
    assert fm.ab_disagreement(None, {'median_std_effect': 1.0}) is None
    assert fm.ab_disagreement({'median_std_effect': 1.0}, None) is None


def test_quality_flags_and_directionless_reason():
    mito = fm.quality_flags(['MT-CO1', 'MT-ND1', 'MT-ATP6', 'TREM2'])
    assert mito['mt_dominated'] and mito['quality_sensitive'] and mito['mt_fraction'] == 0.75
    ribo = fm.quality_flags(['RPL3', 'RPS6', 'APOE'])
    assert ribo['ribosomal_dominated'] and ribo['quality_sensitive'] and not ribo['mt_dominated']
    clean = fm.quality_flags(['TREM2', 'APOE', 'CD9'])
    assert not clean['quality_sensitive']
    assert fm.quality_flags([])['quality_sensitive'] is False
    assert fm.directionless_reason('GWAS0000', 'other', None, None) == 'GWAS gene list'
    assert fm.directionless_reason('X', 'other', None, 'MAGMA 1.10') == 'association gene list'
    assert fm.directionless_reason('X', 'wgcna', 'bidirectional', None) == 'direction is bidirectional'
    assert fm.directionless_reason('Mancuso2024', 'scrnaseq', 'up', 'Seurat') is None


# ── endpoints ────────────────────────────────────────────────────────────────

class _Experiment:
    study, name, method = 'Study', 'Exp', 'wgcna'
    metadata: Dict[str, Any] = {}


class _Geneset:
    """Just what ``_module_info`` and the context flags read from a geneset-store entry."""

    def __init__(self, name: str, genes: List[str], direction: str = 'up', role: Any = None):
        self.name, self.genes, self.organism, self.celltypes, self.role = name, genes, 'human', ['microglial cell'], role
        self.metadata = {'name': name.upper(), 'description': 'a test set', 'direction': direction}
        self.experiment = _Experiment()


class _GenesetRegistry:
    def __init__(self, *genesets: _Geneset, root: Any = None):
        self._by_id = {f'Study/Exp/{g.name}': g for g in genesets}
        self.genesets = self._by_id
        self.root = root

    def get(self, geneset_id: str):
        return self._by_id.get(geneset_id)


STATS_ROWS = [
    {'dataset': 'G1/ds1', 'gene': 'PTPRG', 'n_cells': 2000, 'pct_nonzero': 3.0},
    {'dataset': 'G2/ds2', 'gene': 'PTPRG', 'n_cells': 3000, 'pct_nonzero': 2.0},
    {'dataset': 'G1/ds1', 'gene': 'APOE', 'n_cells': 2000, 'pct_nonzero': 70.0},
    {'dataset': 'G2/ds2', 'gene': 'APOE', 'n_cells': 3000, 'pct_nonzero': 60.0},
    # neuronal and oligodendrocyte markers: rare in the two microglia datasets, common in their own cell type
    {'dataset': 'G1/ds1', 'gene': 'SNAP25', 'n_cells': 2000, 'pct_nonzero': 1.0},
    {'dataset': 'G2/ds2', 'gene': 'SNAP25', 'n_cells': 3000, 'pct_nonzero': 2.0},
    {'dataset': 'G5/ds5', 'gene': 'SNAP25', 'n_cells': 4000, 'pct_nonzero': 60.0},
    {'dataset': 'G1/ds1', 'gene': 'MBP', 'n_cells': 2000, 'pct_nonzero': 0.0},
    {'dataset': 'G2/ds2', 'gene': 'MBP', 'n_cells': 3000, 'pct_nonzero': 1.0},
    {'dataset': 'G6/ds6', 'gene': 'MBP', 'n_cells': 4000, 'pct_nonzero': 45.0},
    # a neuronal-panel gene that microglia express broadly (PTPRG-like), and one that all cell types express
    {'dataset': 'G1/ds1', 'gene': 'PTPRG2', 'n_cells': 2000, 'pct_nonzero': 35.0},
    {'dataset': 'G2/ds2', 'gene': 'PTPRG2', 'n_cells': 3000, 'pct_nonzero': 30.0},
    {'dataset': 'G5/ds5', 'gene': 'PTPRG2', 'n_cells': 4000, 'pct_nonzero': 90.0},
    {'dataset': 'G1/ds1', 'gene': 'HOUSE', 'n_cells': 2000, 'pct_nonzero': 95.0},
    {'dataset': 'G5/ds5', 'gene': 'HOUSE', 'n_cells': 4000, 'pct_nonzero': 99.0},
]
VIEW_METADATA = {
    'G1/ds1': {'facet_scope': 'human_postmortem', 'cell_type': 'microglial cell', 'n_cells': 2000, 'title': 'Dataset one',
               'suspension_type': 'nucleus', 'default_layer': 'X'},
    'G2/ds2': {'facet_scope': 'human_postmortem', 'cell_type': 'microglial cell', 'n_cells': 3000, 'title': 'Dataset two',
               'suspension_type': 'cell', 'default_layer': 'X'},
    'G5/ds5': {'facet_scope': 'human_postmortem', 'cell_type': 'excitatory neuron', 'n_cells': 4000, 'title': 'Neurons'},
    'G6/ds6': {'facet_scope': 'human_postmortem', 'cell_type': 'oligodendrocyte', 'n_cells': 4000, 'title': 'Oligos'},
    'M1/mus': {'suspension_type': 'cell'},
}


@pytest.fixture
def api(views: Path, tmp_path: Path):
    store = tmp_path / 'genesetstore'
    (store / '_orthologs').mkdir(parents=True)
    (store / gene_classes.ORTHOLOG_FILE).write_text('mouse\thuman\nApoe\tAPOE\nCd9\tCD9\nXist\tXIST\n')
    registry = _GenesetRegistry(_Geneset('modA', ['PTPRG', 'APOE', 'CD9', 'NOT_IN_DATA']),
                                _Geneset('gwas', ['MT-CO1', 'MT-ND1'], direction='bidirectional'),
                                _Geneset('Neurons', ['PTPRG', 'PTPRG2', 'HOUSE', 'SNAP25', 'APOE'], role='markers'),
                                _Geneset('Oligodendrocytes', ['MBP'], role='markers'),
                                _Geneset('Microglia', ['APOE', 'P2RY12'], role='markers'),
                                root=store)
    return build_serve(views, geneset_registry=registry, stats_rows=STATS_ROWS, metadata=VIEW_METADATA,
                       extra_databases=('G5/ds5', 'G6/ds6'))


def test_endpoint_gene_detection(api):
    body = api.get('/genes/PTPRG/detection').json()
    summary = {(row['scope'], row['celltype_group']): row for row in body['summary']}
    assert summary[('human_postmortem', 'microglia')]['low_detection'] is True
    assert summary[('human_postmortem', 'microglia')]['pct_median'] == 2.5 and body['partial'] is True
    listed = {entry['db_id']: entry for entry in body['datasets']}
    assert listed['G1/ds1']['modality'] == 'nuclei' and listed['G2/ds2']['modality'] == 'cells'
    assert api.get('/genes/APOE/detection').json()['summary'][0]['low_detection'] is False


def test_gene_effect_cards_carry_the_low_detection_pill(api):
    card = api.get('/facets/braak/effects', params={'gene': 'PTPRG'}).json()
    entry = _replication(card)
    assert entry['low_detection'] is True and entry['detection']['n_datasets'] == 2
    allf = api.get('/genes/PTPRG/facet-effects').json()
    assert _replication(allf['facets']['braak'])['low_detection'] is True
    assert _replication(allf['by_scope']['human_postmortem']['braak'])['detection']['pct_median'] == 2.5
    apoe = api.get('/facets/braak/effects', params={'gene': 'APOE'}).json()
    assert _replication(apoe)['low_detection'] is False
    missing = api.get('/facets/braak/effects', params={'gene': 'XIST'}).json()
    assert all(entry['detection'] is None and entry['low_detection'] is None for entry in missing['replication'])


def test_endpoint_top_genes_pages_filters_and_carries_detection(api):
    body = api.get('/facets/ant_braak/effects/top', params={'cell_type': 'microglia'}).json()
    row = next(r for r in body['rows'] if r['gene'] == 'PTPRG')
    assert row['low_detection'] is True and row['detection']['pct_median'] == 2.5
    assert row['small_cohorts'] == ['CohortA']
    assert body['headline']['n_strict'] >= 1 and body['n_matching'] == body['n_returned']
    big = api.get('/facets/ant_braak/effects/top', params={'n': 5000, 'offset': 0}).json()
    assert big['has_more'] is False
    page = api.get('/facets/ant_braak/effects/top', params={'n': 1, 'offset': 99}).json()
    assert page['rows'] == [] and page['offset'] == 99
    gated = api.get('/facets/ant_braak/effects/top', params={'effect_gate': 'true', 'strict': 'true'}).json()
    assert all(r['replicated_effect'] for r in gated['rows'])
    assert api.get('/facets/ant_braak/effects/top', params={'n': 0}).status_code == 422
    assert api.get('/facets/ant_braak/effects/top', params={'offset': -1}).status_code == 422


def test_endpoint_overview_lists_genes_once_with_detection(api):
    body = api.get('/facets/ant_braak/effects/overview').json()
    assert body['headline']['strict_genes'][0] == 'PTPRG' and body['scope'] == 'human_postmortem'
    gene = next(g for card in body['cell_types'] for g in card['genes'] if g['gene'] == 'PTPRG')
    assert gene['low_detection'] is True and gene['strict'] is True
    assert 'help_topics' in body
    assert api.get('/facets/ant_tissue/effects/overview').status_code == 404
    assert api.get('/facets/ant_braak/effects/overview', params={'scope': 'nowhere'}).status_code == 400
    assert api.get('/facets/ant_braak/effects/overview', params={'n': 0}).status_code == 422


def test_endpoint_facet_tests_rows_carry_modality(api):
    rows = api.get('/facets/ant_braak/tests').json()['rows']
    assert {row['db_id']: row['modality'] for row in rows}['G1/ds1'] == 'nuclei'
    assert {row['db_id']: row['modality'] for row in rows}['G2/ds2'] == 'cells'


def test_endpoint_top_modules(api):
    body = api.get('/facets/ant_braak/modules/top', params={'cell_type': 'microglia'}).json()
    assert {row['module'] for row in body['rows']} == {'modA', 'modB'} and body['n_replicated'] == 2
    mod_a = next(row for row in body['rows'] if row['module'] == 'modA')
    assert mod_a['directionless'] is False and mod_a['quality_sensitive'] is False and mod_a['n_genes'] == 4
    mod_b = next(row for row in body['rows'] if row['module'] == 'modB')
    assert mod_b['ab']['disagree'] is True and mod_b['n_genes'] is None             # not in the geneset store
    assert 'module-replication' in body['help_topics']
    page = api.get('/facets/ant_braak/modules/top', params={'cell_type': 'microglia', 'n': 1, 'offset': 1,
                                                            'include_not_replicated': 'true'}).json()
    assert page['n_returned'] == 1 and page['has_more'] is True
    gate = api.get('/facets/ant_braak/modules/top', params={'effect_gate': 'true'}).json()
    assert gate['n_matching'] == 2
    assert api.get('/facets/ant_tissue/modules/top').status_code == 404
    assert api.get('/facets/ant_braak/modules/top', params={'scope': 'nowhere'}).status_code == 400


def test_endpoint_geneset_facet_effects_joins_module_and_member_results(api):
    body = api.get('/genesets/Study/Exp/modA/facet-effects', params={'facet': 'braak'}).json()
    assert body['geneset_id'] == 'Study/Exp/modA' and body['n_genes'] == 4 and body['directionless'] is False
    assert body['quality']['quality_sensitive'] is False and body['celltypes'] == ['microglial cell']
    assert [entry['db_id'] for entry in body['scored_in']] == ['G1/ds1', 'G2/ds2']
    card = body['facets']['braak']
    assert card['scope'] == 'human_postmortem'
    module = _replication(card['module'])
    assert module['primary']['method'] == 'pseudobulk' and module['variants'][0]['call'] == 'replicated'
    assert card['module']['units'] == []                                            # include_units defaults to false
    members = card['members']['groups']['microglia']
    assert members['n_members'] == 4 and members['genes_up'] == ['PTPRG'] and members['n_in_data'] == 2
    assert set(body['by_scope']) == {'human_postmortem'} and 'braak' in body['by_scope']['human_postmortem']
    assert 'module-replication' in body['help_topics']
    flat = api.get('/genesets/Study/Exp/modA/facet-effects', params={'facet': 'braak', 'include_units': 'true',
                                                                      'scope': 'human_postmortem'}).json()
    assert flat['facets']['braak']['module']['units'] and 'by_scope' not in flat


def test_endpoint_forest_modes(api):
    full = api.get('/facets/braak/effects', params={'gene': 'PTPRG'}).json()
    assert any(row['more_units'] for row in _replication(full)['forest'])
    anchors = api.get('/facets/braak/effects', params={'gene': 'PTPRG', 'forest': 'anchors'}).json()
    assert all(row['more_units'] == [] for row in _replication(anchors)['forest'])
    assert api.get('/facets/braak/effects', params={'gene': 'PTPRG', 'forest': 'none'}).json()['replication'][0]['forest'] == []
    assert api.get('/facets/braak/effects', params={'gene': 'PTPRG', 'forest': 'x'}).status_code == 400
    everything = api.get('/genes/PTPRG/facet-effects').json()                        # the all-facets payload defaults to anchors
    assert all(row['more_units'] == [] and row['n_more_units'] in (0, 1)
               for row in _replication(everything['facets']['braak'])['forest'])
    assert _replication(everything['facets']['braak'])['forest'][0]['anchor']['ci_high'] is not None
    wide = api.get('/genes/PTPRG/facet-effects', params={'forest': 'full'}).json()
    assert any(row['more_units'] for row in _replication(wide['facets']['braak'])['forest'])
    assert api.get('/genes/PTPRG/facet-effects', params={'forest': 'x'}).status_code == 400
    module = api.get('/genesets/Study/Exp/modA/facet-effects', params={'facet': 'braak', 'forest': 'none'}).json()
    assert all(v['forest'] == [] for e in module['facets']['braak']['module']['replication'] for v in e['variants'])
    assert api.get('/genesets/Study/Exp/modA/facet-effects', params={'forest': 'x'}).status_code == 400


def test_endpoint_geneset_marks_directionless_sets_and_unknown_ids(api):
    body = api.get('/genesets/Study/Exp/gwas/facet-effects', params={'facet': 'braak'}).json()
    assert body['directionless'] is True and 'not suited' in body['directionless_note']
    assert body['quality']['quality_sensitive'] is True and body['scored_in'] == []
    assert body['facets']['braak']['module']['replication'] == []
    assert api.get('/genesets/Study/Exp/nothing/facet-effects').status_code == 404
    assert api.get('/genesets/Study/Exp/modA/facet-effects', params={'facet': 'tissue'}).status_code == 404
    assert api.get('/genesets/Study/Exp/modA/facet-effects', params={'scope': 'nowhere'}).status_code == 400


def test_endpoint_geneset_facet_effects_without_a_geneset_store_still_answers_tested_modules(views):
    client = build_serve(views, geneset_registry=None, metadata=VIEW_METADATA, extra_databases=('G5/ds5', 'G6/ds6'))
    body = client.get('/genesets/Study/Exp/modA/facet-effects', params={'facet': 'braak'}).json()
    assert body['n_genes'] is None and body['facets']['braak']['members'] is None
    assert _replication(body['facets']['braak']['module'])['variants'][0]['call'] == 'replicated'
    assert client.get('/genesets/Study/Exp/nothing/facet-effects').status_code == 404


def test_module_info_is_cached_until_the_store_is_rescanned(api):
    cache = api.namespace['_MODULE_INFO_CACHE']
    api.get('/genesets/Study/Exp/modA/facet-effects', params={'facet': 'braak'})
    assert 'Study/Exp/modA' in cache and cache['Study/Exp/modA']['genes'][0] == 'PTPRG'
    assert api.namespace['_module_info']('Study/Exp/absent') is None


def test_detection_tables_follow_the_index_snapshot(api):
    first = api.namespace['_detection_tables']()
    assert api.namespace['_detection_tables']() is first
    assert ('human_postmortem', 'microglia') in first
    api.registry.gene_stats_index = type(api.registry.gene_stats_index)(None)
    assert api.namespace['_detection_tables']() == {}


# ── context gene flags: foreign lineage and no mouse ortholog ────────────────

def test_foreign_lineage_symbols_come_from_non_myeloid_marker_panels():
    panels = [_Geneset('Neurons', ['snap25', 'PTPRG', 'APOE'], role='markers'),
              _Geneset('astro', ['GFAP'], role='markers'),
              _Geneset('micro', ['P2RY12', 'APOE'], role='markers'),
              _Geneset('Macrophages', ['CD68'], role='markers'),
              _Geneset('DAM', ['NEURON_LIKE'], role=None)]
    foreign = gene_classes.foreign_lineage_symbols(panels)
    assert foreign == {'SNAP25', 'PTPRG', 'GFAP'}                                  # APOE is also a microglial marker
    assert gene_classes.foreign_lineage_symbols([]) == frozenset()


@pytest.mark.parametrize('pcts, expected', [
    ({'microglia': 1.0, 'excitatory neuron': 60.0}, True),                     # SNAP25-like
    ({'microglia': 0.0, 'oligodendrocyte': 3.0}, True),                        # MBP-like, floor of 0.5 percent
    ({'microglia': 0.0, 'oligodendrocyte': 2.0}, False),                       # under 5 x the floor
    ({'microglia': 35.0, 'excitatory neuron': 90.0}, False),                   # PTPRG-like: a microglial state gene
    ({'microglia': 10.0, 'excitatory neuron': 99.0}, False),                   # 10 percent or more: never
    ({'microglia': 8.0, 'excitatory neuron': 30.0}, False),                    # under 5 x
    ({'microglia': 8.0, 'excitatory neuron': 40.0}, True),
    ({'microglia': 1.0, 'perivascular macrophage': 80.0}, False),              # other myeloid groups do not count
    ({'microglia': 1.0}, False),                                               # nowhere else detected
    ({'excitatory neuron': 60.0}, False),                                      # microglia unknown
    ({}, False), (None, False),
])
def test_foreign_by_detection_rule(pcts, expected):
    assert gene_classes.foreign_by_detection(pcts) is expected


def test_context_flags_depend_on_group_and_scope():
    foreign, orthologs = frozenset({'PTPRG'}), frozenset({'APOE'})
    flags = gene_classes.context_flags
    rare = {'microglia': 1.0, 'excitatory neuron': 60.0}
    assert flags('ptprg', 'microglia', 'human_postmortem', foreign, orthologs, rare) == ['foreign_lineage_marker']
    assert flags('ptprg', 'microglia', 'human_postmortem', foreign, orthologs) == []     # no detection data: not flagged
    assert flags('PTPRG', 'microglia', 'human_postmortem', foreign, orthologs, {'microglia': 35.0, 'neuron (mixed)': 90.0}) == []
    assert flags('PTPRG', 'astrocyte', 'human_postmortem', foreign, orthologs, rare) == []
    assert flags('PTPRG', 'perivascular macrophage', 'xenograft_human_in_mouse', foreign, orthologs, rare) == [
        'foreign_lineage_marker', 'no_mouse_ortholog']
    assert flags('APOE', 'microglia', 'xenograft_human_in_mouse', foreign, orthologs) == []
    assert flags('TREM2', 'microglia', 'human_postmortem', foreign, orthologs) == []   # not xenograft: no ortholog flag
    assert flags('PTPRG', 'microglia', 'xenograft_human_in_mouse', None, None, rare) == []   # sources missing: never guessed


def test_human_orthologs_table_is_read_once_per_file_state(tmp_path):
    table = tmp_path / 'orthologs.tsv'
    assert gene_classes.human_orthologs(table) is None
    table.write_text('mouse\thuman\nApoe\tAPOE\nOne\t\nCd9\tcd9\nbroken\n')
    first = gene_classes.human_orthologs(table)
    assert first == {'APOE', 'CD9'} and gene_classes.human_orthologs(table) is first
    table.write_text('mouse\thuman\nApoe\tAPOE\n')
    assert gene_classes.human_orthologs(table) == {'APOE'}


def test_gene_cards_carry_context_flags_per_cell_type_group(api):
    card = api.get('/facets/braak/effects', params={'gene': 'PTPRG'}).json()
    assert 'foreign_lineage_marker' not in _replication(card)['gene_flags']      # a panel gene, but no neuron data here
    assert 'foreign_lineage_marker' not in card['gene_flags'] and 'no_mouse_ortholog' not in card['gene_flags']
    apoe = api.get('/facets/braak/effects', params={'gene': 'APOE'}).json()
    assert 'foreign_lineage_marker' not in _replication(apoe)['gene_flags'] and 'apoe_locus' in apoe['gene_flags']
    xeno = api.get('/facets/app_nlgf/effects', params={'gene': 'Trem2'}).json()
    assert xeno['scope'] == 'xenograft_human_in_mouse' and 'no_mouse_ortholog' in xeno['gene_flags']
    assert 'no_mouse_ortholog' in _replication(xeno)['gene_flags']
    cd9 = api.get('/facets/app_nlgf/effects', params={'gene': 'Cd9'}).json()
    assert 'no_mouse_ortholog' not in cd9['gene_flags']
    allf = api.get('/genes/PTPRG/facet-effects').json()
    assert 'foreign_lineage_marker' not in _replication(allf['facets']['braak'])['gene_flags']


def test_foreign_lineage_flag_needs_rarity_in_microglia_and_richness_elsewhere(api):
    flags = api.namespace['_context_gene_flags']
    assert flags('SNAP25', 'microglia', 'human_postmortem', []) == ['foreign_lineage_marker']   # neuronal marker
    assert flags('MBP', 'microglia', 'human_postmortem', []) == ['foreign_lineage_marker']      # oligodendrocyte marker
    assert flags('SNAP25', 'astrocyte', 'human_postmortem', []) == []                           # not a myeloid group
    assert flags('PTPRG2', 'microglia', 'human_postmortem', []) == []       # in a neuron panel, 30 percent in microglia
    assert flags('HOUSE', 'microglia', 'human_postmortem', []) == []        # expressed everywhere
    assert flags('PTPRG', 'microglia', 'human_postmortem', []) == []        # rare here, but nowhere else detected
    assert 'foreign_lineage_marker' not in flags('SNAP25', 'microglia', 'xenograft_human_in_mouse', [])   # no xenograft detection


def test_top_rows_and_overview_genes_carry_context_flags(api):
    top = api.get('/facets/ant_braak/effects/top', params={'cell_type': 'microglia'}).json()
    row = next(r for r in top['rows'] if r['gene'] == 'PTPRG')
    assert 'foreign_lineage_marker' not in row['gene_flags'] and 'gene_flags' in row
    overview = api.get('/facets/ant_braak/effects/overview').json()
    gene = next(g for card in overview['cell_types'] for g in card['genes'] if g['gene'] == 'PTPRG')
    assert 'foreign_lineage_marker' not in gene['gene_flags'] and 'gene_flags' in gene


def test_context_flags_are_absent_without_a_geneset_store(views):
    client = build_serve(views, geneset_registry=None, metadata=VIEW_METADATA, extra_databases=('G5/ds5', 'G6/ds6'))
    card = client.get('/facets/braak/effects', params={'gene': 'PTPRG'}).json()
    assert 'foreign_lineage_marker' not in _replication(card)['gene_flags']
    assert client.namespace['_gene_context']() == (None, None)


# ── routes, MCP, version, docs ───────────────────────────────────────────────

def test_new_routes_are_registered_in_a_safe_order():
    source = (MCP_PATH.parent / 'ant-serve').read_text()
    for route in ('"/genes/{gene}/detection"', '"/facets/{facet_name}/effects/overview"',
                  '"/facets/{facet_name}/modules/top"', '"/genesets/{study}/{experiment}/{name}/facet-effects"'):
        assert f'@app.get({route}' in source, route
    assert source.index('@app.get("/facets/cohorts"') < source.index('@app.get("/facets/{facet_name}",')


def _mcp_tools() -> Dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(MCP_PATH.read_text())
    tools = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == 'tool'):
                    tools[node.name] = node
    return tools


def test_mcp_exposes_module_effects_and_paging_in_plain_words():
    tools = _mcp_tools()
    assert {'module_facet_effects', 'facet_top_modules'} <= set(tools)
    for name in ('module_facet_effects', 'facet_top_modules'):
        text = ast.get_docstring(tools[name])
        assert 'EXPLORATORY' in text and '—' not in text, name
    assert 'member genes' in ast.get_docstring(tools['module_facet_effects'])
    arguments = [a.arg for a in tools['facet_top_genes'].args.args]
    assert {'offset', 'effect_gate', 'strict'} <= set(arguments)
    assert 'exclude_secondary' in [a.arg for a in tools['module_facet_effects'].args.args]
    source = MCP_PATH.read_text()
    assert 'module_facet_effects' in source.split('REPLICATE-LEVEL FACET EFFECTS')[1].split('PLOTTING')[0]


def test_version_and_changelog_name_2_45_0():
    source = (MCP_PATH.parent / 'ant-serve').read_text()
    assert 'API_VERSION = "2.45.0"' in source
    changelog = (MCP_PATH.parent.parent.parent / 'doc' / 'API_CHANGELOG.md').read_text()
    assert '## 2.45.0' in changelog and changelog.index('## 2.45.0') < changelog.index('## 2.44.0')
    assert '—' not in changelog.split('## 2.45.0')[1].split('## 2.44.0')[0]


def test_module_variants_carry_their_rank_in_the_family_and_top_rows_the_effect_range(modules):
    best = _replication(modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak'))['variants'][0]
    noise = _replication(modules.module_effects('Study/Exp', 'modC', VIEW_DATASETS, 'braak'))['variants'][0]
    assert best['family_size'] == noise['family_size'] >= 3
    assert 1 <= best['family_rank'] < noise['family_rank'] <= best['family_size']
    row = modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia', include_not_replicated=True)['rows'][0]
    assert row['min_effect'] <= row['median_std_effect'] <= row['max_effect']
