"""Facet engine v3 (2026-10-06): facet scopes, thresholds, new facets, module replication.

* ``lib/facet_scopes.py``: scope of a dataset from ``.h5ad.mus`` and its published facets.
* ``lib/obscol_tags_vocab.yaml``: ``facet_scopes`` per facet, the ``app_nlgf`` registry entry.
* ``lib/facet_lint.py`` / ``lib/md_check.py``: ``facet_scope_mismatch``.
* ``lib/facet_rules.py``: the statistical-minimum thresholds and the effect-gate taus.
* ``bin/annot/ant-facet-test``: scope-aware runs, ``app_nlgf`` and ``age`` end to end (R needed).
* ``bin/annot/ant-facet-test-modules``: partial-conjunction replication of modules.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import facet_lint  # noqa: E402
import facet_results  # noqa: E402
import facet_rules  # noqa: E402
import facet_scopes  # noqa: E402
import gene_classes  # noqa: E402
import obscol_tags  # noqa: E402
import replicates  # noqa: E402
from test_facet_test import RSCRIPT, aft, make_dataset, needs_r  # noqa: E402
from test_facet_test_modules import mod  # noqa: E402


# ── lib/facet_scopes.py ──────────────────────────────────────────────────────

@pytest.mark.parametrize('annotation, columns, expected', [
    ({'organism': 'human', 'experimental_system': 'post_mortem_tissue'}, (), 'human_postmortem'),
    ({'organism': 'human', 'experimental_system': 'postmortem_tissue'}, (), 'human_postmortem'),
    ({'organism': 'human', 'experimental_system': 'fresh_biopsy'}, (), 'human_postmortem'),
    ({'organism': 'human', 'experimental_system': 'live_biopsy'}, (), 'human_postmortem'),
    ({'organism': 'human'}, (), 'human_postmortem'),
    ({'organism': 'human', 'experimental_system': 'xenograft'}, (), 'xenograft_human_in_mouse'),
    ({'organism': 'human', 'experimental_system': 'chimera'}, (), 'xenograft_human_in_mouse'),
    ({'organism': 'human', 'experimental_system': 'post_mortem_tissue'}, ('ant_host_sex',), 'xenograft_human_in_mouse'),
    ({'organism': 'mouse', 'experimental_system': 'post_mortem_tissue'}, (), None),      # mouse_in_vivo was retired
    ({'organism': 'mouse'}, (), None),
    ({'organism': 'mouse', 'experimental_system': 'xenograft'}, (), None),               # a labelling slip: still pure mouse
    ({'organism': 'human', 'experimental_system': 'in_vitro_2d'}, (), 'in_vitro'),
    ({'organism': 'mouse', 'experimental_system': 'organoid'}, (), None),                # pure mouse, any system
    ({'organism': 'human', 'experimental_system': 'bulk_tissue'}, (), None),
    ({'organism': ['human', 'mouse'], 'experimental_system': 'post_mortem_tissue'}, (), None),
    ({'organism': 'rat'}, (), None),
    ({}, (), None),
    ({'organism': 'human', 'experimental_system': 'something_new'}, (), None),
])
def test_scope_of(annotation, columns, expected):
    assert facet_scopes.scope_of(annotation, columns) == expected


def test_missing_organism_is_human_only_for_the_listed_wachter_files():
    assert facet_scopes.scope_of({'experimental_system': 'post_mortem_tissue'}, db_id='WA4/MEC') == 'human_postmortem'
    assert facet_scopes.scope_of({'experimental_system': 'post_mortem_tissue'}, db_id='XX9/ABC') is None


def test_check_scope_and_default():
    assert facet_scopes.check_scope(None) == facet_scopes.check_scope('') == 'human_postmortem'
    assert facet_scopes.check_scope('xenograft_human_in_mouse') == 'xenograft_human_in_mouse'
    with pytest.raises(facet_scopes.ScopeError):
        facet_scopes.check_scope('mouse_in_vivo')
    assert facet_scopes.is_mouse_only({'organism': 'mouse'}) and not facet_scopes.is_mouse_only({'organism': 'human'})
    assert not facet_scopes.is_mouse_only({'organism': ['human', 'mouse']})
    with pytest.raises(facet_scopes.ScopeError):
        facet_scopes.check_scope('martian')
    assert set(facet_scopes.TESTED_SCOPES) == {'human_postmortem', 'xenograft_human_in_mouse'}


# ── the facet registry ───────────────────────────────────────────────────────

@pytest.mark.parametrize('facet, scopes', [
    ('sex', ['human_postmortem', 'xenograft_human_in_mouse', 'in_vitro']),
    ('genotype', ['human_postmortem', 'xenograft_human_in_mouse', 'in_vitro']),
    ('host_genotype', ['xenograft_human_in_mouse']),
    ('disease', ['human_postmortem']), ('braak', ['human_postmortem']), ('apoe4_carrier', ['human_postmortem']),
    ('adnc', ['human_postmortem']), ('cognitive_status', ['human_postmortem']), ('age', ['human_postmortem']),
    ('host_sex', ['xenograft_human_in_mouse']),
    ('app_nlgf', ['xenograft_human_in_mouse']),
    ('apoe', ['human_postmortem', 'xenograft_human_in_mouse']),
])
def test_registry_scopes_per_facet(facet, scopes):
    assert facet_scopes.facet_allowed_scopes(facet) == scopes
    assert facet_scopes.facet_allowed_scopes('ant_' + facet) == scopes


def test_every_facet_scope_name_in_the_vocab_is_known_and_app_nlgf_is_registered():
    vocab = yaml.safe_load(obscol_tags.VOCAB_PATH.read_text())
    for tag, entry in vocab['tags'].items():
        block = (entry or {}).get('facet') or {}
        declared = block.get('facet_scopes')
        if declared is None or declared == 'all':
            continue
        assert set(declared) <= set(facet_scopes.SCOPES), tag
    entry = obscol_tags.facet_registry()['app_nlgf']
    assert entry['status'] == 'registered' and entry['controlled_values'] == ['nlgf', 'control']
    assert obscol_tags.facet_definition('app_nlgf')['facet_scopes'] == ['xenograft_human_in_mouse']
    assert facet_scopes.facet_in_scope('app_nlgf', None) and not facet_scopes.facet_in_scope('app_nlgf', 'human_postmortem')


def test_unknown_facet_scope_in_the_vocab_is_an_error(monkeypatch):
    broken = {'tags': {'x': {'facet': {'status': 'registered', 'kind': 'categorical', 'facet_scopes': ['mars']}}}}
    monkeypatch.setattr(obscol_tags, 'load_vocab', lambda: broken)
    obscol_tags.facet_registry.cache_clear()
    try:
        with pytest.raises(obscol_tags.ObsColTagError):
            obscol_tags.facet_registry()
    finally:
        monkeypatch.undo()
        obscol_tags.facet_registry.cache_clear()


# ── lint: facet_scope_mismatch ───────────────────────────────────────────────

def test_lint_facet_scope_mismatch():
    api = {'obs_rename': {'Braak': 'ant_braak'}, 'obs_derive': {'ant_app_nlgf': {'source': 'g', 'map': {'a': 'nlgf'}}},
           'obs_constant': {'ant_sex': {'value': 'f'}}}
    mouse = facet_lint.lint_facet_scope(api, 'in_vitro')
    assert {f.rule for f in mouse} == {'facet_scope_mismatch'} and any('ant_braak' in f.message for f in mouse)
    human = facet_lint.lint_facet_scope(api, 'human_postmortem')
    assert [f.rule for f in human] == ['facet_scope_mismatch'] and 'ant_app_nlgf' in human[0].message
    assert facet_lint.lint_facet_scope(api, 'xenograft_human_in_mouse')[0].message.count('ant_braak') == 1
    assert facet_lint.lint_facet_scope(api, None) == []            # no scope: nothing to say
    assert facet_lint.lint_facet_scope({}, 'in_vitro') == []
    assert 'facet_scope_mismatch' in facet_lint.RULES


def test_lint_scope_of_dataset_uses_the_published_columns():
    annotation = {'organism': 'human', 'experimental_system': 'post_mortem_tissue'}
    api = {'obs_rename': {'sex': 'ant_host_sex'}}
    assert facet_lint.facet_scope_of_dataset(api, annotation) == 'xenograft_human_in_mouse'
    assert facet_lint.facet_scope_of_dataset({}, annotation) == 'human_postmortem'
    assert facet_lint.facet_scope_of_dataset({}, {}, 'WA4/MIT') == 'human_postmortem'     # listed: human by description
    assert facet_lint.facet_scope_of_dataset({}, {}, 'XX9/ABC') is None


def test_md_check_reports_a_facet_outside_its_scope(tmp_path):
    import md_check
    group = tmp_path / 'Mouse2026'
    group.mkdir()
    (group / 'index.md').write_text('---\ngroup: Mouse2026\ngroup_id: MO1\n---\n')
    (group / 'ds.md').write_text('---\nid: DSA\ntitle: ds\nversion: 0.1.0\nreplicate_unit: none\n'
                                 'api:\n  obs_rename:\n    Braak: ant_braak\n    Sex: ant_sex\n---\n')
    (group / 'ds.h5ad').write_bytes(b'')
    (group / 'ds.h5ad.mus').write_text('organism=mouse\nexperimental_system=post_mortem_tissue\n')
    report = md_check.MdCheckReport()
    md_check.check_md_folder(tmp_path, report, fix=False)
    messages = [item for item in report.errors if 'facet_scope_mismatch' in item]
    assert len(messages) == 2 and all('mouse-only' in m for m in messages)      # braak and sex: no facet in a pure mouse dataset
    assert 'facet_scope_mismatch' in ' '.join(report.rule_summary())


# ── lib/facet_rules.py and lib/replicates.py ─────────────────────────────────

def test_thresholds_are_the_statistical_minimum():
    assert (facet_rules.MIN_DONORS_PER_LEVEL, facet_rules.NUMERIC_MIN_REPLICATES,
            facet_rules.NUMERIC_MIN_DISTINCT, facet_rules.MIN_RESIDUAL_DF) == (3, 4, 3, 4)
    assert facet_rules.MIN_TEST_RESIDUAL_DF == 2 and facet_rules.LAMBDA_INFLATED == 1.3
    for unit in ('donor', 'animal', 'sample'):          # the fit minimum is the same for every unit
        assert facet_rules.min_per_level(unit) == 3 and facet_rules.min_numeric(unit) == 4
    # the p-value floor (decided 2026-10-07) differs: donors vary more than inbred animals
    assert (facet_rules.p_floor_per_level('donor'), facet_rules.p_floor_numeric('donor')) == (5, 10)
    assert (facet_rules.p_floor_per_level('animal'), facet_rules.p_floor_numeric('animal')) == (3, 6)
    assert (facet_rules.p_floor_per_level('sample'), facet_rules.p_floor_numeric('sample')) == (3, 6)
    assert facet_rules.SMALL_COHORT_REPLICATES == 20
    assert facet_rules.strong_tau('sex') == pytest.approx(math.log2(1.25))
    assert facet_rules.strong_tau('braak') == pytest.approx(math.log2(1.25) / 3)
    assert facet_rules.MIN_DONORS_DATASET == 4
    assert replicates.MIN_REPLICATES_PER_LEVEL == 3 and replicates.MIN_REPLICATES_NUMERIC == 4
    assert replicates.MIN_DISTINCT_NUMERIC == 3
    assert facet_rules.MIN_DONORS_FOR_AGE_PMI == 15                  # a power rule that never blocks a test
    assert facet_rules.BRAAK_MIN_DONORS == 4 and facet_rules.BRAAK_MIN_DISTINCT == 3


def test_gate_taus_per_facet():
    assert facet_rules.gate_tau('sex') == pytest.approx(math.log2(1.1))
    assert facet_rules.gate_tau('app_nlgf') == pytest.approx(math.log2(1.1))
    assert facet_rules.gate_tau('braak') == pytest.approx(math.log2(1.1) / 3)
    assert facet_rules.gate_tau('age') == pytest.approx(math.log2(1.1))      # per decade
    assert facet_rules.MODULE_STD_EFFECT_GATE == 0.2 and facet_rules.AGE_UNIT_YEARS == 10.0


# ── ant-facet-test end to end (R) ────────────────────────────────────────────

ANIMALS = 'replicate_unit: animal\nreplicate_column: ant_donor\n'
NLGF_DERIVE = ('  obs_derive:\n    ant_app_nlgf:\n      source: dx\n      dtype: categorical\n'
               '      map:\n        nlgf: nlgf\n        control: control\n')


@needs_r
def test_app_nlgf_in_a_xenograft_end_to_end_without_host_sex(tmp_path):
    pattern = ','.join(['nlgf', 'control'] * 6)                     # alternates, balanced over the two sexes
    store, entry = make_dataset(tmp_path, n_f=6, n_m=6, system='xenograft', disease_pattern=pattern,
                                extra_md=NLGF_DERIVE + ANIMALS)
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, list(aft.FACETS), RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'done' and manifest['facet_scope'] == 'xenograft_human_in_mouse'
    assert manifest['facets_tested_in_scope'] == ['sex', 'app_nlgf']
    assert {u['facet'] for u in manifest['units']} == {'sex', 'app_nlgf'}           # no disease, braak, age in mouse
    nlgf = [u for u in manifest['units'] if u['facet'] == 'app_nlgf' and u['status'] == 'ok']
    assert nlgf and nlgf[0]['facet_scope'] == 'xenograft_human_in_mouse' and nlgf[0]['covariates'] == []   # no host sex published
    assert nlgf[0]['level_counts'] == {'control': 6, 'nlgf': 6} and nlgf[0]['small_sample'] is False
    assert nlgf[0]['tau_log2'] == pytest.approx(math.log2(1.1))
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    assert set(table['facet']) == {'sex', 'app_nlgf'} and (table['facet_scope'] == 'xenograft_human_in_mouse').all()
    sub = table[table['facet'] == 'app_nlgf']
    assert set(sub['contrast']) == {'nlgf_vs_control'} and set(sub['level_ref']) == {'control'}
    sex = table[(table['facet'] == 'sex') & (table['gene'] == 'GENE0') & (table['cell_type'] == 'T1')]
    assert sex['logFC'].iloc[0] > 1.0 and sex['q_value'].iloc[0] < 0.05           # the planted effect is still found


@needs_r
def test_age_facet_end_to_end_per_decade_with_the_age_floor_and_the_adjusted_twin(tmp_path):
    pattern = ','.join(['AD', 'control'] * 10)
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, extra_rename='    age: ant_age\n', disease_pattern=pattern)
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['age', 'braak'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['facets_tested_in_scope'] == ['age', 'age_adjusted', 'braak', 'braak_threshold']
    age = [u for u in manifest['units'] if u['facet'] == 'age' and u['status'] == 'ok']
    assert age, manifest['units']
    unit = age[0]
    assert 'age' not in unit['covariates'] and 'sex' in unit['covariates']
    assert unit['tau_log2'] == pytest.approx(math.log2(1.1))
    # the default age floor of 60 years leaves the four donors aged 50 to 59 out of both age units
    assert unit['level_counts']['n_distinct'] == 16 and unit['level_counts']['value_min'] == pytest.approx(62.0)
    assert manifest['facet_min_age']['age']['n_donors_below_floor'] == 4
    assert unit['lambda_gc'] is not None and unit['inflated'] in (True, False) and unit['df_total_median'] > 0
    adjusted = [u for u in manifest['units'] if u['facet'] == 'age_adjusted' and u['status'] == 'ok']
    assert adjusted and 'pathology:disease' in adjusted[0]['covariates']
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    sub = table[table['facet'] == 'age']
    assert set(sub['contrast']) == {'age_per_decade'} and sub['level_ref'].isna().all()
    assert sub['persists_after_pathology_adjustment'].notna().all()
    assert set(table[table['facet'] == 'age_adjusted']['contrast']) == {'age_per_decade_pathology_adjusted'}
    assert not any(u['facet'] == 'braak' and u['status'] == 'ok' for u in manifest['units'])    # no Braak column


@needs_r
def test_app_nlgf_is_stratified_by_timepoint_and_refused_when_confounded(tmp_path):
    # genotype crosses the timepoint column (bank): one unit per timepoint, 6 vs 6 animals each
    pattern = ','.join('nlgf' if (index // 2) % 2 == 0 else 'control' for index in range(24))
    design = 'control_basis: wt_host\napp_nlgf_design:\n  timepoint_column: bank\n'
    store, entry = make_dataset(tmp_path / 'a', n_f=12, n_m=12, system='xenograft', disease_pattern=pattern,
                                extra_md=NLGF_DERIVE + ANIMALS + design)
    manifest = aft.process_dataset(entry, store, tmp_path / 'out_a', ['app_nlgf'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'done' and manifest['control_basis'] == 'wt_host'
    units = [u for u in manifest['units'] if u['facet'] == 'app_nlgf' and u['status'] == 'ok']
    assert {u['celltype_source'] for u in units} == {'ant_author_cell_type@timepoint=BA', 'ant_author_cell_type@timepoint=BB'}
    assert all(u['level_counts'] == {'control': 6, 'nlgf': 6} and u['control_basis'] == 'wt_host' for u in units)
    assert {u['design_subset'] for u in units} == {'timepoint=BA', 'timepoint=BB'}
    table = pd.read_parquet(tmp_path / 'out_a' / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    assert set(table['celltype_source']) == {u['celltype_source'] for u in units}
    # the injection-like covariate is aligned with the genotype: the unit is refused, not silently adjusted
    aligned = ','.join('nlgf' if index % 2 == 0 else 'control' for index in range(24))
    store, entry = make_dataset(tmp_path / 'b', n_f=12, n_m=12, system='xenograft', disease_pattern=aligned,
                                extra_md=NLGF_DERIVE + ANIMALS + 'control_basis: other\napp_nlgf_design:\n  covariate_columns: [bank]\n')
    manifest = aft.process_dataset(entry, store, tmp_path / 'out_b', ['app_nlgf'], RSCRIPT, threads=1, memory_limit_gb=1)
    statuses = {u['status'] for u in manifest['units'] if u['facet'] == 'app_nlgf'}
    assert statuses and all('confounded with bank' in status for status in statuses)


def test_a_missing_design_column_skips_the_dataset_with_a_reason(tmp_path):
    if RSCRIPT is None:
        pytest.skip('needs R')
    pattern = ','.join(['nlgf', 'control'] * 6)
    store, entry = make_dataset(tmp_path, n_f=6, n_m=6, system='xenograft', disease_pattern=pattern,
                                extra_md=NLGF_DERIVE + ANIMALS + 'app_nlgf_design:\n  timepoint_column: no_such_column\n')
    manifest = aft.process_dataset(entry, store, tmp_path / 'out', ['app_nlgf'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'skipped' and 'no_such_column' in manifest['reason']


@needs_r
def test_xenograft_scope_end_to_end_with_host_sex_covariate(tmp_path):
    pattern = ','.join(['nlgf', 'control'] * 6)
    store, entry = make_dataset(
        tmp_path, n_f=6, n_m=6, organism='human', system='xenograft', disease_pattern=pattern,
        extra_rename='    bank: ant_host_sex\n',
        extra_md=NLGF_DERIVE + ANIMALS)
    text = entry.md_path.read_text().replace('  obs_value_rename:\n', '  obs_value_rename:\n    bank:\n      BA: f\n      BB: m\n', 1)
    entry.md_path.write_text(text)
    manifest = aft.process_dataset(entry, store, tmp_path / 'out', list(aft.FACETS), RSCRIPT, threads=1,
                                   memory_limit_gb=1)
    assert manifest['status'] == 'done' and manifest['facet_scope'] == 'xenograft_human_in_mouse'
    assert manifest['facets_tested_in_scope'] == ['sex', 'app_nlgf']
    nlgf = [u for u in manifest['units'] if u['facet'] == 'app_nlgf' and u['status'] == 'ok']
    assert nlgf and nlgf[0]['covariates'] == ['sex']                                 # the host's sex


def test_inventory_lists_the_scope(tmp_path):
    store, entry = make_dataset(tmp_path, organism='mouse')
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    annotation = aft.mus.resolve_annotation(entry.h5ad_path, root=store)
    assert aft.dataset_scope(entry, annotation, aft.get_api_config(md_yaml)) is None      # pure mouse: no scope


# ── ant-facet-test-modules: replication of modules ───────────────────────────

def _module_table(cohort_effects):
    """One module-result row per (cohort, module): estimate, std_effect and two-sided permutation p."""
    rows = []
    for db_id, effects in cohort_effects.items():
        for module, (estimate, std_effect, p_perm) in effects.items():
            rows.append({'facet_scope': 'human_postmortem', 'facet': 'sex', 'method': 'cell_mean', 'source': 'geneset',
                         'db_id': db_id, 'cell_type': 'microglial cell', 'celltype_source': 'constant_all_cells',
                         'n_donors': 20, 'catalog': 'Cat', 'module': module, 'estimate': estimate,
                         'std_effect': std_effect, 'p_perm': p_perm, 'q_perm': min(1.0, p_perm * 3),
                         'q_param': min(1.0, p_perm * 3), 'descriptive_only': False})
    return pd.DataFrame(rows)


def _cohort_table(tmp_path, db_ids):
    root = tmp_path / '_facet_tests'
    root.mkdir()
    lines = ['status\tcohort\tcell_class\trole\tdb_id']
    lines += [f'DRAFT\tCohort{i}\tmicroglia\tanchor\t{db_id}' for i, db_id in enumerate(db_ids)]
    path = root / 'cohort_selection.DRAFT.tsv'
    path.write_text('\n'.join(lines) + '\n')
    return root, path


def test_replicated_modules_partial_conjunction_with_the_effect_gate(tmp_path):
    ids = ['A/a', 'B/b', 'C/c']
    root, cohort_table = _cohort_table(tmp_path, ids)
    table = _module_table({
        'A/a': {'UP': (0.5, 0.8, 0.0002), 'WEAK': (0.01, 0.05, 0.0002), 'MIXED': (0.5, 0.8, 0.0002)},
        'B/b': {'UP': (0.4, 0.7, 0.0004), 'WEAK': (0.01, 0.05, 0.0004), 'MIXED': (0.5, 0.8, 0.0004)},
        'C/c': {'UP': (0.6, 0.9, 0.001), 'WEAK': (0.02, 0.06, 0.001), 'MIXED': (-0.7, -0.9, 0.0005)},
    })
    table = pd.concat([table] + [_module_table({'A/a': {f'N{i}': (0.0, 0.0, 0.6)},
                                                'B/b': {f'N{i}': (0.0, 0.0, 0.7)},
                                                'C/c': {f'N{i}': (0.0, 0.0, 0.8)}}) for i in range(20)])
    replicated = mod.replicated_modules(table, root, cohort_table, min_std_effect=0.2)
    by_module = replicated.set_index('module')
    assert bool(by_module.loc['UP', 'replicated']) and by_module.loc['UP', 'direction'] == 'up'
    assert by_module.loc['UP', 'm'] == 3 and by_module.loc['UP', 'median_std_effect'] == pytest.approx(0.8)
    # r < 0.05 so it replicates, but the standardised effect is below the 0.2 gate in every cohort: shown, not fused
    assert by_module.loc['WEAK', 'r_value'] < 0.05 and not bool(by_module.loc['WEAK', 'effect_gate_passed'])
    assert bool(by_module.loc['WEAK', 'replicated']) and by_module.loc['WEAK', 'n_cohorts_ge_gate'] == 0
    assert by_module.loc['UP', 'n_cohorts_ge_gate'] == 3
    # two cohorts up, one strongly down: "present in at least 2 cohorts" holds and the median effect is up
    assert by_module.loc['MIXED', 'k'] == 2 and bool(by_module.loc['MIXED', 'replicated'])
    assert by_module.loc['MIXED', 'median_std_effect'] == pytest.approx(0.8)
    assert 'N0' not in by_module.index or not bool(by_module.loc['N0', 'replicated'])
    assert set(replicated['facet_scope']) == {'human_postmortem'}


def test_replicated_modules_never_mix_scopes(tmp_path):
    ids = ['A/a', 'B/b']
    root, cohort_table = _cohort_table(tmp_path, ids)
    human = _module_table({'A/a': {'M': (0.5, 0.8, 0.0002)}})
    mouse = _module_table({'B/b': {'M': (0.5, 0.8, 0.0002)}}).assign(facet_scope='in_vitro')
    table = pd.concat([human, mouse], ignore_index=True)
    replicated = mod.replicated_modules(table, root, cohort_table)
    assert replicated.empty                                   # one cohort per scope: nothing can replicate


# ── v3b: lint, gene classes, notes, strongest facet ──────────────────────────

def test_lint_control_basis_and_design():
    api = {'obs_derive': {'ant_app_nlgf': {'source': 'g', 'map': {'a': 'nlgf'}}}}
    assert [f.rule for f in facet_lint.lint_app_nlgf_design({}, api)] == ['control_basis_missing']
    assert facet_lint.lint_app_nlgf_design({}, {}) == []                       # does not publish the facet
    assert facet_lint.lint_app_nlgf_design({'control_basis': 'wt_host'}, api) == []
    assert [f.rule for f in facet_lint.lint_app_nlgf_design({'control_basis': 'wild'}, api)] == ['control_basis_invalid']
    design = {'control_basis': 'other', 'app_nlgf_design': {'timepoint_column': 'age', 'covariate_columns': ['inj']}}
    assert facet_lint.lint_app_nlgf_design(design, api) == []
    bad = {'control_basis': 'other', 'app_nlgf_design': {'nope': 'x'}}
    assert [f.rule for f in facet_lint.lint_app_nlgf_design(bad, api)] == ['control_basis_invalid']


@pytest.mark.parametrize('symbol, expected', [
    ('MT-CO1', ['mitochondrial']), ('mt-Nd1', ['mitochondrial']), ('RPL13', ['ribosomal']), ('Rps6', ['ribosomal']),
    ('AC012345.1', ['lncrna_or_clone']), ('LINC00682', ['lncrna_or_clone']), ('SNHG5-AS1', ['lncrna_or_clone']),
    ('Gm12345', ['lncrna_or_clone']), ('2610021A01Rik', ['lncrna_or_clone']), ('APOE', ['apoe_locus']),
    ('TREM2', []),
])
def test_gene_classes_by_name(symbol, expected):
    assert gene_classes.flags(symbol, annotation={}) == expected


def test_gene_classes_from_an_annotation():
    annotation = {'XIST': ('chrX', 20_000), 'DMD': ('chrX', 2_200_000), 'TREM2': ('chr6', 8_000),
                  'CNTNAP2': ('chr7', 2_300_000), 'UTY': ('chrY', 240_000)}
    assert gene_classes.flags('XIST', annotation) == ['sex_chromosome']
    assert gene_classes.flags('DMD', annotation) == ['sex_chromosome', 'long_gene']
    assert gene_classes.flags('CNTNAP2', annotation) == ['long_gene']
    assert gene_classes.flags('TREM2', annotation) == []
    assert gene_classes.flags('trem2', annotation) == []                       # symbols are matched case-insensitively


def test_gene_classes_read_a_gtf(tmp_path, monkeypatch):
    gtf = tmp_path / 'genes.gtf'
    gtf.write_text('chr1\tHAVANA\tgene\t100\t900000\t.\t+\t.\tgene_id "G1"; gene_name "LONGONE";\n'
                   'chr1\tHAVANA\ttranscript\t100\t200\t.\t+\t.\tgene_id "G1"; gene_name "LONGONE";\n'
                   'chrX\tHAVANA\tgene\t5\t500\t.\t-\t.\tgene_id "G2"; gene_name "XGENE";\n')
    monkeypatch.setenv('ANTHIVE_GENE_GTF', str(gtf))
    assert gene_classes.flags('LONGONE') == ['long_gene'] and gene_classes.flags('XGENE') == ['sex_chromosome']
    monkeypatch.setenv('ANTHIVE_GENE_GTF', str(tmp_path / 'missing.gtf'))
    assert gene_classes.flags('LONGONE') == []


def test_unit_notes_say_what_a_reader_must_keep_in_mind():
    age = facet_results.unit_notes({'facet': 'age', 'age_top_coded': 90.0, 'flags': {'small_sample': True},
                                    'inflated': True, 'lambda_gc': 1.8, 'n_sex_discordant': 2, 'n_sex_checked': 40})
    text = ' | '.join(age)
    assert 'not adjusted for pathology' in text and 'top-coded at 90' in text and 'lambda 1.80' in text
    assert 'unit-wide technical shift' in text
    assert '2 of 40 replicates express the other sex' in text and 'small sample' in text
    assert 'adjusted for pathology' in facet_results.unit_notes({'facet': 'age_adjusted'})[0]
    nlgf = facet_results.unit_notes({'facet': 'app_nlgf', 'facet_scope': 'xenograft_human_in_mouse',
                                     'control_basis': 'humanised_app_host', 'design_subset': 'timepoint=6m|line=WT',
                                     'flags': {}})
    text = ' | '.join(nlgf)
    assert 'large effects only' in text and 'live-cell isolation' in text and 'humanised-App host' in text
    assert 'stratified: timepoint=6m, line=WT' in text
    mouse = ' | '.join(facet_results.unit_notes({'facet': 'app_nlgf', 'facet_scope': 'human_postmortem', 'flags': {}}))
    assert 'live-cell isolation' not in mouse


def test_strongest_facet_names_the_card_a_page_opens_on():
    cards = {'sex': {'scope': 'human_postmortem', 'replication': [{'cell_type': 'microglia', 'replicated': True, 'r_value': 0.03}]},
             'braak': {'scope': 'human_postmortem', 'replication': [{'cell_type': 'microglia', 'replicated': True, 'r_value': 0.001},
                                                                     {'cell_type': 'astrocyte', 'replicated': False, 'r_value': 1e-9}]},
             'age': {'scope': 'human_postmortem', 'replication': []}}
    assert facet_results.strongest_facet(cards) == {'facet': 'braak', 'cell_type': 'microglia', 'r_value': 0.001,
                                                    'scope': 'human_postmortem'}
    assert facet_results.strongest_facet({'sex': {'replication': []}}) is None


# ── v3c: p-value floors, small cohorts, sensitivity facets, strong tier, permutation null ────────

def test_apoe_dose_and_braak_threshold_in_the_donor_table():
    assert aft.derive_apoe4_dose_from_genotype('E3/E4') == 1.0 and aft.derive_apoe4_dose_from_genotype('44') == 2.0
    assert aft.derive_apoe4_dose_from_genotype('e2e3') == 0.0 and aft.derive_apoe4_dose_from_genotype('unknown') is None
    cells = pd.DataFrame({'ant_donor': ['A', 'B', 'C', 'D'], 'ant_apoe': ['E3/E4', 'E4/E4', 'E3/E3', None],
                          'ant_braak': [2.0, 3.0, 6.0, None]})
    table = aft.build_donor_table(cells)[0].set_index('donor')
    assert table.loc['B', 'apoe4_dose'] == 2.0 and table.loc['C', 'apoe4_dose'] == 0.0 and pd.isna(table.loc['D', 'apoe4_dose'])
    assert [table.loc[d, 'braak_threshold'] for d in 'ABC'] == ['low', 'high', 'high'] and pd.isna(table.loc['D', 'braak_threshold'])
    plan_table = pd.DataFrame({'donor': [f'D{i}' for i in range(12)], 'braak_threshold': ['low'] * 6 + ['high'] * 6,
                               'sex': ['f', 'm'] * 6, 'apoe4_dose': [0.0, 1.0, 2.0] * 4,
                               'age': np.nan, 'pmi': np.nan, 'braak': np.nan, 'batch': None, 'host_sex': None})
    two = aft.plan_job(plan_table, 'braak_threshold', plan_table['donor'])
    assert two['status'] == 'ok' and two['level_counts'] == {'low': 6, 'high': 6}
    dose = aft.plan_job(plan_table, 'apoe4_dose', plan_table['donor'])
    assert dose['status'] == 'ok' and dose['level_counts']['n_distinct'] == 3
    assert aft.tau_for_facet('apoe4_dose') == pytest.approx(math.log2(1.1) / 2)
    assert facet_scopes.facet_allowed_scopes('apoe4_dose') == ['human_postmortem']
    assert facet_scopes.facet_allowed_scopes('braak_threshold') == ['human_postmortem']


def test_badge_says_how_many_small_cohorts_m_includes_and_the_strong_tier():
    entry = {'m': 5, 'k': 2, 'r_value': 0.01, 'r_direction': 'up', 'replicated': True, 'effect_gate_passed': True,
             'effect_gate_cohorts': ['A', 'B'], 'r_cohorts': ['A', 'B'], 'effect_gate_unit': '10%', 'cohorts_tested': 5,
             'replicated_effect': True, 'r_treat': 0.02, 'replicated_strong': True, 'r_strong': 0.04, 'n_small': 3,
             'small_cohort_replicates': 20}
    badge = facet_results.FacetResults._badge(entry)
    assert 'm includes 3 cohorts below 20 donors' in badge and 'r_strong = 0.04' in badge
    assert 'm includes' not in facet_results.FacetResults._badge({**entry, 'n_small': 0})


def test_n_small_counts_anchor_units_below_twenty_analysed_replicates():
    units = pd.DataFrame({'unit': [0, 1, 2], 'cohort': list('ABC'), 'cohort_label': list('ABC'),
                          'n_donors': [60, 12, 19], 'n_siblings': 1, 'overlap_unknown': False,
                          'counted': True, 'descriptive_only': False})
    eligible = units.assign(representative=True)
    fields = facet_results.FacetResults._replicability_fields(None, 3, eligible, 'sex')
    assert fields['n_small'] == 2 and fields['small_cohort_replicates'] == 20


def test_strong_tier_needs_a_25_percent_change():
    units = pd.DataFrame({'unit': [0, 1], 'cohort': ['A', 'B'], 'n_siblings': [1, 1]})
    rows = pd.DataFrame({'unit': [0, 1], 'gene': ['G', 'G'], 'gene_upper': ['G', 'G'], 'logFC': [0.5, 0.5],
                         'p_value': [1e-6, 1e-6], 'q_value': [1e-5, 1e-5], 'p_treat': [1e-5, 1e-5], 'q_treat': [1e-4, 1e-4],
                         'p_treat_strong': [0.4, 0.4]})
    row = facet_results.FacetResults._family(rows, units, 'sex')['frame'].loc['G']
    assert bool(row['replicated']) and bool(row['replicated_effect']) and not bool(row['replicated_strong'])
    rows['p_treat_strong'] = [1e-5, 1e-5]
    row = facet_results.FacetResults._family(rows, units, 'sex')['frame'].loc['G']
    assert bool(row['replicated_strong']) and row['r_strong'] < 0.05


@needs_r
def test_permutation_null_is_recorded_for_small_units(tmp_path, monkeypatch):
    monkeypatch.setattr(aft, 'NULL_PERMUTATIONS', 3)
    store, entry = make_dataset(tmp_path, n_f=6, n_m=6)
    manifest = aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    unit = [u for u in manifest['units'] if u['facet'] == 'sex' and u['status'] == 'ok'][0]
    assert unit['null_permutations'] == 3 and unit['null_hits_mean'] >= 0 and unit['null_lambda_median'] > 0
    assert unit['null_hits_max'] >= unit['null_hits_mean'] and manifest['parameters_v3b']['null_permutations'] == 3


# ── 2026-10-07: pure mouse datasets leave the facet layer; genotype / host_genotype split ────────

def test_a_pure_mouse_dataset_may_publish_only_structural_facets():
    api = {'obs_rename': {'mouse': 'ant_donor', 'Sex': 'ant_sex', 'g': 'ant_genotype'},
           'obs_derive': {'ant_app_nlgf': {'source': 'g', 'map': {'a': 'nlgf'}}},
           'obs_constant': {'ant_cell_type': {'value': 'microglial cell'}}}
    findings = facet_lint.lint_facet_scope(api, None, mouse_only=True)
    assert {f.rule for f in findings} == {'facet_scope_mismatch'} and len(findings) == 3
    assert all('mouse-only' in f.message for f in findings) and all(f.severity == 'error' for f in findings)
    assert {c for f in findings for c in ('ant_sex', 'ant_genotype', 'ant_app_nlgf') if c in f.message} == {
        'ant_sex', 'ant_genotype', 'ant_app_nlgf'}
    assert facet_lint.lint_facet_scope({'obs_rename': {'m': 'ant_donor'}}, None, mouse_only=True) == []


def test_genotype_is_the_cells_own_genome_and_host_genotype_is_xenograft_only():
    definition = obscol_tags.facet_definition('genotype')
    assert 'SEQUENCED cells' in obscol_tags.load_vocab()['tags']['genotype']['description']
    assert definition['facet_scopes'] == ['human_postmortem', 'xenograft_human_in_mouse', 'in_vitro']
    host = obscol_tags.facet_registry()['host_genotype']
    assert host['status'] == 'registered' and host['facet_scopes'] == ['xenograft_human_in_mouse']
    vocabulary = obscol_tags.load_vocab()['allele_vocabulary']
    assert 'App NL-G-F' in vocabulary['App'] and 'APOE e4/e4' in vocabulary['APOE'] and 'TREM2 R47H' in vocabulary['TREM2']
    assert {'PSEN1', 'PSEN2', 'PICALM'} <= set(vocabulary)
    assert facet_scopes.facet_in_scope('host_genotype', 'xenograft_human_in_mouse')
    assert not facet_scopes.facet_in_scope('host_genotype', 'human_postmortem')
