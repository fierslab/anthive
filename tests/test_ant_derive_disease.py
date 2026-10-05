"""bin/annot/ant-derive-disease and bin/meta/ant-set-replicate: the pure parts and the .md edits."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml

import md_edit

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(relative: str, name: str):
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


derive = _load('bin/annot/ant-derive-disease', 'ant_derive_disease')
setrep = _load('bin/meta/ant-set-replicate', 'ant_set_replicate')

MD = """---
title: Syn
version: 0.1.2
sha256: abc
updated: '2026-10-01'
obscat_include:
- Sex
api:
  obs_rename:
    Overall: ant_disease
    Sex: ant_sex
  obs_value_rename:
    Overall:
      High: AD
changelog:
- version: 0.1.2
  date: '2026-10-01'
  changes:
  - earlier
caveats:
- level: low
  kind: note
  message: 'ant_adnc: scale. ant_disease uses the same column: AD = High.'
  scope: object
---

# Syn
"""


def _spec():
    return derive.DERIVATIONS['G24/MMG'][0]


def test_derivation_table_is_valid_and_covers_the_expected_datasets():
    for db_id, (spec, source) in derive.DERIVATIONS.items():
        derive.disease_rule.validate_spec(spec)
        assert source and spec['column'] == derive.COLUMN, db_id
    groups = {db_id.split('/')[0] for db_id in derive.DERIVATIONS}
    assert groups == {'G24', 'M23', 'GF4', 'MY4', 'Z20', 'WA4', 'P23'}
    assert len(derive.DERIVATIONS) == 27
    assert 'L26/PSY' not in derive.DERIVATIONS and 'Lee2026' in derive.SKIP_GROUPS


def test_compute_labels_from_a_duckdb(tmp_path):
    path = tmp_path / 'd.duckdb'
    connection = duckdb.connect(str(path))
    connection.execute('CREATE TABLE __cells (cell_id INTEGER, cell_name VARCHAR)')
    connection.execute("INSERT INTO __cells VALUES (0,'a'),(1,'b'),(2,'c'),(3,'d')")
    connection.execute('CREATE TABLE __obscat (cell_id INTEGER, "Braak" VARCHAR, "CERAD score" VARCHAR)')
    connection.execute("INSERT INTO __obscat VALUES (0,'Braak VI','Frequent'),(1,'Braak 0','Absent'),"
                       "(2,'Braak III','Absent'),(3,'Reference','Reference')")
    connection.close()
    labels = derive.compute_labels(path, _spec())
    assert labels.to_dict() == {'a': 'AD', 'b': 'control', 'c': None, 'd': None}
    assert derive.summarize(labels) == 'AD=1, control=1, NULL=2'
    bad = dict(_spec(), braak={'column': 'missing'})
    with pytest.raises(KeyError):
        derive.compute_labels(path, bad)


def test_md_operations_publish_the_derived_column_and_record_the_rule():
    md_yaml = yaml.safe_load(MD.split('---\n')[1])
    operations = derive.md_operations(md_yaml, _spec(), 'a source', '2026-10-05')
    new = md_edit.apply_edits(MD, operations)
    front = yaml.safe_load(new.split('---\n')[1])
    assert front['api']['obs_rename'] == {'Sex': 'ant_sex', 'disease_braak_cerad': 'ant_disease'}
    assert 'obs_value_rename' not in front['api']
    assert front['disease_basis'] == 'braak_cerad' and front['disease_derivation'] == _spec()
    assert front['obscat_include'] == ['Sex', 'disease_braak_cerad']
    assert front['version'] == '0.1.3' and front['updated'] == '2026-10-05'
    assert front['facet_notes']['disease'].startswith('pathologic AD, Braak plus neuritic plaque based')
    messages = [c['message'] for c in front['caveats']]
    assert messages[0] == 'ant_adnc: scale.'                    # the ADNC half survives
    assert messages[1].startswith('ant_disease: pathologic AD')
    assert front['changelog'][-1]['version'] == '0.1.3'
    again = yaml.safe_load(md_edit.apply_edits(new, derive.md_operations(front, _spec(), 'a source', '2026-10-05')
                                              [:-1]).split('---\n')[1])
    assert again['api']['obs_rename']['disease_braak_cerad'] == 'ant_disease'


def test_publishes_disease():
    assert derive.publishes_disease({'api': {'obs_rename': {'x': 'ant_disease'}}})
    assert derive.publishes_disease({'api': {'obs_derive': {'ant_disease': {}}}})
    assert not derive.publishes_disease({'api': {'obs_rename': {'x': 'ant_sex'}}})
    assert not derive.publishes_disease({})


def test_rule_text_mentions_the_rule_and_source():
    text = derive.rule_text('SEA-AD donor table')
    assert 'Braak stage III or higher' in text and 'SEA-AD donor table' in text


def test_set_replicate_decisions_are_valid():
    for db_id, (unit, column, why) in setrep.DECISIONS.items():
        assert unit in ('donor', 'animal', 'sample', 'none') and why, db_id
        assert (column is None) == (unit == 'none'), db_id
    assert setrep.DECISIONS['AZ5/SLM'] == ('animal', 'hash.ID', '12 hashtags, one per animal')
