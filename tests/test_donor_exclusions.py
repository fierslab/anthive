"""Tests for ``lib/donor_exclusions.py`` (the facet_donor_exclusions rule).

Run with:
    uv run --with pytest --with pandas --no-project env PYTHONPATH=lib \
        python -m pytest tests/test_donor_exclusions.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import donor_exclusions as dex  # noqa: E402


def make_cells() -> pd.DataFrame:
    # D1, D2: bank R (D1 carries a rosmap uid); D3, D4: bank M; D5 has no bank value
    return pd.DataFrame({
        'ant_donor': ['D1', 'D1', 'D2', 'D3', 'D3', 'D4', 'D5', None],
        'ant_donor_uid': ['rosmap:00000001', 'rosmap:00000001', 'L:D2', 'L:D3', 'L:D3', 'seaad:X', 'L:D5', None],
        'Source': ['R', 'R', 'R', 'M', 'M', 'M', None, 'R'],
    })


def test_rule_from_md_absent_and_valid():
    assert dex.rule_from_md({}) is None
    rule = dex.rule_from_md({dex.MD_KEY: {'reason': 'a  b\n c', 'obs': {'Source': 'R'}, 'cohorts': ['rosmap']}})
    assert rule.reason == 'a b c' and rule.obs == {'Source': ('R',)} and rule.cohorts == ('rosmap',)


@pytest.mark.parametrize('bad', ['text', {'obs': 'x'}, {'obs': {'Source': []}}, {'reason': 'only'},
                                 {'obs': {'Source': ['R']}, 'colour': 1}, {'cohorts': 5}])
def test_rule_from_md_rejects_malformed(bad):
    with pytest.raises(ValueError):
        dex.rule_from_md({dex.MD_KEY: bad})


def test_parse_where_and_combine():
    assert dex.parse_where(['Source=R|RS', 'Source=X', 'b=1']) == {'Source': ('R', 'RS', 'X'), 'b': ('1',)}
    with pytest.raises(ValueError):
        dex.parse_where(['nonsense'])
    md_rule = dex.rule_from_md({dex.MD_KEY: {'obs': {'Source': ['R']}}})
    assert dex.combine(md_rule) is md_rule
    both = dex.combine(md_rule, ['rosmap,seaad'], ['Source=RS'])
    assert both.obs['Source'] == ('R', 'RS') and both.cohorts == ('rosmap', 'seaad')
    assert dex.combine(None, ['rosmap']).origin == 'cli'


def test_excluded_donors_by_obs_cohort_and_union():
    cells = make_cells()
    by_obs, counts = dex.excluded_donors(cells, dex.ExclusionRule(obs={'Source': ('R',)}))
    # D1, D2 plus the unkeyed NaN-donor cell is ignored
    assert by_obs == {'D1', 'D2'} and counts == {'Source=R': 2}
    by_cohort, counts = dex.excluded_donors(cells, dex.ExclusionRule(cohorts=('rosmap',)))
    assert by_cohort == {'D1'} and counts == {'cohort:rosmap': 1}
    both, _ = dex.excluded_donors(cells, dex.ExclusionRule(obs={'Source': ('M',)}, cohorts=('rosmap',)))
    assert both == {'D1', 'D3', 'D4'}


def test_missing_columns_fail_loudly():
    with pytest.raises(ValueError, match='not an obs column'):
        dex.excluded_donors(make_cells(), dex.ExclusionRule(obs={'nope': ('x',)}))
    with pytest.raises(ValueError, match='ant_donor_uid'):
        dex.excluded_donors(make_cells().drop(columns='ant_donor_uid'), dex.ExclusionRule(cohorts=('rosmap',)))


def test_apply_rule_counted_and_all_donors_variants():
    cells = make_cells()
    rule = dex.ExclusionRule(reason='r', obs={'Source': ('R',)}, cohorts=('rosmap',))
    kept, report = dex.apply_rule(cells, rule)
    assert sorted(kept['ant_donor'].dropna().unique()) == ['D3', 'D4', 'D5']
    assert list(kept.index) == list(range(len(kept)))                 # positional joins stay valid
    assert report['counted'] is True and report['variant'] == 'counted'
    assert (report['n_donors_before'], report['n_donors_excluded'], report['n_donors_after']) == (5, 2, 3)
    assert report['n_cells_before'] == 8 and report['n_cells_after'] == 5   # incl. the donor-less cell
    assert report['n_excluded_by_criterion'] == {'Source=R': 2, 'cohort:rosmap': 1}
    full, report_all = dex.apply_rule(cells, rule, dex.VARIANT_ALL_DONORS)
    assert len(full) == len(cells) and report_all['counted'] is False
    assert (report_all['n_donors_excluded'], report_all['n_donors_after']) == (2, 5)


def test_apply_rule_without_rule_returns_input():
    cells = make_cells()
    out, report = dex.apply_rule(cells, None)
    assert out is cells and report is None


def test_report_has_no_identifiers():
    _, report = dex.apply_rule(make_cells(), dex.ExclusionRule(obs={'Source': ('R',)}))
    text = str(report)
    for identifier in ('D1', 'D2', 'rosmap:00000001'):
        assert identifier not in text


def test_result_dir(tmp_path):
    assert dex.result_dir(tmp_path, 'G', True, False) == tmp_path / 'G'
    assert dex.result_dir(tmp_path, 'G', True, True) == tmp_path / 'G' / dex.SUPPLEMENTARY_DIRNAME
    assert dex.result_dir(tmp_path, 'G', False, True) == tmp_path / 'G'


# ── age floor and declared batch column ───────────────────────────────────────

def test_min_age_floors_parse_and_validate():
    assert dex.min_age_floors({}) == {}
    assert dex.min_age_floors({dex.MIN_AGE_KEY: {'ant_disease': 60, 'braak': 60.5}}) == {'disease': 60.0, 'braak': 60.5}
    for bad in ('x', {}, {'disease': 'old'}, {'disease': -1}, {'disease': True}, [60]):
        with pytest.raises(ValueError):
            dex.min_age_floors({dex.MIN_AGE_KEY: bad})


def test_below_age_floor_is_per_facet_and_keeps_unknown_ages():
    donors = pd.DataFrame({'donor': ['a', 'b', 'c', 'd'], 'age': [30.0, 59.9, 60.0, float('nan')]})
    below, report = dex.below_age_floor(donors, {'disease': 60.0, 'braak': 40.0})
    assert below == {'disease': {'a', 'b'}, 'braak': {'a'}}
    assert report['disease'] == {'floor_years': 60.0, 'n_donors_below_floor': 2, 'n_age_unknown_kept': 1,
                                 'n_donors': 4}
    assert dex.below_age_floor(donors, {}) == ({}, None)
    stratum = pd.Series(['a', 'b', 'c', 'd'])
    assert list(dex.stratum_donors(stratum, 'disease', below)) == ['c', 'd']
    assert list(dex.stratum_donors(stratum, 'sex', below)) == ['a', 'b', 'c', 'd']       # other facets keep everyone


def test_declared_batch_column():
    cells = make_cells()
    assert dex.declared_batch_column({}, cells) is None
    assert dex.declared_batch_column({dex.BATCH_KEY: 'Source'}, cells) == 'Source'
    assert dex.declared_columns({dex.BATCH_KEY: 'Source'}) == ['Source'] and dex.declared_columns({}) == []
    with pytest.raises(ValueError, match='not an obs column'):
        dex.declared_batch_column({dex.BATCH_KEY: 'nope'}, cells)
