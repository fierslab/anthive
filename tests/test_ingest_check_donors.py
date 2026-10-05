"""ant ingest-check: donor uid form and resolution, donor overlap by id and by demographics, redaction."""
import copy
import json

import pandas as pd
import pytest

from ingest_check import FAIL, PASS, SKIP, WARN, HELD_DONOR_OVERLAP, run_checks, tool_version
from ingest_check import checks_donors, thresholds
from ingest_check.report import build_report, curator_view, scrub
from tests.ingest_helpers import Submission, donor_obs, good_api, seed_index

ALIASES = [f'ROS{number:04d}' for number in range(8)]
CANONICAL = [f'rosmap:SECRET{number:04d}' for number in range(8)]


def donor_labels(obs):
    """Rename D00.. to ROS0000.. so the aliases in the crosswalk match."""
    obs = obs.copy()
    obs['donor'] = obs['donor'].map(lambda label: f'ROS{int(label[1:]):04d}')
    return obs


def write_crosswalk(store, n_known=8, cohort='rosmap', extra_header=''):
    rows = ['cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets' + extra_header]
    for alias, canonical in list(zip(ALIASES, CANONICAL))[:n_known]:
        rows.append(f'{cohort}\tdonor_id\t{alias}\t{canonical}\tsynthetic\t1')
    (store / 'donor_crosswalk.tsv').write_text('\n'.join(rows) + '\n')


def join_api(cohort='rosmap', fallback=True):
    api = copy.deepcopy(good_api())
    spec = {'source': 'donor', 'join': 'donor_crosswalk.tsv', 'cohort': cohort}
    if fallback:
        spec['fallback_template'] = '{group_id}:{value}'
    api['obs_derive']['ant_donor_uid'] = spec
    return api


@pytest.fixture
def submission(tmp_path):
    sub = Submission(tmp_path)
    sub.obs = donor_labels(sub.obs)
    return sub


def run(sub, check_function, api=None, md=None, mus=None, duckdb=False, **context_options):
    front = dict(md or {})
    if api is not None:
        front['api'] = api
    options = {'md': front}
    if mus is not None:
        options['mus'] = mus
    sub.build(**options)
    with sub.context(duckdb_path=sub.duckdb() if duckdb else None, **context_options) as ctx:
        return check_function(ctx)


# ── uid form ─────────────────────────────────────────────────────────────────

def test_uid_form_template_passes(submission):
    outcome = run(submission, checks_donors.donor_uid_form)
    assert outcome.status == PASS and outcome.details['form'] == 'template'


def test_uid_form_missing_fails_for_human_donor_data_and_is_skipped_for_animals(submission):
    api = good_api()
    api['obs_derive'] = {}
    assert run(submission, checks_donors.donor_uid_form, api=api).status == FAIL
    outcome = run(Submission(submission.root / 'm'), checks_donors.donor_uid_form, api=api,
                  md={'replicate_unit': 'animal'}, mus='organism=mouse\naccess=internal\n')
    assert outcome.status == SKIP


def test_uid_form_malformed_and_unknown_source_fail(submission):
    api = good_api()
    api['obs_derive']['ant_donor_uid'] = {'source': 'donor', 'template': '{nonsense}'}
    assert run(submission, checks_donors.donor_uid_form, api=api).status == FAIL
    api['obs_derive']['ant_donor_uid'] = {'source': 'missing_column', 'template': '{value}'}
    outcome = run(Submission(submission.root / 's'), checks_donors.donor_uid_form, api=api)
    assert outcome.status == FAIL and 'missing_column' in outcome.summary


def test_uid_form_legacy_map_form_is_refused(submission):
    api = good_api()
    api['obs_derive']['ant_donor_uid'] = {'source': 'donor', 'dtype': 'categorical', 'map': {'ROS0000': 'x:1'}}
    outcome = run(submission, checks_donors.donor_uid_form, api=api)
    assert outcome.status == FAIL and 'template or join' in outcome.summary


# ── uid resolution (crosswalk) ───────────────────────────────────────────────

def test_uid_resolves_all_donors(submission):
    write_crosswalk(submission.store)
    submission.store.mkdir(exist_ok=True)
    outcome = run(submission, checks_donors.donor_uid_resolves, api=join_api())
    assert outcome.status == PASS
    assert outcome.details == {'n_donor_values': 8, 'n_resolved': 8, 'n_fallback': 0, 'n_unmatched': 0}


