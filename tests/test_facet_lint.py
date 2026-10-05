"""Facet registry (L1), mapping lint and central facets (L2), lineage rules (L3).

Covers ``lib/obscol_tags_vocab.yaml`` as the single source of the closed
``ant_*`` namespace, ``lib/facet_lint.py`` (static, data and lineage rules),
the ``md_check`` integration, and the view builder's warning plus the
centrally computed ``ant_braak_group`` / ``ant_apoe4_carrier``.
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pytest
import yaml

import canonical_columns
import facet_lint
import obscol_tags
from anthive_db import build_obs_view_sql
from facet_mapping import COMPUTED, project_dataset
from md_check import MdCheckReport, check_md_folder, collect_db_ids

VOCAB = Path(obscol_tags.VOCAB_PATH)

APPROVED_REGISTERED = {
    'donor', 'sex', 'host_sex', 'age', 'pmi', 'disease', 'braak', 'braak_group', 'apoe',
    'apoe4_carrier', 'cell_type', 'tissue', 'genotype', 'author_cluster',
    'cognitive_status', 'comorbidity', 'adnc', 'donor_uid',
}


# ── L1: the registry ─────────────────────────────────────────────────────────

def test_registered_set_is_the_approved_one():
    assert set(obscol_tags.registered_facet_tags()) == APPROVED_REGISTERED


def test_canonical_tags_are_derived_from_the_vocab():
    vocab = yaml.safe_load(VOCAB.read_text())
    facet_tags = [tag for tag, entry in vocab['tags'].items()
                  if ((entry or {}).get('facet') or {}).get('status') in ('registered', 'deprecated')]
    assert list(canonical_columns.CANONICAL_TAGS) == facet_tags
    assert set(canonical_columns.REGISTERED_TAGS) == APPROVED_REGISTERED
    assert set(canonical_columns.DEPRECATED_TAGS) == set(canonical_columns.CANONICAL_TAGS) - APPROVED_REGISTERED
    assert canonical_columns.NUMERICAL_TAGS == {'age', 'braak', 'pmi'}


def test_every_facet_has_a_complete_registry_entry():
    for tag, entry in obscol_tags.facet_registry().items():
        definition = obscol_tags.facet_definition(tag)
        assert definition['defined'] and definition['summary'] and definition['definition'], tag
        assert entry['scope'] in ('cross_dataset', 'dataset'), tag
        if entry['status'] == 'deprecated':
            assert 'replaced_by' in yaml.safe_load(VOCAB.read_text())['tags'][tag]['facet'], tag
        for replacement in entry['replaced_by']:
            assert replacement in obscol_tags.registered_facet_tags(), (tag, replacement)


def test_new_facet_vocabularies():
    registry = obscol_tags.facet_registry()
    assert registry['cognitive_status']['controlled_values'] == ['NCI', 'MCI', 'dementia', 'other_dementia']
    assert registry['comorbidity']['controlled_values'] == ['DLBD', 'vascular', 'SCZ']
    assert registry['adnc']['controlled_values'] == ['Not', 'Low', 'Intermediate', 'High']
    assert registry['adnc']['ordered'] is True
    assert registry['braak']['range'] == (0.0, 6.0) and registry['braak']['integer']
    assert registry['author_cluster']['scope'] == 'dataset'
    assert '<cohort>:<id>' in obscol_tags.facet_definition('ant_donor_uid')['summary']
    assert registry['braak_group']['computed_from'] == 'braak'


def test_legacy_alias_set_is_frozen():
    # facets registered after the 2026-08 migration never had a bare name
    assert 'cell_type' not in canonical_columns.BACKCOMPAT_LEGACY_NAMES
    assert canonical_columns.BACKCOMPAT_LEGACY_NAMES['braak'] == 'ant_braak'
    assert canonical_columns.canonicalise_legacy_target('tissue') == 'tissue'


def test_namespace_rule_modes():
    api = {'obs_rename': {'Lesion': 'ant_lesion', 'region': 'ant_region', 'sex': 'ant_sex'}}
    findings = facet_lint.lint_api(api)
    by_rule = {f.rule: f for f in findings}
    assert by_rule['namespace_unregistered'].severity == facet_lint.WARNING
    assert 'ant_lesion' in by_rule['namespace_unregistered'].message
    assert by_rule['namespace_deprecated'].severity == facet_lint.WARNING
    assert 'ant_tissue' in by_rule['namespace_deprecated'].message
    strict = {f.rule: f for f in facet_lint.lint_api(api, namespace_mode='error')}
    assert strict['namespace_unregistered'].severity == facet_lint.ERROR
    assert not [f for f in findings if 'ant_sex' in f.message]


def test_treatment_is_a_dataset_local_column_not_a_facet():
    """Treatment arms vary too much between experiments to share a facet."""
    assert 'treatment' not in obscol_tags.facet_registry()
    assert 'treatment' not in canonical_columns.CANONICAL_TAGS
    assert 'treatment' in obscol_tags.load_vocab()['tags']  # still usable as an obs tag
    for entry in obscol_tags.facet_registry().values():
        assert 'treatment' not in entry['replaced_by']
    findings = facet_lint.lint_api({'obs_derive': {'treatment': {'source': 'cond', 'map': {'a': 'vehicle'}}},
                                    'obs_rename': {'drug': 'ant_treatment'}})
    assert [f.rule for f in findings] == ['namespace_unregistered']
    assert 'ant_treatment' in findings[0].message


# ── L2: static mapping rules ─────────────────────────────────────────────────

def _rules(findings):
    return sorted({f.rule for f in findings})


def test_rename_chain():
    findings = facet_lint.lint_api({'obs_rename': {'sample': 'donor_id', 'donor_id': 'ant_donor'}})
    assert _rules(findings) == ['rename_chain']
    assert findings[0].severity == facet_lint.ERROR


def test_string_map_on_numerical_facet_and_non_numeric_values():
    api = {'obs_derive': {
        'ant_pmi': {'source': 'PMI', 'dtype': 'categorical', 'map': {'5h': '5h'}},
        'ant_age': {'source': 'Age', 'dtype': 'numerical', 'map': {'90+': '90+', '80': 80}},
    }}
    findings = facet_lint.lint_api(api)
    assert _rules(findings) == ['derive_dtype', 'numeric_value']
    assert all(f.severity == facet_lint.ERROR for f in findings)
    assert sum(f.rule == 'numeric_value' for f in findings) == 2


def test_braak_range_and_dead_key():
    api = {'obs_derive': {'ant_braak': {'source': 'Braak', 'dtype': 'numerical',
                                        'map': {'IV': 4, 'VII': 7, 'half': 2.5}}}}
    findings = facet_lint.lint_api(api)
    assert _rules(findings) == ['numeric_range']
    assert len(findings) == 2 and any('7' in f.message for f in findings)
    renamed = facet_lint.lint_api({'obs_rename': {'b': 'ant_braak'}, 'obs_value_rename': {'b': {'VII': '7'}}})
    assert _rules(renamed) == ['numeric_range']


def test_controlled_values_and_braak_group_rule():
    api = {
        'obs_value_rename': {'Sex': {'M': 'male'}},
        'obs_rename': {'Sex': 'ant_sex'},
        'obs_derive': {'ant_braak_group': {'source': 'Braak', 'dtype': 'categorical',
                                           'map': {'Braak 0': 'low', 'II': 'low', 'III': 'low', 4: 'medium'}}},
    }
    findings = facet_lint.lint_api(api)
    assert _rules(findings) == ['braak_group_rule', 'controlled_value']
    group = next(f for f in findings if f.rule == 'braak_group_rule')
    assert 'III: low (rule: medium)' in group.message


def test_braak_helpers():
    assert [facet_lint.braak_group_of(v) for v in (0, 2, 3, 4.0, 5, 6, 7, 2.5, None, 'x')] == \
        ['low', 'low', 'medium', 'medium', 'high', 'high', None, None, None, None]
    assert [facet_lint.parse_braak_label(v) for v in ('Braak IV', 'VI', '0', 3.0, 'Braak stage 2', 'n/a')] == \
        [4, 6, 0, 3, 2, None]


def test_clean_mapping_has_no_findings():
    api = {
        'obs_rename': {'donor': 'ant_donor', 'sex': 'ant_sex', 'braak': 'ant_braak'},
        'obs_value_rename': {'sex': {'M': 'm', 'F': 'f'}},
        'obs_derive': {'ant_braak_group': {'source': 'braak', 'dtype': 'categorical',
                                           'map': {0: 'low', 3: 'medium', 6: 'high'}}},
        'obs_constant': {'ant_cell_type': {'value': 'microglial cell', 'dtype': 'categorical'}},
    }
    assert facet_lint.lint_api(api) == []


# ── L2: data rules ───────────────────────────────────────────────────────────

def _lookup(values):
    return lambda column: values.get(column)


def test_data_rules_source_missing_and_unmapped_values():
    api = {
        'obs_rename': {'donor_id': 'ant_donor'},
        'obs_derive': {
            'ant_braak': {'source': 'Braak', 'dtype': 'numerical', 'map': {'Braak IV': 4}},
            'ant_braak_group': {'source': 'Braak', 'dtype': 'categorical', 'map': {'Braak IV': 'medium'},
                                'default': 'unknown'},
            'ant_age': {'source': 'AgeMissing', 'dtype': 'numerical', 'map': {}},
        },
    }
    columns = {'Braak': 'categorical', 'sample': 'categorical'}
    values = {'Braak': [('Braak IV', 100), ('Reference', 7)]}
    findings = facet_lint.lint_api_data(api, columns, _lookup(values))
    assert _rules(findings) == ['rename_source_missing', 'source_missing', 'unmapped_value']
    unmapped = next(f for f in findings if f.rule == 'unmapped_value')
    assert "'Reference' (7 cells)" in unmapped.message and 'ant_braak]' in unmapped.message
    # a default covers unmapped values
    assert not any('ant_braak_group' in f.message for f in findings)


def test_data_rules_numeric_source_matching_mirrors_sql():
    api = {'obs_derive': {'ant_braak_group': {'source': 'braak', 'dtype': 'categorical',
                                              'map': {0: 'low', 1: 'low', 4: 'medium'}}}}
    columns = {'braak': 'numerical'}
    values = {'braak': [(0.0, 5), (1.0, 3), (4.0, 2), (6.0, 1)]}
    findings = facet_lint.lint_api_data(api, columns, _lookup(values))
    assert len(findings) == 1 and '6.0' in findings[0].message
    # a text column compares text: '1.0' does not match the key 1
    findings = facet_lint.lint_api_data(api, {'braak': 'categorical'},
                                        _lookup({'braak': [('1', 2), ('1.0', 3)]}))
    assert len(findings) == 1 and "'1.0'" in findings[0].message


def test_data_rules_numeric_facet_from_text():
    api = {'obs_rename': {'PMI': 'ant_pmi', 'Braak': 'ant_braak'},
           'obs_value_rename': {'PMI': {'unknown': None}}}
    columns = {'PMI': 'categorical', 'Braak': 'numerical'}
    values = {'PMI': [('5.5', 10), ('12h', 3), ('unknown', 2)], 'Braak': [(3.0, 4), (7.0, 1)]}
    findings = facet_lint.lint_api_data(api, columns, _lookup(values))
    assert _rules(findings) == ['numeric_from_text', 'numeric_range', 'numeric_value']
    text = next(f for f in findings if f.rule == 'numeric_value')
    assert "'12h'" in text.message and 'unknown' not in text.message


# ── L3: lineage rules ────────────────────────────────────────────────────────

def test_lineage_rules():
    known = {'V25/SAL', 'V25/SSC'}
    assert facet_lint.lint_lineage({'is_secondary': True, 'reanalysis_of': ['V25/SAL']}, known) == []
    findings = facet_lint.lint_lineage({'reanalysis_of': ['V25/SAL', 'X99/NOP']}, known)
    assert _rules(findings) == ['reanalysis_not_secondary', 'reanalysis_target_missing']
    assert all(f.severity == facet_lint.ERROR for f in findings)
    assert _rules(facet_lint.lint_lineage({'donor_group': 'Lu 2026'}, known)) == ['donor_group_format']
    assert facet_lint.lint_lineage({'donor_group': 'madrc-hyman-2024'}, known) == []


# ── md_check integration ─────────────────────────────────────────────────────

def _write_md(path: Path, frontmatter: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\n' + yaml.safe_dump(frontmatter, sort_keys=False) + '---\n')
    if path.name != 'index.md':
        path.with_suffix('.h5ad').write_bytes(b'')


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / 'store'
    _write_md(root / 'Study2024' / 'index.md', {'group': 'Study2024', 'group_id': 'S24'})
    _write_md(root / 'Study2024' / 'primary.md', {
        'id': 'PRI', 'donor_group': 'study2024',
        'api': {'obs_rename': {'donor': 'ant_donor', 'Braak': 'ant_braak', 'Lesion': 'ant_lesion'}}})
    _write_md(root / 'Study2024' / 'reanalysis.md', {
        'id': 'REA', 'reanalysis_of': ['S24/PRI', 'S24/GONE'],
        'api': {'obs_rename': {'sample': 'donor_id', 'donor_id': 'ant_donor'}}})
    # underscore folders are archive: never linted
    _write_md(root / '_backup' / 'old.md', {'id': 'OLD', 'api': {'obs_rename': {'a': 'ant_zzz'}}})
    return root


def test_md_check_reports_rules_per_file(store):
    report = MdCheckReport()
    check_md_folder(store, report, fix=False)
    assert report.rule_files['namespace_unregistered'] == {'Study2024/primary.md'}
    assert report.rule_files['rename_chain'] == {'Study2024/reanalysis.md'}
    assert report.rule_files['reanalysis_target_missing'] == {'Study2024/reanalysis.md'}
    assert report.rule_files['reanalysis_not_secondary'] == {'Study2024/reanalysis.md'}
    assert not any('_backup' in message for message in report.errors + report.warnings)
    assert any('namespace_unregistered' in line for line in report.rule_summary())
    assert collect_db_ids(store) == {'S24/PRI', 'S24/REA'}


def test_md_check_data_rules_read_the_duckdb(store):
    duckdb_path = store / 'Study2024' / 'primary.duckdb'
    connection = duckdb.connect(str(duckdb_path))
    connection.execute('CREATE TABLE __obscat (cell_id INTEGER, donor VARCHAR)')
    connection.execute('CREATE TABLE __obsnum (cell_id INTEGER, Braak DOUBLE)')
    connection.execute("INSERT INTO __obscat VALUES (0, 'd1'), (1, 'd2')")
    connection.execute('INSERT INTO __obsnum VALUES (0, 3), (1, 9)')
    connection.close()
    report = MdCheckReport()
    check_md_folder(store, report, fix=False, with_data=True)
    assert report.data_checked == 1
    assert report.data_unavailable == ['Study2024/reanalysis.md']
    assert report.rule_files['numeric_range'] == {'Study2024/primary.md'}
    assert any("9.0 (1 cells)" in e for e in report.errors)


# ── L2: view builder ─────────────────────────────────────────────────────────

def _view(api, obscat, obsnum, rows_cat, rows_num):
    connection = duckdb.connect()
    connection.execute('CREATE TABLE __cells (cell_id INTEGER, cell_name VARCHAR)')
    connection.execute('CREATE TABLE __obscat (cell_id INTEGER, ' + ', '.join(f'"{c}" VARCHAR' for c in obscat) + ')')
    connection.execute('CREATE TABLE __obsnum (cell_id INTEGER, ' + ', '.join(f'"{c}" DOUBLE' for c in obsnum) + ')')
    for index, (cat, num) in enumerate(zip(rows_cat, rows_num)):
        connection.execute('INSERT INTO __cells VALUES (?, ?)', [index, f'c{index}'])
        connection.execute(f'INSERT INTO __obscat VALUES (?{", ?" * len(obscat)})', [index, *cat])
        connection.execute(f'INSERT INTO __obsnum VALUES (?{", ?" * len(obsnum)})', [index, *num])
    for view, own, sibling, own_cols, sibling_cols in (
            ('obscat', '__obscat', '__obsnum', obscat, obsnum),
            ('obsnum', '__obsnum', '__obscat', obsnum, obscat)):
        connection.execute(build_obs_view_sql(view, own, ['cell_id', *own_cols], api,
                                              sibling_underscore_table=sibling,
                                              sibling_columns=['cell_id', *sibling_cols]))
    return connection


def test_builder_computes_braak_group_and_apoe4_carrier():
    api = {'obs_rename': {'APOE': 'ant_apoe'},
           'obs_value_rename': {'APOE': {'34': 'E3/E4', '33': 'E3/E3', '44': 'E4/E4'}},
           'obs_derive': {'ant_braak': {'source': 'Braak', 'dtype': 'numerical',
                                        'map': {'Braak 0': 0, 'Braak II': 2, 'Braak III': 3, 'Braak VI': 6}}}}
    connection = _view(api, ['Braak', 'APOE'], ['dummy'],
                       [('Braak 0', '34'), ('Braak III', '33'), ('Braak VI', '44'), ('Reference', 'E4'),
                        ('Braak II', None)],
                       [(1.0,)] * 5)
    rows = connection.execute('SELECT c.cell_name, ant_braak_group, ant_apoe4_carrier FROM obscat c '
                              'ORDER BY cell_name').fetchall()
    assert rows == [('c0', 'low', 'E4+'), ('c1', 'medium', 'E4-'), ('c2', 'high', 'E4+'),
                    ('c3', None, None), ('c4', 'low', None)]


def test_builder_keeps_a_declared_braak_group():
    api = {'obs_rename': {'braak': 'ant_braak'},
           'obs_derive': {'ant_braak_group': {'source': 'braak', 'dtype': 'categorical',
                                              'map': {1: 'early'}, 'default': 'late'}}}
    connection = _view(api, ['x'], ['braak'], [('a',), ('b',)], [(1.0,), (5.0,)])
    rows = connection.execute('SELECT ant_braak_group FROM obscat ORDER BY cell_name').fetchall()
    assert rows == [('early',), ('late',)]
    columns = [r[0] for r in connection.execute('DESCRIBE obscat').fetchall()]
    assert columns.count('ant_braak_group') == 1 and 'ant_apoe4_carrier' not in columns


def test_builder_warns_on_missing_derive_source(caplog):
    api = {'obs_derive': {'ant_age': {'source': 'Age_typo', 'dtype': 'numerical', 'map': {}}}}
    with caplog.at_level(logging.WARNING, logger='anthive_db'):
        sql = build_obs_view_sql('obsnum', '__obsnum', ['cell_id', 'age'], api,
                                 sibling_underscore_table='__obscat', sibling_columns=['cell_id'],
                                 db_label='G1/ds1')
    assert 'ant_age' not in sql
    assert any('Age_typo' in r.getMessage() and 'G1/ds1' in r.getMessage() for r in caplog.records)


def test_facet_mapping_describes_computed_facets():
    entries = {e['facet']: e for e in project_dataset(
        {'obs_rename': {'Braak': 'ant_braak', 'apoe': 'ant_apoe'}},
        live_categorical=['ant_apoe', 'ant_braak_group', 'ant_apoe4_carrier'], live_numerical=['ant_braak'])}
    assert entries['ant_braak_group']['mapping_kind'] == COMPUTED
    assert entries['ant_braak_group']['live'] is True
    assert entries['ant_apoe4_carrier']['source_columns'] == ['ant_apoe']
    assert '5 to 6 high' in entries['ant_braak_group']['computed_rule']
