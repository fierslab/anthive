"""lib/md_edit.py: minimal verified text edits of a dataset .md front matter."""
from __future__ import annotations

import pytest
import yaml

import md_edit

TEXT = """---
title: Syn
version: 0.1.4
sha256: abc
updated: '2026-10-01'
is_secondary: false
donor_group: syn
api:
  obs_rename:
    Braak: ant_braak
    Old disease: ant_disease
  obs_derive:
    ant_age:
      source: Age
      dtype: numerical
      map:
        '60': 60
  obs_value_rename:
    Old disease:
      High: AD
changelog:
- version: 0.1.4
  date: '2026-10-01'
  changes:
  - earlier change
---

# Syn

Body text stays.
"""


def _front(text):
    return yaml.safe_load(md_edit.FRONT_RE.match(text).group(1))


def test_set_top_inserts_before_api_and_replaces_existing():
    new = md_edit.apply_edits(TEXT, [md_edit.set_top('replicate_unit', 'donor'),
                                     md_edit.set_top('replicate_column', 'ant_donor')])
    assert _front(new)['replicate_unit'] == 'donor' and _front(new)['replicate_column'] == 'ant_donor'
    assert new.index('replicate_unit') < new.index('api:')
    assert new.endswith('Body text stays.\n')
    again = md_edit.apply_edits(new, [md_edit.set_top('replicate_unit', 'animal')])
    assert _front(again)['replicate_unit'] == 'animal' and again.count('replicate_unit') == 1


def test_nested_set_delete_and_untouched_lines():
    new = md_edit.apply_edits(TEXT, [
        md_edit.delete_path(['api', 'obs_rename', 'Old disease']),
        md_edit.delete_path(['api', 'obs_value_rename']),
        md_edit.set_path(['api', 'obs_rename', 'pathology_2x2'], 'ant_disease'),
        md_edit.set_path(['api', 'obs_derive', 'ant_cell_type'], {'source': 'Supertype', 'dtype': 'categorical',
                                                                   'map': {'a': 'b'}})])
    front = _front(new)
    assert front['api']['obs_rename'] == {'Braak': 'ant_braak', 'pathology_2x2': 'ant_disease'}
    assert 'obs_value_rename' not in front['api']
    assert front['api']['obs_derive']['ant_cell_type']['map'] == {'a': 'b'}
    assert front['api']['obs_derive']['ant_age']['map'] == {'60': 60}
    changed = {line for line in new.splitlines() if line not in TEXT.splitlines()}
    assert len(changed) < 12                        # a local diff, not a re-dump


def test_bump_version_updates_changelog_and_updated():
    new = md_edit.apply_edits(TEXT, [md_edit.bump_version(['a: b', 'second'], '2026-10-06')])
    front = _front(new)
    assert front['version'] == '0.1.5' and front['updated'] == '2026-10-06'
    assert front['changelog'][-1] == {'version': '0.1.5', 'date': '2026-10-06', 'changes': ['a: b', 'second']}
    assert len(front['changelog']) == 2
    no_log = TEXT.replace(TEXT[TEXT.index('changelog:'):TEXT.index('\n---\n\n#')], '').replace('\n\n---', '\n---')
    new = md_edit.apply_edits(no_log, [md_edit.bump_version(['x'], '2026-10-06')])
    assert _front(new)['changelog'][0]['version'] == '0.1.5'


def test_errors_are_raised_not_written():
    with pytest.raises(md_edit.MdEditError):
        md_edit.apply_edits('no front matter', [])
    with pytest.raises(md_edit.MdEditError):
        md_edit.apply_edits(TEXT, [md_edit.set_path(['nope', 'x'], 1)])
    with pytest.raises(md_edit.MdEditError):
        md_edit.apply_edits(TEXT.replace('version: 0.1.4', 'version: 1.0'), [md_edit.bump_version(['x'], 'd')])
    with pytest.raises(md_edit.MdEditError):
        md_edit.apply_edits(TEXT, [{'op': 'rename'}])
    assert md_edit.apply_edits(TEXT, [md_edit.delete_path(['api', 'missing'])]) == TEXT


def test_quoted_keys_and_values_with_colons():
    text = TEXT.replace("        '60': 60", "        '60': 60\n        '61': 61")
    new = md_edit.apply_edits(text, [md_edit.set_path(['api', 'obs_derive', 'ant_age', 'map', '61'], 62),
                                     md_edit.set_top('note', 'a: b # c')])
    front = _front(new)
    assert front['api']['obs_derive']['ant_age']['map'] == {'60': 60, '61': 62}
    assert front['note'] == 'a: b # c'
