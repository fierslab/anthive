"""The age facet rule: human donors only, years only (facet_lint age rules, registry, help text)."""
from pathlib import Path

import pytest

import facet_lint
import facet_scopes
import obscol_tags

RENAME_API = {'obs_rename': {'Age': 'ant_age'}}


def _lookup(values):
    return lambda column: values.get(column)


def _rules(findings):
    return sorted(f.rule for f in findings)


def _data(values, kind='numerical', api=None, metadata=None):
    return facet_lint.lint_api_data(api or RENAME_API, {'Age': kind}, _lookup({'Age': values}), metadata=metadata)


def test_numeric_years_pass():
    assert _data([(63.0, 10), (81.0, 5), (88.0, 2)]) == []


def test_categorical_age_is_error():
    findings = _data([(63.0, 10)], kind='categorical')
    assert 'age_not_years' in _rules(findings)
    assert all(f.severity == facet_lint.ERROR for f in findings if f.rule == 'age_not_years')


def test_value_above_120_is_error():
    findings = _data([(70.0, 3), (150.0, 1)])
    assert _rules(findings) == ['age_not_years']
    assert '150' in findings[0].message


def test_negative_age_is_error():
    assert _rules(_data([(-1.0, 1)])) == ['age_not_years']


def test_months_like_values_in_mouse_dataset_error_via_scope():
    findings = facet_lint.lint_facet_scope(RENAME_API, None, mouse_only=True)
    assert _rules(findings) == ['facet_scope_mismatch'] and 'ant_age' in findings[0].message


def test_xenograft_host_age_error_via_scope():
    findings = facet_lint.lint_facet_scope(RENAME_API, facet_scopes.SCOPE_XENOGRAFT)
    assert _rules(findings) == ['facet_scope_mismatch']


def test_age_not_in_mouse_or_xenograft_allowances():
    assert 'age' not in facet_scopes.MOUSE_ONLY_ALLOWED_TAGS
    assert facet_scopes.facet_allowed_scopes('age') == [facet_scopes.SCOPE_HUMAN]


@pytest.mark.parametrize('organisms, expected', [
    (['rat'], ['age_not_human']),
    (['human', 'mouse'], ['age_not_human']),
    ([], []),
    (['human'], []),
    (['mouse'], []),  # reported by the scope rule
])
def test_age_human_only(organisms, expected):
    assert _rules(facet_lint.lint_age_human_only(RENAME_API, organisms)) == expected


def test_age_human_only_without_age_facet():
    assert facet_lint.lint_age_human_only({'obs_rename': {'sex': 'ant_sex'}}, ['rat']) == []


@pytest.mark.parametrize('top', [89.0, 90.0])
def test_top_code_warns_without_caveat(top):
    findings = _data([(70.0, 3), (top, 2)])
    assert _rules(findings) == ['age_top_coded'] and findings[0].severity == facet_lint.WARNING


def test_top_code_silenced_by_caveat():
    metadata = {'description': 'Ages above 89 are Top-coded at 90 by the author.'}
    assert _data([(70.0, 3), (90.0, 2)], metadata=metadata) == []


def test_max_below_top_code_is_quiet():
    assert _data([(70.0, 3), (85.0, 2)]) == []


def test_derive_numeric_map_checked():
    api = {'obs_derive': {'ant_age': {'source': 'Age', 'dtype': 'numerical', 'map': {'old': 150, 'young': 30}}}}
    findings = facet_lint.lint_api_data(api, {'Age': 'categorical'}, _lookup({'Age': [('old', 1), ('young', 1)]}))
    assert _rules(findings) == ['age_not_years']


def test_derive_categorical_age_is_error():
    api = {'obs_derive': {'ant_age': {'source': 'Age', 'dtype': 'categorical', 'map': {'aged': 'old'},
                                      'default': 'young'}}}
    findings = facet_lint.lint_api_data(api, {'Age': 'categorical'}, _lookup({'Age': [('aged', 1)]}))
    assert 'age_not_years' in _rules(findings)


def test_rules_registered():
    for rule in ('age_not_years', 'age_top_coded', 'age_not_human'):
        assert rule in facet_lint.RULES


def test_registry_text_says_human_years_only():
    assert obscol_tags.facet_registry()['age']['facet_scopes'] == ['human_postmortem']
    entry = obscol_tags.facet_definition('ant_age')
    assert entry['summary'].startswith('Age of the human donor in years')
    assert 'Human datasets only' in entry['definition']


def test_help_text_says_human_years_only():
    import help_content
    assert 'human donors only' in help_content.get_topic('facet-age').body_html
    assert 'humans only' in help_content.get_term('age-twins').definition_html
    assert 'human donors, in years' in help_content.get_term('facet').definition_html