def test_uid_resolves_warns_when_many_fall_back(submission):
    write_crosswalk(submission.store, n_known=4)
    outcome = run(submission, checks_donors.donor_uid_resolves, api=join_api())
    assert outcome.status == WARN and outcome.details['n_fallback'] == 4


def test_unmatched_without_fallback_counts_as_unresolved(submission):
    write_crosswalk(submission.store, n_known=4)
    outcome = run(submission, checks_donors.donor_uid_resolves, api=join_api(fallback=False))
    assert outcome.status == WARN and outcome.details['n_unmatched'] == 4


def test_unknown_cohort_fails_and_only_the_admin_sees_the_cohort_names(submission):
    write_crosswalk(submission.store)
    outcome = run(submission, checks_donors.donor_uid_resolves, api=join_api(cohort='nowhere'))
    assert outcome.status == FAIL
    assert 'rosmap' not in json.dumps(outcome.details) + outcome.summary + outcome.fix
    assert outcome.admin_detail['cohorts_available'] == ['rosmap']


def test_missing_crosswalk_fails_without_naming_paths(submission):
    outcome = run(submission, checks_donors.donor_uid_resolves, api=join_api())
    assert outcome.status == FAIL and str(submission.store) not in json.dumps(outcome.details) + outcome.summary


def test_uid_resolves_skipped_without_join_form(submission):
    assert run(submission, checks_donors.donor_uid_resolves).status == SKIP


# ── overlap by id ────────────────────────────────────────────────────────────

SHARED_API = {'obs_rename': good_api()['obs_rename'], 'obs_value_rename': good_api()['obs_value_rename'],
              'obs_derive': {'ant_donor_uid': {'source': 'donor', 'template': 'shared:{value}'}}}


def stored_dataset(sub, db_key='OLD/AAA', **changes):
    obs = sub.obs.copy()
    for column, values in changes.items():
        obs[column] = values
    seed_index(sub.index_dir, db_key, obs, SHARED_API)


def test_overlap_ids_skipped_without_index(submission):
    assert run(submission, checks_donors.donor_overlap_ids, api=SHARED_API, duckdb=True).status == SKIP


def test_overlap_ids_fail_without_designation(submission):
    stored_dataset(submission)
    outcome = run(submission, checks_donors.donor_overlap_ids, api=SHARED_API, duckdb=True)
    assert outcome.status == FAIL
    assert outcome.details['n_shared_persons'] == 8 and outcome.details['n_datasets'] == 1
    assert outcome.admin_detail == {'shared_per_dataset': {'OLD/AAA': 8}}
    assert 'OLD' not in json.dumps(outcome.details) + outcome.summary


@pytest.mark.parametrize('md', [{'is_secondary': True, 'reanalysis_of': ['OLD/AAA']}, {'donor_group': 'cohort-a'},
                                {'facet_donor_exclusions': {'reason': 'x', 'cohorts': ['shared']}}])
def test_overlap_ids_downgrade_with_a_designation(submission, md):
    stored_dataset(submission)
    outcome = run(submission, checks_donors.donor_overlap_ids, api=SHARED_API, md=md, duckdb=True)
    assert outcome.status == PASS and outcome.info and outcome.details['designation']


def test_overlap_ids_ignores_other_datasets_of_the_same_group_and_disjoint_donors(submission):
    stored_dataset(submission, db_key='ZZ1/OTH')
    assert run(submission, checks_donors.donor_overlap_ids, api=SHARED_API, duckdb=True).status == PASS
    stored_dataset(submission, db_key='OLD/BBB', donor=submission.obs['donor'].map(lambda label: label + 'x'))
    assert run(submission, checks_donors.donor_overlap_ids, api=SHARED_API, duckdb=True).status == PASS


def test_overlap_ids_pass_when_donors_only_have_group_scoped_keys(submission):
    stored_dataset(submission)
    api = good_api()
    api['obs_derive'] = {}
    outcome = run(submission, checks_donors.donor_overlap_ids, api=api, duckdb=True)
    assert outcome.status == PASS and 'no cross-dataset donor ids' in outcome.summary


def test_designation_names():
    assert checks_donors.designation({'is_secondary': False}) == []
    assert checks_donors.designation({'is_secondary': True, 'donor_group': 'a', 'reanalysis_of': 'X/Y'}) == [
        'is_secondary', 'reanalysis_of', 'donor_group']


