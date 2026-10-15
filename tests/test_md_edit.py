"""lib/md_edit.py: minimal verified text edits of a dataset .md front matter."""
from __future__ import annotations

from pathlib import Path

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


# ── flow-style empty values (vark init writes `caveats: []`) ──────────────────

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


@pytest.mark.parametrize('name', ['vark_init_seed.md', 'store_md_with_empty_caveats.md'])
def test_append_caveat_after_a_flow_empty_list(name):
    text = (FIXTURES / name).read_text()
    assert '\ncaveats: []\n' in text
    caveat = {'level': 'low', 'text': 'Metadata update: age added.'}
    new_text = md_edit.apply_edits(text, [md_edit.append_top('caveats', caveat)])
    data = yaml.safe_load(new_text.split('---\n')[1])
    assert data['caveats'] == [caveat]
    assert 'caveats: []' not in new_text
    again = md_edit.apply_edits(new_text, [md_edit.append_top('caveats', {'level': 'low', 'text': 'two'})])
    assert len(yaml.safe_load(again.split('---\n')[1])['caveats']) == 2


def test_store_md_update_edits_with_empty_caveats_and_bump():
    text = (FIXTURES / 'store_md_with_empty_caveats.md').read_text()
    new_text = md_edit.apply_edits(text, [
        md_edit.append_top('caveats', {'level': 'low', 'text': 'x'}),
        md_edit.set_top('age', '33-62 years'),
        md_edit.bump_version(['metadata update'], '2026-10-15')])
    data = yaml.safe_load(new_text.split('---\n')[1])
    assert data['version'] == '1.0.2' and data['caveats'] == [{'level': 'low', 'text': 'x'}]


@pytest.mark.parametrize('line,before', [
    ('caveats: [{level: low, text: a}]', [{'level': 'low', 'text': 'a'}]),
    ('caveats:  [] # none yet', []),
    ('caveats: null', None),
    ('caveats: ~', None),
])
def test_append_after_inline_list_values(line, before):
    text = f'---\nid: X\n{line}\nversion: 1.0.0\n---\nbody\n'
    new_text = md_edit.apply_edits(text, [md_edit.append_top('caveats', {'level': 'low', 'text': 'b'})])
    data = yaml.safe_load(new_text.split('---\n')[1])
    assert data['caveats'] == (before or []) + [{'level': 'low', 'text': 'b'}]
    assert new_text.endswith('---\nbody\n')


@pytest.mark.parametrize('line', ['api: {}', 'api:', 'api: null', 'api: {obs_hidden: [a]}'])
def test_set_path_under_an_inline_mapping(line):
    text = f'---\nid: X\n{line}\nversion: 1.0.0\n---\n'
    new_text = md_edit.apply_edits(text, [md_edit.set_path(['api', 'obs_rename'], {'Braak': 'ant_braak'})])
    data = yaml.safe_load(new_text.split('---\n')[1])
    assert data['api']['obs_rename'] == {'Braak': 'ant_braak'}
    if 'obs_hidden' in line:
        assert data['api']['obs_hidden'] == ['a']


@pytest.mark.parametrize('line', ['changelog: []', 'changelog:', 'changelog: null'])
def test_bump_with_an_empty_changelog(line):
    text = f'---\nid: X\nversion: 1.0.0\n{line}\n---\n'
    new_text = md_edit.apply_edits(text, [md_edit.bump_version(['c'], '2026-10-15')])
    data = yaml.safe_load(new_text.split('---\n')[1])
    assert data['changelog'] == [{'version': '1.0.1', 'date': '2026-10-15', 'changes': ['c']}]
