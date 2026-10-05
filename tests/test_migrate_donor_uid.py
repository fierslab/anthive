"""bin/meta/ant-migrate-donor-uid on a synthetic store (fake ids only)."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / 'bin' / 'meta' / 'ant-migrate-donor-uid'

CROSSWALK = ('cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets\n'
             'fakecoh\tdonor_id\tD1\tfakecoh:00000001\tsynthetic\t1\n'
             'fakecoh\tdonor_id\tD2\tfakecoh:00000002\tsynthetic\t1\n')

JOINABLE = """---
id: DS1
title: "Joinable: keeps formatting"
api:
  obs_rename:
    donor: ant_donor
  obs_derive:
    ant_donor_uid:
      source: donor
      dtype: categorical
      map:
        D1: fakecoh:00000001
        D2: fakecoh:00000002
        D3: null
    ant_braak_group:
      source: braak
      dtype: categorical
      map:
        '1': low
  obs_hidden:
  - secret
---

Body text stays.
"""

DISAGREES = """---
id: DS2
api:
  obs_derive:
    ant_donor_uid:
      source: donor
      dtype: categorical
      map:
        D1: fakecoh:99999999
---
"""

TEMPLATE_ME = """---
id: DS3
api:
  obs_rename:
    sample_donor: ant_donor
---
"""

VALUE_RENAMED = """---
id: DS4
api:
  obs_rename:
    donor: ant_donor
  obs_value_rename:
    donor:
      d1: D1
---
"""

NO_DONOR = """---
id: DS5
api:
  obs_rename:
    Sex: ant_sex
---
"""


def _store(tmp_path: Path) -> Path:
    store = tmp_path / 'store'
    folder = store / 'Group2026'
    folder.mkdir(parents=True)
    (store / 'donor_crosswalk.tsv').write_text(CROSSWALK)
    (folder / 'index.md').write_text('---\ngroup_id: GR6\ngroup: Group2026\n---\n')
    for name, text in (('a', JOINABLE), ('b', DISAGREES), ('c', TEMPLATE_ME), ('d', VALUE_RENAMED),
                       ('e', NO_DONOR)):
        (folder / f'{name}.md').write_text(text)
    (store / '_staging').mkdir()
    (store / '_staging' / 'x.md').write_text(TEMPLATE_ME)
    return store


def _load():
    loader = importlib.machinery.SourceFileLoader('ant_migrate_donor_uid', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _api(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split('---')[1])['api']


def _run(store: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), '--store', str(store), *args],
                          capture_output=True, text=True, env={'PYTHONPATH': str(REPO_ROOT / 'lib'),
                                                                'PATH': '/usr/bin:/bin'})


def test_dry_run_writes_nothing(tmp_path: Path):
    store = _store(tmp_path)
    before = {p: p.read_text() for p in store.rglob('*.md')}
    result = _run(store, '--dry-run')
    assert result.returncode == 0, result.stderr
    assert {p: p.read_text() for p in store.rglob('*.md')} == before
    out = result.stdout
    assert 'value map -> join                1  (cohorts: fakecoh 1)' in out
    assert 'template added                   1' in out
    assert 'skipped, manual review           2' in out
    assert 'no declared ant_donor            1' in out
    assert 'fakecoh:0000' not in out  # never prints identifiers


def test_rewrite(tmp_path: Path):
    store = _store(tmp_path)
    result = _run(store, '--fallback')
    assert result.returncode == 0, result.stderr
    folder = store / 'Group2026'
    joined = _api(folder / 'a.md')
    assert joined['obs_derive']['ant_donor_uid'] == {
        'source': 'donor', 'join': 'donor_crosswalk.tsv', 'cohort': 'fakecoh',
        'fallback_template': '{group_id}:{value}'}
    assert joined['obs_derive']['ant_braak_group']['map'] == {'1': 'low'}  # neighbours untouched
    text = (folder / 'a.md').read_text()
    assert 'title: "Joinable: keeps formatting"' in text and text.endswith('Body text stays.\n')
    assert _api(folder / 'b.md')['obs_derive']['ant_donor_uid']['map'] == {'D1': 'fakecoh:99999999'}
    assert _api(folder / 'c.md')['obs_derive']['ant_donor_uid'] == {'source': 'sample_donor',
                                                                     'template': '{group_id}:{value}'}
    assert 'obs_derive' not in _api(folder / 'd.md')
    assert 'obs_derive' not in _api(folder / 'e.md')
    assert 'obs_derive' not in _api(store / '_staging' / 'x.md')
    # idempotent
    again = _run(store)
    assert 'join form already                1' in again.stdout
    assert 'template form already            1' in again.stdout


def test_edit_frontmatter_appends_to_an_existing_derive_block():
    module = _load()
    front = 'id: X\napi:\n  obs_derive:\n    other:\n      source: a\n      template: x{value}\n  obs_hidden:\n  - b\n'
    edited = module.edit_frontmatter(front, {'source': 'd', 'template': '{group_id}:{value}'})
    parsed = yaml.safe_load(edited)
    assert list(parsed['api']['obs_derive']) == ['other', 'ant_donor_uid']
    assert parsed['api']['obs_hidden'] == ['b']