# ── demographic matching ─────────────────────────────────────────────────────

def donors_frame(rows):
    columns = ['sex', 'age', 'age_censored', 'apoe', 'braak', 'pmi']
    frame = pd.DataFrame(rows, columns=columns)
    frame.insert(0, 'key_digest', [f'k{index}' for index in range(len(frame))])
    return frame


def matrix(new_rows, old_rows, shift=0.0):
    candidates, distance = checks_donors.candidate_matrix(donors_frame(new_rows), donors_frame(old_rows), shift)
    return candidates


def test_candidate_requires_sex_age_and_two_agreeing_attributes():
    base = ('f', 70.0, False, '3/4', 3.0, 5.0)
    assert matrix([base], [base])[0, 0]
    assert not matrix([base], [('m',) + base[1:]])[0, 0]                          # sex differs
    assert not matrix([base], [('f', 72.5) + base[2:]])[0, 0]                      # age beyond the tolerance
    assert matrix([base], [('f', 70.9) + base[2:]])[0, 0]                          # age within a year
    assert not matrix([base], [base[:4] + (4.0,) + base[5:]])[0, 0]                # Braak contradicts
    assert not matrix([base], [base[:3] + ('4/4',) + base[4:]])[0, 0]              # APOE contradicts
    assert not matrix([base], [base[:5] + (7.0,)])[0, 0]                           # PMI more than an hour apart
    assert not matrix([base], [('f', 70.0, False, None, 3.0, None)])[0, 0]         # only one attribute agrees


def test_censored_ages_agree_and_missing_values_never_contradict():
    new = ('f', 90.0, True, '3/3', 4.0, None)
    old = ('f', 93.0, False, '3/3', 4.0, None)
    assert matrix([new], [old])[0, 0]
    assert not matrix([('f', None, False, '3/3', 4.0, None)], [old])[0, 0]


def test_age_shift_moves_the_new_donor_away_from_the_true_partner():
    base = ('f', 70.0, False, '3/4', 3.0, 5.0)
    assert not matrix([base], [base], shift=6.0)[0, 0]


def test_unique_matches_break_ties_by_closeness():
    rows = [('f', 70.0, False, '3/3', 3.0, 5.0), ('f', 70.4, False, '3/3', 3.0, 5.5)]
    new = [('f', 70.0, False, '3/3', 3.0, 5.0)]
    candidates, distance = checks_donors.candidate_matrix(donors_frame(new), donors_frame(rows))
    assert candidates.sum() == 2
    assert checks_donors.unique_matches(candidates, distance).tolist() == [True]       # one is exactly equal
    tied = [('f', 70.4, False, '3/3', 3.0, 5.4), ('f', 70.4, False, '3/3', 3.0, 5.4)]
    candidates, distance = checks_donors.candidate_matrix(donors_frame(new), donors_frame(tied))
    assert checks_donors.unique_matches(candidates, distance).tolist() == [False]       # both far, no clear match


def test_match_cohort_reports_chance_level_and_excess():
    new = donors_frame([('f' if i % 2 else 'm', 60.0 + 3 * i, False, None, float(i % 7), 4.0 + i) for i in range(12)])
    old = new.copy()
    old['key_digest'] = [f'other{i}' for i in range(len(old))]
    result = checks_donors.match_cohort(new, old)
    assert result['n_unique'] == 12 and result['fraction'] == 1.0
    assert result['corrected'] == 1.0 - result['background']
    assert checks_donors.match_cohort(new.iloc[0:0], old)['fraction'] == 0.0


def with_other_ids(sub):
    return sub.obs['donor'].map(lambda label: label + 'x')


def test_demographic_overlap_without_shared_ids_is_held(submission):
    stored_dataset(submission, donor=with_other_ids(submission))
    outcome = run(submission, checks_donors.donor_overlap_demographics, api=SHARED_API, duckdb=True)
    assert outcome.status == WARN and outcome.held == [HELD_DONOR_OVERLAP]
    assert outcome.details['best_fraction'] == 1.0 and outcome.details['n_cohorts_compared'] == 1
    assert 'OLD' not in json.dumps(outcome.details) + outcome.summary
    assert outcome.admin_detail['best_cohort'] == 'OLD'


