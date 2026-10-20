"""ant ingest-check: facet mapping checks (namespace, static, data, unmapped values, Braak, core facets)."""
import copy

import pytest

from ingest_check import FAIL, PASS, SKIP, WARN
from ingest_check import checks_facets
from tests.ingest_helpers import Submission, good_api


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path)


def outcome_of(submission, check_function, api=None, obs=None, with_duckdb=True, md=None):
    if obs is not None:
        submission.obs = obs
    front = dict(md or {})
    if api is not None:
        front['api'] = api
    submission.build(md=front)
    duckdb_path = submission.duckdb() if with_duckdb else None
    with submission.context(duckdb_path=duckdb_path) as ctx:
        return check_function(ctx)


def api_with(**changes):
    api = copy.deepcopy(good_api())
    for section, content in changes.items():
        api.setdefault(section, {}).update(content)
    return api


def test_clean_mapping_passes_every_facet_check(submission):
    for check_function in (checks_facets.facets_namespace, checks_facets.facets_mapping_static,
                           checks_facets.facets_mapping_data, checks_facets.facets_unmapped_values,
                           checks_facets.facets_braak_range, checks_facets.facets_core_present):
        assert outcome_of(Submission(submission.root / check_function.__name__), check_function).status == PASS, \
            check_function.__name__


def test_unregistered_facet_is_an_error_in_strict_namespace(submission):
    outcome = outcome_of(submission, checks_facets.facets_namespace,
                         api=api_with(obs_rename={'cell_type_raw': 'ant_not_a_facet'}))
    assert outcome.status == FAIL and 'ant_not_a_facet' in outcome.summary


def test_deprecated_facet_is_a_warning(submission):
    outcome = outcome_of(submission, checks_facets.facets_namespace, api=api_with(obs_rename={'cell_type_raw': 'ant_region'}))
    assert outcome.status == WARN and 'deprecated' in outcome.summary


def test_rename_chain_is_a_static_error(submission):
    outcome = outcome_of(submission, checks_facets.facets_mapping_static,
                         api=api_with(obs_rename={'age': 'ant_age', 'ant_age': 'ant_pmi'}))
    assert outcome.status == FAIL and 'rename_chain' in outcome.summary


def test_braak_value_in_map_outside_range_is_a_static_error(submission):
    api = api_with(obs_derive={'ant_braak': {'source': 'braak', 'dtype': 'numerical', 'map': {'1': 9}}})
    api['obs_rename'].pop('braak')
    outcome = outcome_of(submission, checks_facets.facets_mapping_static, api=api)
    assert outcome.status == FAIL and 'numeric_range' in outcome.summary


def test_missing_source_column_fails_mapping_data(submission):
    outcome = outcome_of(submission, checks_facets.facets_mapping_data,
                         api=api_with(obs_derive={'ant_tissue': {'source': 'nope', 'dtype': 'categorical', 'map': {'a': 'b'}}}))
    assert outcome.status == FAIL and 'source_missing' in outcome.summary


def test_mapping_data_checks_replicate_column_exists(submission):
    outcome = outcome_of(submission, checks_facets.facets_mapping_data, md={'replicate_unit': 'sample', 'replicate_column': 'nope'})
    assert outcome.status == FAIL and 'replicate_column_missing' in outcome.summary


def test_mapping_data_skips_without_a_database(submission):
    outcome = outcome_of(submission, checks_facets.facets_mapping_data, with_duckdb=False)
    assert outcome.status == SKIP


def test_unmapped_values_in_a_derive_without_default_fail(submission):
    api = api_with(obs_derive={'ant_cell_type': {'source': 'cell_type_raw', 'dtype': 'categorical',
                                                 'map': {'microglia': 'microglial cell'}}})
    outcome = outcome_of(submission, checks_facets.facets_unmapped_values, api=api)
    assert outcome.status == FAIL
    assert outcome.details['facets']['ant_cell_type']['values'] == ['astro']


def test_explicit_null_and_default_count_as_mapped(submission):
    api = api_with(obs_derive={'ant_cell_type': {'source': 'cell_type_raw', 'dtype': 'categorical',
                                                 'map': {'microglia': 'microglial cell', 'astro': None}}})
    assert outcome_of(submission, checks_facets.facets_unmapped_values, api=api).status == PASS
    api = api_with(obs_derive={'ant_cell_type': {'source': 'cell_type_raw', 'dtype': 'categorical',
                                                 'map': {'microglia': 'microglial cell'}, 'default': 'other'}})
    assert outcome_of(Submission(submission.root / 'again'), checks_facets.facets_unmapped_values, api=api).status == PASS


def test_value_outside_controlled_vocabulary_after_rename_fails(submission):
    api = api_with()
    api['obs_value_rename'] = {'sex_raw': {'F': 'female'}}
    outcome = outcome_of(submission, checks_facets.facets_unmapped_values, api=api)
    assert outcome.status == FAIL
    assert outcome.details['facets']['ant_sex']['allowed'] == ['m', 'f']


