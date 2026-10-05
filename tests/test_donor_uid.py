"""Template and join forms of ``obs_derive`` (``ant_donor_uid``) and donor counting.

Covers ``lib/obs_derive_forms.py`` (spec validation, value normalisation,
crosswalk parsing), the view SQL built by ``anthive_db.build_obs_view_sql``
(run on a real DuckDB), ``metadata.get_api_config``, ``facet_mapping``
(kinds ``template`` and ``join`` with counts), ``facet_lint`` and
``md_check``, ``config.get_store_file``, ``lib/donor_keys.py`` and the
distinct donor counts in ``facet_results`` replication.

The crosswalk here is synthetic: cohort ``fakecoh`` with made-up ids.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd
import pytest

import config
import donor_keys
import facet_lint
import facet_mapping
import facet_results as fr
import obs_derive_forms as forms
from anthive_db import build_obs_view_sql
from metadata import MetadataError, get_api_config

CROSSWALK = (
    'cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets\n'
    'fakecoh\tdonor_id\tD1\tfakecoh:00000001\tsynthetic\t2\n'
    'fakecoh\tprojid\t42\tfakecoh:00000042\tsynthetic\t1\n'
    'fakecoh\tdonor_id\tD9\tfakecoh:00000009\tsynthetic\t1\n'
    'fakecoh\tdonor_id\tDUP\tfakecoh:A\tsynthetic\t1\n'
    'fakecoh\tdonor_id\tDUP\tfakecoh:B\tsynthetic\t1\n'
    'othercoh\tdonor_id\tD2\tothercoh:X\tsynthetic\t1\n'
)

JOIN = {'source': 'donor', 'join': 'donor_crosswalk.tsv', 'cohort': 'fakecoh'}
TEMPLATE = {'source': 'donor', 'template': '{group_id}:{value}'}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / 'store'
    root.mkdir()
    (root / 'donor_crosswalk.tsv').write_text(CROSSWALK)
    return root


def _dataset(conn: duckdb.DuckDBPyConnection) -> None:
    """Six cells: text donor labels in __obscat, a numerical projid in __obsnum."""
    conn.execute("CREATE TABLE __cells AS SELECT range AS cell_id, 'c' || range AS cell_name FROM range(6)")
    conn.execute('CREATE TABLE __obscat (cell_id BIGINT, donor VARCHAR)')
    conn.execute("INSERT INTO __obscat VALUES (0, 'D1'), (1, ' D1 '), (2, 'D2'), (3, NULL), (4, '42.0'), (5, '')")
    conn.execute('CREATE TABLE __obsnum (cell_id BIGINT, projid DOUBLE)')
    conn.execute("INSERT INTO __obsnum VALUES (0, 42), (1, 42.0), (2, 7), (3, NULL), (4, 'nan'), (5, 1.5)")


def _view(conn, api, store_root, group_id='ZZ9', view='obscat') -> List[Tuple]:
    own, sibling = ('__obscat', '__obsnum') if view == 'obscat' else ('__obsnum', '__obscat')
    columns = {t: [r[0] for r in conn.execute(
        'SELECT column_name FROM information_schema.columns WHERE table_name = ?', [t]).fetchall()]
        for t in ('__obscat', '__obsnum')}
    conn.execute(f'DROP VIEW IF EXISTS {view}')
    conn.execute(build_obs_view_sql(view, own, columns[own], api, sibling, columns[sibling],
                                    group_id=group_id, store_root=store_root))
    return conn.execute(f'SELECT * FROM {view} ORDER BY cell_name').fetchall()


# ── spec validation and values ───────────────────────────────────────────────

def test_template_fields_and_errors():
    assert forms.template_fields('{group_id}:{value}') == ['group_id', 'value']
    assert forms.template_fields('{{literal}}-{value}') == ['value']
    for bad in ('{group_id}', '{value:>8}', '{value!r}', '{cohort}:{value}', '{value', ''):
        with pytest.raises(forms.DeriveFormError):
            forms.template_fields(bad)


def test_validate_spec():
    assert forms.validate_spec('ant_donor_uid', TEMPLATE)['form'] == 'template'
    clean = forms.validate_spec('ant_donor_uid', {**JOIN, 'fallback_template': '{group_id}:{value}'})
    assert (clean['form'], clean['dtype'], clean['cohort']) == ('join', 'categorical', 'fakecoh')
    bad_specs = [
        {**TEMPLATE, 'dtype': 'numerical'},
        {**TEMPLATE, 'map': {'a': 'b'}},
        {**JOIN, 'cohort': ''},
        {**JOIN, 'join': '../secret.tsv'},
        {**JOIN, 'join': '/etc/passwd'},
        {**JOIN, 'default': 'x'},
        {**JOIN, 'fallback_template': '{group_id}'},
        {'template': '{value}'},
    ]
    for spec in bad_specs:
        with pytest.raises(forms.DeriveFormError):
            forms.validate_spec('ant_donor_uid', spec)


@pytest.mark.parametrize('value, expected', [
    (None, None), (float('nan'), None), ('', None), ('  ', None), (' D1 ', 'D1'), ('42.0', '42'),
    ('42.50', '42.50'), (42.0, '42'), (42, '42'), (1.5, '1.5'), ('-3.00', '-3'), ('R1.0x', 'R1.0x'),
])
def test_normalize_value(value, expected):
    assert forms.normalize_value(value) == expected


def test_render_template():
    assert forms.render_template('{group_id}:{value}', ' 7.0 ', 'GF4') == 'GF4:7'
    assert forms.render_template('{group_id}:{value}', None, 'GF4') is None


def test_crosswalk_parsing_conflicts_and_reload(store: Path):
    crosswalk = forms.load_crosswalk(store / 'donor_crosswalk.tsv')
    assert crosswalk.cohorts() == ['fakecoh', 'othercoh']
    assert crosswalk.lookup('FakeCoh', '42.0') == 'fakecoh:00000042'
    assert crosswalk.lookup('fakecoh', 'DUP') is None          # conflicting alias left out
    assert crosswalk.conflicts == (('fakecoh', 'DUP'),)
    assert crosswalk.lookup('fakecoh', 'D2') is None           # other cohort
    # an edited table is re-read
    path = store / 'donor_crosswalk.tsv'
    path.write_text(CROSSWALK + 'fakecoh\tdonor_id\tD7\tfakecoh:00000007\tsynthetic\t1\n')
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10_000_000))
    assert forms.load_crosswalk(path).lookup('fakecoh', 'D7') == 'fakecoh:00000007'
    bad = store / 'bad.tsv'
    bad.write_text('cohort\talias\nx\ty\n')
    with pytest.raises(forms.CrosswalkError):
        forms.load_crosswalk(bad)


def test_get_store_file_refuses_escapes(monkeypatch, tmp_path: Path):
    monkeypatch.setenv('ANTHIVE_H5ADSTORE', str(tmp_path))
    assert config.get_store_file('donor_crosswalk.tsv') == tmp_path / 'donor_crosswalk.tsv'
    for bad in ('../x.tsv', '/abs/x.tsv', ''):
        with pytest.raises(ValueError):
            config.get_store_file(bad)


def test_get_api_config_accepts_and_rejects_forms():
    api = get_api_config({'api': {'obs_derive': {
        'ant_donor_uid': {**JOIN, 'fallback_template': '{group_id}:{value}'},
        'other_uid': TEMPLATE,
        'ant_braak_group': {'source': 'b', 'dtype': 'categorical', 'map': {'1': 'low'}}}}})
    assert api['obs_derive']['ant_donor_uid']['form'] == 'join'
    assert api['obs_derive']['other_uid'] == {'source': 'donor', 'dtype': 'categorical', 'form': 'template',
                                             'template': '{group_id}:{value}'}
    assert api['obs_derive']['ant_braak_group']['map'] == {'1': 'low'}
    with pytest.raises(MetadataError):
        get_api_config({'api': {'obs_derive': {'ant_donor_uid': {**TEMPLATE, 'template': '{x}'}}}})


# ── view SQL ─────────────────────────────────────────────────────────────────

def test_view_template_and_join(store: Path):
    conn = duckdb.connect()
    _dataset(conn)
    api = {'obs_derive': {
        'ant_donor_uid': {**JOIN, 'cohort': 'FakeCoh', 'fallback_template': '{group_id}:{value}'},
        'tmpl_num': {'source': 'projid', 'template': '{group_id}-{value}'},
        'join_num': {'source': 'projid', 'join': 'donor_crosswalk.tsv', 'cohort': 'fakecoh'},
    }}
    rows = _view(conn, api, store)
    assert rows == [
        ('c0', 'D1', 'fakecoh:00000001', 'ZZ9-42', 'fakecoh:00000042'),
        ('c1', ' D1 ', 'fakecoh:00000001', 'ZZ9-42', 'fakecoh:00000042'),   # trimmed; 42.0 -> 42
        ('c2', 'D2', 'ZZ9:D2', 'ZZ9-7', None),                              # other cohort: fallback / NULL
        ('c3', None, None, None, None),                                     # NULL stays NULL
        ('c4', '42.0', 'fakecoh:00000042', None, None),                     # NaN is NULL
        ('c5', '', None, 'ZZ9-1.5', None),                                  # empty is NULL
    ]
    # the served view needs no file at query time
    (store / 'donor_crosswalk.tsv').unlink()
    assert conn.execute('SELECT count(ant_donor_uid) FROM obscat').fetchone()[0] == 4


def test_view_rebuild_rereads_crosswalk(store: Path):
    conn = duckdb.connect()
    _dataset(conn)
    api = {'obs_derive': {'ant_donor_uid': JOIN}}
    assert [r[2] for r in _view(conn, api, store)][2] is None
    path = store / 'donor_crosswalk.tsv'
    path.write_text(CROSSWALK + 'fakecoh\tdonor_id\tD2\tfakecoh:00000002\tsynthetic\t1\n')
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10_000_000))
    assert [r[2] for r in _view(conn, api, store)][2] == 'fakecoh:00000002'


@pytest.mark.parametrize('api, message', [
    ({'obs_derive': {'ant_donor_uid': {**JOIN, 'cohort': 'nope'}}}, "cohort 'nope' is not in"),
    ({'obs_derive': {'ant_donor_uid': {**JOIN, 'join': 'missing.tsv'}}}, 'not found in the store root'),
    ({'obs_derive': {'ant_donor_uid': TEMPLATE}}, 'group id is unknown'),
])
def test_view_drops_unbuildable_derives_loudly(store: Path, caplog, api, message):
    conn = duckdb.connect()
    _dataset(conn)
    with caplog.at_level(logging.WARNING, logger='anthive_db'):
        rows = _view(conn, api, store, group_id=None)
    assert len(rows[0]) == 2  # cell_name, donor only
    assert message in caplog.text and 'ant_donor_uid is not published' in caplog.text


def test_template_without_group_id_needs_none(store: Path):
    conn = duckdb.connect()
    _dataset(conn)
    rows = _view(conn, {'obs_derive': {'ant_donor_uid': {'source': 'donor', 'template': 'x:{value}'}}},
                 store, group_id=None)
    assert rows[0][-1] == 'x:D1'


# ── facet mapping ────────────────────────────────────────────────────────────

def test_facet_mapping_kinds_and_counts(store: Path):
    api = {'obs_derive': {'ant_donor_uid': JOIN}, 'obs_rename': {'donor': 'ant_donor'}}
    entries = {e['facet']: e for e in facet_mapping.project_dataset(api, ['ant_donor', 'ant_donor_uid'], [],
                                                                    group_id='ZZ9', store_root=store)}
    entry = entries['ant_donor_uid']
    assert entry['mapping_kind'] == facet_mapping.JOIN
    assert (entry['cohort'], entry['join_table'], entry['join_problem']) == ('fakecoh', 'donor_crosswalk.tsv', None)
    assert entry['n_crosswalk_aliases'] == 3 and entry['unmapped'] == forms.RULE_UNMATCHED
    facet_mapping.translate_observed(entry, [('D1', 10), ('D2', 4), (None, 2)], 2, store_root=store)
    table = {row['from']: (row['to'], row['rule'], row['n_cells']) for row in entry['value_map']}
    assert table == {'D1': ('fakecoh:00000001', 'crosswalk', 10), 'D2': (None, 'unmatched', 4),
                     None: (None, 'null', 2)}
    assert entry['rule_counts'] == {'crosswalk': 10, 'unmatched': 4, 'null': 2}

    template = facet_mapping.project_dataset({'obs_derive': {'ant_donor_uid': TEMPLATE}}, ['ant_donor_uid'], [],
                                             group_id='ZZ9', store_root=store)[0]
    assert template['mapping_kind'] == facet_mapping.TEMPLATE and template['template'] == '{group_id}:{value}'
    facet_mapping.translate_observed(template, [(' D1 ', 3), (7.0, 1)], 2)
    assert [(r['to'], r['rule']) for r in template['value_map']] == [('ZZ9:D1', 'template'), ('ZZ9:7', 'template')]

    missing = facet_mapping.project_dataset({'obs_derive': {'ant_donor_uid': {**JOIN, 'cohort': 'nope'}}},
                                            [], [], group_id='ZZ9', store_root=store)[0]
    assert 'nope' in missing['join_problem'] and missing['n_crosswalk_aliases'] is None


# ── lint ─────────────────────────────────────────────────────────────────────

def _rules(findings) -> Dict[str, str]:
    return {f.rule: f.severity for f in findings}


def test_lint_static_rules(store: Path):
    def lint(spec, group_id='ZZ9'):
        return _rules(facet_lint.lint_api({'obs_derive': {'ant_donor_uid': spec}}, store_root=store,
                                          group_id=group_id))
    assert lint(JOIN) == {'crosswalk_conflict': 'warning'}  # DUP in fakecoh
    assert lint({**JOIN, 'cohort': 'othercoh'}) == {}
    assert lint({**JOIN, 'join': 'nope.tsv'}) == {'join_table_missing': 'error'}
    assert lint({**JOIN, 'cohort': 'nope'}) == {'join_cohort_unknown': 'error'}
    assert lint({**TEMPLATE, 'template': '{nope}'}) == {'derive_form': 'error'}
    assert lint(TEMPLATE, group_id='') == {'template_group_id': 'error'}
    assert lint(TEMPLATE, group_id=None) == {}
    assert lint({**TEMPLATE, 'dtype': 'numerical'}) == {'derive_form': 'error', 'derive_dtype': 'error'}


def test_lint_data_rules(store: Path):
    columns = {'donor': 'categorical'}
    values = {'donor': [('D1', 5), ('D2', 3), ('Dx', 1)]}

    def data(spec):
        return facet_lint.lint_api_data({'obs_derive': {'ant_donor_uid': spec}}, columns,
                                        lambda col: values.get(col), store_root=store)
    findings = data(JOIN)
    assert _rules(findings) == {'join_unmatched': 'warning'}
    assert '2 value(s) of donor (4 cells)' in findings[0].message
    assert data({**JOIN, 'fallback_template': '{group_id}:{value}'}) == []
    assert data(TEMPLATE) == []  # no unmapped_value for the template form
    assert _rules(data({**TEMPLATE, 'source': 'gone'})) == {'source_missing': 'error'}


def test_md_check_runs_the_form_rules(tmp_path: Path):
    from md_check import MdCheckReport, check_md_folder
    store_root = tmp_path / 'store'
    folder = store_root / 'Group2026'
    folder.mkdir(parents=True)
    (store_root / 'donor_crosswalk.tsv').write_text(CROSSWALK)
    (folder / 'index.md').write_text('---\ngroup_id: GR6\ngroup: Group2026\n---\n')
    (folder / 'ds.h5ad').write_bytes(b'')
    (folder / 'ds.md').write_text('---\nid: DS1\napi:\n  obs_derive:\n    ant_donor_uid:\n'
                                  '      source: donor\n      join: donor_crosswalk.tsv\n      cohort: nope\n---\n')
    report = MdCheckReport()
    check_md_folder(store_root, report, fix=False)
    assert 'join_cohort_unknown' in report.rule_files


# ── donor keys and counting ──────────────────────────────────────────────────

def test_donor_keys():
    assert donor_keys.donor_key('fakecoh:1', 'D1', 'G1') == ('fakecoh:1', 'uid')
    assert donor_keys.donor_key(None, ' 7.0 ', 'G1') == ('G1:7', 'fallback')
    assert donor_keys.donor_key(None, None, 'G1') is None
    first = donor_keys.donor_keys([('fakecoh:1', 'D1'), (None, 'D3')], 'G1')
    second = donor_keys.donor_keys([(None, 'D3'), ('G1:D9', 'D9')], 'G1')   # template-form uid
    assert donor_keys.summarize([first, second, {'G1:D9': 'fallback'}]) == {
        'n_donors': 3, 'n_donors_uid': 2, 'n_donors_fallback': 1}
    keys, stats = donor_keys.donor_key_map([('D1', 'u1'), ('D1', 'u1'), ('D2', None), ('D3', 'a'), ('D3', 'b'),
                                            (None, 'u9')], 'G1')
    assert keys == {'D1': 'u1', 'D2': 'G1:D2', 'D3': 'G1:D3'}
    assert stats == {'n_donors': 3, 'n_donors_uid': 1, 'n_uid_conflicts': 1}
    assert donor_keys.distinct_count([{'a', 'b'}, {'b', 'c'}]) == 3
    assert donor_keys.distinct_count([{'a'}, None]) is None
    assert len(donor_keys.key_digest('fakecoh:1')) == 16


def _write_results(root: Path, db_id: str, donors: Optional[List[str]], q_value: float) -> None:
    group, stem = db_id.split('/')
    folder = root / group
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{'facet': 'sex', 'cell_type': 'microglial cell', 'celltype_source': 'ant_cell_type',
                   'gene': 'GENE1', 'logFC': 0.5, 'ci_low': 0.4, 'ci_high': 0.6, 'p_value': q_value / 2,
                   'q_value': q_value, 'n_donors': len(donors or ['x'] * 3), 'descriptive_only': False,
                   'n_lt5': False}]).to_parquet(folder / f'{stem}.facet_tests.parquet')
    unit = {'facet': 'sex', 'cell_type': 'microglial cell', 'celltype_source': 'ant_cell_type', 'status': 'ok',
            'n_donors_analysed': len(donors or ['x'] * 3), 'n_genes_tested': 1, 'n_sig_q05': 1,
            'covariates': [], 'covariates_omitted': []}
    if donors is not None:
        unit['donor_key_digests'] = sorted(donor_keys.key_digest(k) for k in donors)
    (folder / f'{stem}.facet_run_manifest.json').write_text(json.dumps(
        {'tool': 'ant-facet-test/1.0', 'db_id': db_id, 'units': [unit], 'parameters': {}}))


def test_replication_reports_distinct_donors(tmp_path: Path):
    root = tmp_path / '_facet_tests'
    # two cohorts share one person (fakecoh:1) by uid
    _write_results(root, 'A1/ds', ['fakecoh:1', 'fakecoh:2', 'A1:D3'], 0.001)
    _write_results(root, 'B1/ds', ['fakecoh:1', 'B1:D4'], 0.001)
    table = ['status\tcohort\tcell_class\trole\tdb_id\trule_from_audit_8_3',
             'DRAFT\tCohortA\tmicroglia\tanchor\tA1/ds\tanchor', 'DRAFT\tCohortB\tmicroglia\tanchor\tB1/ds\tanchor']
    (root / fr.COHORT_TABLE_NAME).write_text('\n'.join(table) + '\n')
    datasets = [{'db_id': f'{g}/ds', 'group_folder': g, 'stem': 'ds'} for g in ('A1', 'B1')]
    results = fr.FacetResults(root)
    rep = results.gene_effects('sex', 'GENE1', datasets)['replication'][0]
    assert (rep['n_donors_sum'], rep['n_donors_distinct']) == (5, 4)
    top = results.recurring_genes('sex', datasets)
    assert top['donors_in_cell_type'] == {'n_donors_sum': 5, 'n_donors_distinct': 4}
    assert (top['rows'][0]['n_donors_sum'], top['rows'][0]['n_donors_distinct']) == (5, 4)
    card = results.gene_effects('sex', 'GENE1', datasets)
    assert 'donor_digests' not in card['units'][0] and 'donor_key_digests' not in json.dumps(card)
    # a run that predates the donor keys: distinct is not recorded
    _write_results(root, 'B1/ds', None, 0.001)
    rep = fr.FacetResults(root).gene_effects('sex', 'GENE1', datasets)['replication'][0]
    assert rep['n_donors_distinct'] is None and rep['n_donors_sum'] == 6