def test_demographic_overlap_is_a_note_with_a_designation(submission):
    stored_dataset(submission, donor=with_other_ids(submission))
    outcome = run(submission, checks_donors.donor_overlap_demographics, api=SHARED_API,
                  md={'facet_donor_exclusions': {'reason': 'x', 'cohorts': ['old']}}, duckdb=True)
    assert outcome.status == PASS and outcome.info and not outcome.held


def test_unrelated_cohort_passes(submission):
    unrelated = submission.obs.copy()
    unrelated['age'] = unrelated['age'] + 30
    unrelated['donor'] = with_other_ids(submission)
    seed_index(submission.index_dir, 'OLD/AAA', unrelated, SHARED_API)
    outcome = run(submission, checks_donors.donor_overlap_demographics, api=SHARED_API, duckdb=True)
    assert outcome.status == PASS and not outcome.held


def test_demographics_skipped_without_index_or_with_few_donors(submission, tmp_path):
    assert run(submission, checks_donors.donor_overlap_demographics, api=SHARED_API, duckdb=True).status == SKIP
    small = Submission(tmp_path / 'small')
    small.obs = donor_obs(12, n_donors=3)
    stored_dataset(small)
    assert run(small, checks_donors.donor_overlap_demographics, api=SHARED_API, duckdb=True).status == SKIP


def test_demographic_thresholds_are_the_documented_ones():
    assert thresholds.DEMOGRAPHIC_AGE_TOLERANCE_YEARS == 1.0 and thresholds.DEMOGRAPHIC_PMI_TOLERANCE_HOURS == 1.0
    assert thresholds.DEMOGRAPHIC_HELD_FRACTION == 0.5


# ── redaction of controlled-access data ──────────────────────────────────────

def full_report(sub, audience):
    with sub.context(duckdb_path=sub.duckdb()) as ctx:
        results = run_checks(ctx)
        report = build_report(ctx, results, 'admin', tool_version())
        scrub(report, ctx.sensitive_tokens())
        return curator_view(report) if audience == 'curator' else report


@pytest.mark.parametrize('audience', ['curator', 'admin'])
def test_reports_never_contain_crosswalk_content_or_donor_labels(submission, audience):
    write_crosswalk(submission.store)
    stored_dataset(submission, donor=with_other_ids(submission))
    submission.build(md={'api': join_api()})
    text = json.dumps(full_report(submission, audience))
    for secret in ALIASES + CANONICAL + ['SECRET0']:
        assert secret not in text, secret
    for label in ('ROS0001', 'ROS0007'):
        assert label not in text


def test_curator_report_has_no_admin_detail_and_no_dataset_ids(submission):
    write_crosswalk(submission.store)
    stored_dataset(submission, donor=with_other_ids(submission))
    submission.build(md={'api': join_api()})
    admin = full_report(submission, 'admin')
    curator = full_report(submission, 'curator')
    assert 'admin_detail' in admin and 'admin_detail' not in curator
    assert 'OLD' in json.dumps(admin['admin_detail']) and 'cohorts_available' in json.dumps(admin['admin_detail'])
    assert 'OLD' not in json.dumps(curator) and 'rosmap' not in json.dumps(curator)


def test_scrub_replaces_leaked_tokens_in_strings_keys_and_nested_values(submission):
    write_crosswalk(submission.store)
    sub_ctx = submission.build().context()
    leaked = {'a': f'donor {ALIASES[0]} found', 'b': [CANONICAL[1], {'rosmap:SECRET0002': 'x'}], 'n': 12345,
              'safe': 'GENE1 and 1234'}
    replaced = scrub(leaked, sub_ctx.sensitive_tokens())
    assert replaced == 3
    text = json.dumps(leaked)
    assert ALIASES[0] not in text and 'SECRET' not in text and '[redacted]' in text
    assert leaked['n'] == 12345 and leaked['safe'] == 'GENE1 and 1234' and leaked['redactions'] == 3


def test_controlled_cohorts_come_from_the_optional_column_and_expose_names_only(submission):
    write_crosswalk(submission.store, extra_header='\tcontrolled')
    text = (submission.store / 'donor_crosswalk.tsv').read_text().splitlines()
    (submission.store / 'donor_crosswalk.tsv').write_text('\n'.join([text[0]] + [line + '\ttrue' for line in text[1:]]) + '\n')
    with submission.build().context() as ctx:
        assert ctx.controlled_cohorts() == {'rosmap'}
