"""Deprecated facet name aliases (apoe_carrier -> apoe4_carrier)."""
import duckdb
import pytest

import config
from anthive_db import build_obs_view_sql
from facet_aliases import canonical_facet, is_deprecated_facet_column
from facet_lint import lint_api
from obscol_tags import facet_registry


@pytest.mark.parametrize('name, expected', [
    ('ant_apoe_carrier', 'ant_apoe4_carrier'),
    ('apoe_carrier', 'apoe4_carrier'),
    ('ant_apoe4_carrier', 'ant_apoe4_carrier'),
    ('ant_sex', 'ant_sex'),
    ('', ''),
])
def test_canonical_facet(name, expected):
    assert canonical_facet(name) == expected


def test_deprecated_column_detection():
    assert is_deprecated_facet_column('ant_apoe_carrier')
    assert not is_deprecated_facet_column('ant_apoe4_carrier')


def test_registry_marks_the_old_tag_deprecated():
    registry = facet_registry()
    assert registry['apoe4_carrier']['status'] == 'registered'
    assert registry['apoe_carrier']['status'] == 'deprecated'
    assert registry['apoe_carrier']['replaced_by'] == ['apoe4_carrier']


def test_lint_warns_on_the_deprecated_target():
    findings = lint_api({'obs_rename': {'carrier': 'ant_apoe_carrier'}})
    assert any(f.rule == 'namespace_deprecated' and 'ant_apoe4_carrier' in f.message for f in findings)
    assert not any(f.rule.startswith('namespace') for f in
                   lint_api({'obs_rename': {'carrier': 'ant_apoe4_carrier'}}))


def _view_connection(api):
    connection = duckdb.connect(':memory:')
    connection.execute('CREATE TABLE __cells AS SELECT * FROM (VALUES (0, \'c0\'), (1, \'c1\')) t(cell_id, cell_name)')
    connection.execute("CREATE TABLE __obscat AS SELECT * FROM (VALUES (0, 'E3/E4'), (1, 'E3/E3')) t(cell_id, apoe)")
    connection.execute(build_obs_view_sql('obscat', '__obscat', ['cell_id', 'apoe'], api))
    return connection


def test_view_computes_the_new_name_and_keeps_the_old_as_alias():
    connection = _view_connection({'obs_rename': {'apoe': 'ant_apoe'}})
    rows = connection.execute('SELECT ant_apoe4_carrier, ant_apoe_carrier FROM obscat ORDER BY cell_name').fetchall()
    assert rows == [('E4+', 'E4+'), ('E4-', 'E4-')]


def test_view_has_no_alias_without_the_new_column():
    connection = _view_connection({})
    columns = [row[0] for row in connection.execute('DESCRIBE obscat').fetchall()]
    assert 'ant_apoe_carrier' not in columns and 'ant_apoe4_carrier' not in columns


def test_facets_visible_accepts_the_old_name(monkeypatch):
    monkeypatch.delenv('ANTHIVE_FACETS_VISIBLE', raising=False)
    assert config.get_facets_visible({'facets_visible': ['ant_sex', 'ant_apoe_carrier']}) == [
        'ant_sex', 'ant_apoe4_carrier']
    monkeypatch.setenv('ANTHIVE_FACETS_VISIBLE', 'ant_apoe_carrier, ant_apoe4_carrier')
    assert config.get_facets_visible() == ['ant_apoe4_carrier']
    assert 'ant_apoe4_carrier' in config.DEFAULT_FACETS_VISIBLE