def test_hidden_values_are_not_reported(submission):
    api = api_with()
    api['obs_value_hidden'] = {'sex_raw': ['F']}
    api['obs_value_rename'] = {'sex_raw': {'M': 'm'}}
    assert outcome_of(submission, checks_facets.facets_unmapped_values, api=api).status == PASS


def test_donor_values_are_never_listed(submission):
    api = api_with(obs_derive={'ant_donor_uid': {'source': 'donor', 'dtype': 'categorical', 'map': {'D00': 'x:1'}}})
    outcome = outcome_of(submission, checks_facets.facets_mapping_data, api=api)
    text = str(outcome.details) + outcome.summary
    assert 'D01' not in text and 'D02' not in text


@pytest.mark.parametrize('braak_value,status', [(3.0, PASS), (7.0, FAIL), (2.5, FAIL)])
def test_braak_range_values(submission, braak_value, status):
    obs = Submission(submission.root / 'obs').obs.copy()
    obs['braak'] = obs['braak'].astype(float)
    obs.loc[obs.index[0], 'braak'] = braak_value
    assert outcome_of(submission, checks_facets.facets_braak_range, obs=obs).status == status


def test_braak_roman_labels_are_accepted(submission):
    obs = Submission(submission.root / 'obs').obs.copy()
    obs['braak'] = obs['braak'].map(lambda number: ['0', 'I', 'II', 'III', 'IV', 'V', 'VI'][int(number)])
    api = api_with()
    api['obs_rename'].pop('braak')
    api['obs_derive']['ant_braak'] = {'source': 'braak', 'dtype': 'categorical',
                                      'map': {label: label for label in ['0', 'I', 'II', 'III', 'IV', 'V', 'VI']}}
    outcome = outcome_of(submission, checks_facets.facets_braak_range, api=api, obs=obs)
    assert outcome.status == PASS


def test_braak_not_published_passes(submission):
    api = api_with()
    api['obs_rename'].pop('braak')
    assert outcome_of(submission, checks_facets.facets_braak_range, api=api).status == PASS


def test_core_facets_missing_is_a_warning_and_a_failure_for_donor_units(submission):
    api = {'obs_rename': {'age': 'ant_age'}}
    outcome = outcome_of(submission, checks_facets.facets_core_present, api=api, md={'replicate_unit': 'none', 'replicate_column': None})
    assert outcome.status == WARN and 'ant_sex' in outcome.summary
    outcome = outcome_of(Submission(submission.root / 'unit'), checks_facets.facets_core_present, api=api)
    assert outcome.status == FAIL and 'ant_donor' in outcome.summary


def test_core_facets_require_braak_for_alzheimer_data(submission):
    api = api_with()
    api['obs_rename'].pop('braak')
    outcome = outcome_of(submission, checks_facets.facets_core_present, api=api)
    assert outcome.status == WARN and 'ant_braak' in outcome.summary
    outcome = outcome_of(Submission(submission.root / 'b'), checks_facets.facets_core_present, api=api,
                         md={'braak_unavailable': 'not recorded'})
    assert outcome.status == PASS


def test_core_facets_skipped_for_other_organisms(tmp_path):
    submission = Submission(tmp_path)
    submission.build(mus='organism=rat\naccess=internal\n')
    with submission.context() as ctx:
        assert checks_facets.facets_core_present(ctx).status == SKIP


def test_public_message_withholds_crosswalk_and_donor_content():
    from facet_lint import Finding
    join = Finding('error', 'join_cohort_unknown', "cohort 'x' is not in donor_crosswalk.tsv (cohorts: a, b)")
    donor = Finding('warning', 'unmapped_value', "obs_derive[ant_donor_uid] maps values of donor: 'D1' (3 cells)")
    plain = Finding('warning', 'unmapped_value', "obs_derive[ant_sex] maps values of sex: 'x' (3 cells)")
    assert 'a, b' not in checks_facets.public_message(join)
    assert 'D1' not in checks_facets.public_message(donor)
    assert "'x'" in checks_facets.public_message(plain)


def test_missing_rename_source_fails_and_names_the_column(submission):
    # Francken2026, 2026-10-20: obs_rename sources that were not stored went live as a warning
    api = api_with(obs_rename={'sex_harmonised': 'ant_sex'})
    api['obs_rename'].pop('sex_raw')
    api.get('obs_value_rename', {}).pop('sex_raw', None)
    outcome = outcome_of(submission, checks_facets.facets_mapping_data, api=api)
    assert outcome.status == FAIL and 'rename_source_missing' in outcome.summary
    assert 'sex_harmonised' in outcome.summary and 'ant_sex' in outcome.summary


def test_missing_donor_source_names_the_column_not_withheld(submission):
    api = api_with()
    api['obs_rename'] = {key: value for key, value in api['obs_rename'].items() if key != 'donor'}
    api['obs_rename']['donor_gone'] = 'ant_donor'
    api['obs_derive']['ant_donor_uid'] = {'source': 'donor_gone', 'template': '{group_id}:{value}'}
    outcome = outcome_of(submission, checks_facets.facets_mapping_data, api=api)
    assert outcome.status == FAIL and 'donor_gone' in outcome.summary
    assert 'withheld' not in outcome.summary
    assert 'D00' not in str(outcome.details)                  # donor values never appear
