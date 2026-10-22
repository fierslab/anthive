"""The watcher's versioned merge of pending obs columns into the h5ad (lib/h5ad_merge.py, design section 26.3).

Gates (window, load, a deposit pending), the new version (minor when additive, major for a replaced value),
the archived previous h5ad (store and S3, sha256 kept), the new sha256 and previous_sha256 in the .md, the S3
push verified by HEAD, __pending_cols cleared, crash recovery on both sides of the swap, and the version chain
of an obs-columns deposit followed by its merge.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import anndata
import h5py
import numpy as np
import pandas as pd
import pytest
import yaml

import h5ad_merge
import ingest_pipeline
import ingest_versions as iv
from anthive_db import AnnotationWriter, read_pending_cols
from tests.artefact_deposit_helpers import ArtefactFakeS3, FakeApi, FakeGit

CATALOG = 'cat/'
BUCKET = 'bucket'
CELLS = [f'cell{index}' for index in range(12)]
IN_WINDOW = dt.datetime(2026, 10, 23, 2, 30)
OUT_OF_WINDOW = dt.datetime(2026, 10, 23, 14, 0)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_dataset(root: Path, pending: bool = True, replace: bool = False) -> Path:
    """Merge2026/ds: a small h5ad, its duckdb, an .md at 1.1.0; with ``pending`` two columns written through
    AnnotationWriter (state categorical, score numerical); with ``replace`` the h5ad's n_counts overwritten."""
    import duckdb
    group = root / 'Merge2026'
    group.mkdir(parents=True)
    (group / 'index.md').write_text('---\ngroup: Merge2026\ngroup_id: MG6\n---\n')
    obs = pd.DataFrame({'n_counts': np.arange(12, dtype='f8'),
                        'cell_type': pd.Categorical(['MG'] * 6 + ['BAM'] * 6)}, index=CELLS)
    adata = anndata.AnnData(X=np.ones((12, 3), dtype='f4'), obs=obs,
                            var=pd.DataFrame(index=['Apoe', 'Trem2', 'Cst7']))
    h5ad = group / 'ds.h5ad'
    adata.write_h5ad(h5ad)
    (group / 'ds.h5ad.mus').write_text('organism=human\naccess=internal\ns3_bucket=bucket\ns3_key=cat/Merge2026/ds.h5ad\n'
                                       's3_etag=old\n')
    (group / 'ds.md').write_text('---\nid: ds\ntitle: Merge test\nversion: 1.1.0\nsha256: ' + sha(h5ad) + '\n'
                                 'changelog:\n- version: 1.0.0\n  date: 2026-01-01\n  changes: [first]\n'
                                 '- version: 1.1.0\n  date: 2026-02-01\n  changes: [obs columns]\n---\n')
    connection = duckdb.connect(str(group / 'ds.duckdb'))
    try:
        connection.execute('CREATE TABLE __cells (cell_id INTEGER PRIMARY KEY, cell_name VARCHAR)')
        connection.executemany('INSERT INTO __cells VALUES (?, ?)', list(enumerate(CELLS)))
        connection.execute('CREATE TABLE __obsnum (cell_id INTEGER PRIMARY KEY, n_counts DOUBLE)')
        connection.executemany('INSERT INTO __obsnum VALUES (?, ?)', [(index, float(index)) for index in range(12)])
        connection.execute('CREATE TABLE __obscat (cell_id INTEGER PRIMARY KEY, cell_type VARCHAR)')
        connection.executemany('INSERT INTO __obscat VALUES (?, ?)',
                               [(index, 'MG' if index < 6 else 'BAM') for index in range(12)])
    finally:
        connection.close()
    if pending:
        with AnnotationWriter(group / 'ds.duckdb', annotator='obs-columns-deposit', run_id='dep1') as writer:
            writer.add_categorical('state', pd.Series(['A', 'B'] * 5 + [None, None], index=CELLS))
            writer.add_numerical('score', pd.Series(np.linspace(0, 1, 12), index=CELLS))
            if replace:
                writer.add_numerical('n_counts', pd.Series(np.full(12, 7.0), index=CELLS), on_conflict='overwrite')
            writer.commit()
    return group / 'ds.md'


def merger(tmp_path: Path, s3=None, clock=IN_WINDOW, busy=None, **kwargs):
    s3 = s3 or ArtefactFakeS3()
    (tmp_path / 'store' / '.git').mkdir(parents=True, exist_ok=True)
    git, api = FakeGit(), FakeApi()
    instance = h5ad_merge.H5adMerger(store_root=tmp_path / 'store', state_dir=tmp_path / 'state', s3=s3,
                                     bucket=BUCKET, catalog_prefix=CATALOG, api=api, log=lambda message: None,
                                     run=git, clock=lambda: clock, busy=busy or (lambda: None), **kwargs)
    return instance, s3, git, api


def publish_catalog(s3, md: Path) -> None:
    """The catalog copy of the published h5ad and sidecar, as the dataset route left them."""
    h5ad = md.with_suffix('.h5ad')
    s3.put(f'{CATALOG}Merge2026/ds.h5ad', h5ad.read_bytes(), metadata={'sha256': sha(h5ad)})


def meta(md: Path):
    return yaml.safe_load(md.read_text().split('---\n')[1])


def test_the_window_wraps_past_midnight_and_an_empty_window_is_off():
    assert h5ad_merge.in_window(dt.datetime(2026, 1, 1, 1, 0), '01:00-06:00')
    assert not h5ad_merge.in_window(dt.datetime(2026, 1, 1, 6, 0), '01:00-06:00')
    assert h5ad_merge.in_window(dt.datetime(2026, 1, 1, 23, 30), '22:00-02:00')
    assert h5ad_merge.in_window(dt.datetime(2026, 1, 1, 1, 30), '22:00-02:00')
    assert not h5ad_merge.in_window(dt.datetime(2026, 1, 1, 12, 0), '22:00-02:00')
    for bad in ('1:00-6:00', '01:00-01:00', '25:00-06:00', 'night'):
        with pytest.raises(ValueError):
            h5ad_merge.parse_window(bad)


def test_outside_the_window_or_on_a_busy_host_nothing_is_merged(tmp_path):
    md = make_dataset(tmp_path / 'store')
    before = sha(md.with_suffix('.h5ad'))
    late, _s3, _git, _api = merger(tmp_path, clock=OUT_OF_WINDOW)
    assert late.run_pending() == []
    off, _s3, _git, _api = merger(tmp_path, window='')
    assert off.run_pending() == []
    busy, _s3, _git, _api = merger(tmp_path, busy=lambda: 'load 30.0 is above 8')
    assert busy.run_pending() == []
    assert sha(md.with_suffix('.h5ad')) == before and not read_pending_cols(md.with_suffix('.duckdb')).empty


def test_a_pending_deposit_for_the_dataset_holds_its_merge(tmp_path):
    md = make_dataset(tmp_path / 'store')
    instance, _s3, _git, _api = merger(tmp_path)
    assert instance.run_pending(deposit_pending=lambda db_id: db_id == 'MG6/ds') == []
    assert meta(md)['version'] == '1.1.0'


def test_merge_makes_a_minor_version_and_archives_the_previous_h5ad(tmp_path):
    md = make_dataset(tmp_path / 'store')
    h5ad = md.with_suffix('.h5ad')
    old_sha = sha(h5ad)
    instance, s3, git, api = merger(tmp_path)
    publish_catalog(s3, md)
    assert [entry['db_id'] for entry in instance.pending()] == ['MG6/ds']
    results = instance.run_pending()
    assert [(result.state, result.version) for result in results] == [('merged', '1.2.0')], results[0].detail
    new_sha = sha(h5ad)
    assert new_sha != old_sha
    merged = anndata.read_h5ad(h5ad)
    assert list(merged.obs['state'].astype(object).iloc[:3]) == ['A', 'B', 'A']
    assert merged.obs['state'].isna().sum() == 2
    assert np.allclose(merged.obs['score'].to_numpy(), np.linspace(0, 1, 12))
    assert list(merged.obs['n_counts']) == list(np.arange(12, dtype='f8'))       # untouched
    current = meta(md)
    assert current['version'] == '1.2.0' and current['sha256'] == new_sha
    entry = current['changelog'][-1]
    assert entry['previous_sha256'] == old_sha
    assert entry['changes'][0].startswith('Version class minor: additive')
    assert entry['changes'][1].startswith('h5ad: merged obs columns score, state (no data change')
    archived = tmp_path / 'store' / 'Merge2026' / '_versions' / 'ds' / '1.1.0'
    assert sha(archived / 'ds.h5ad') == old_sha and meta(archived / 'ds.md')['version'] == '1.1.0'
    assert s3.objects[f'{CATALOG}Merge2026/_versions/ds/1.1.0/ds.h5ad']['metadata']['sha256'] == old_sha
    assert s3.objects[f'{CATALOG}Merge2026/ds.h5ad']['metadata']['sha256'] == new_sha
    assert s3.objects[f'{CATALOG}Merge2026/ds.h5ad']['body'] == h5ad.read_bytes()
    assert s3.objects[f'{CATALOG}Merge2026/ds.duckdb']['body'] == md.with_suffix('.duckdb').read_bytes()
    assert read_pending_cols(md.with_suffix('.duckdb')).empty
    commits = [cmd for cmd in git.commands if 'commit' in cmd]
    assert len(commits) == 1 and commits[0][-2:] == ['Merge2026/ds.md', 'Merge2026/ds.h5ad.mus']
    sidecar = (tmp_path / 'store' / 'Merge2026' / 'ds.h5ad.mus').read_text()
    assert f's3_etag={s3.objects[f"{CATALOG}Merge2026/ds.h5ad"]["etag"]}' in sidecar and 'organism=human' in sidecar
    assert 's3_etag=old' in (archived / 'ds.h5ad.mus').read_text()          # the archived sidecar is the old one
    assert s3.objects[f'{CATALOG}Merge2026/ds.h5ad.mus']['body'].decode() == sidecar
    assert ('POST', '/admin/rescan') in api.calls
    assert instance.history()[-1]['state'] == 'merged' and instance.pending() == []
    assert not list((tmp_path / 'store' / 'Merge2026').glob('.*merge-work'))
    # nothing pending any more: the next run does nothing
    assert instance.run_pending() == []


def test_a_replaced_value_makes_a_major_version(tmp_path):
    md = make_dataset(tmp_path / 'store', replace=True)
    instance, s3, _git, _api = merger(tmp_path)
    publish_catalog(s3, md)
    result = instance.run_pending()[0]
    assert result.state == 'merged' and result.version == '2.0.0'
    assert meta(md)['changelog'][-1]['changes'][0].startswith('Version class major: data altered: replaced obs '
                                                              'values of n_counts')
    assert list(anndata.read_h5ad(md.with_suffix('.h5ad')).obs['n_counts']) == [7.0] * 12


def test_a_crash_after_the_swap_is_finished_by_the_next_run(tmp_path, monkeypatch):
    md = make_dataset(tmp_path / 'store')
    instance, s3, _git, _api = merger(tmp_path)
    publish_catalog(s3, md)
    original = h5ad_merge.H5adMerger._push
    calls = []

    def fail_first_h5ad_push(self, path, sha256, warnings):
        calls.append(path.name)
        if path.suffix == '.h5ad' and calls.count('ds.h5ad') == 1:
            return False
        return original(self, path, sha256, warnings)
    monkeypatch.setattr(h5ad_merge.H5adMerger, '_push', fail_first_h5ad_push)
    first = instance.run_pending()
    assert first[0].state == 'failed' and 'could not be pushed' in first[0].detail
    assert meta(md)['version'] == '1.2.0'                                   # the store has the new version
    assert (tmp_path / 'state' / 'merge_swaps' / 'resume' / 'merge-MG6_ds.json').exists()
    again = instance.run_pending(now=True)
    assert [result.state for result in again] == ['finished']
    assert s3.objects[f'{CATALOG}Merge2026/ds.h5ad']['metadata']['sha256'] == sha(md.with_suffix('.h5ad'))
    assert not (tmp_path / 'state' / 'merge_swaps' / 'resume' / 'merge-MG6_ds.json').exists()
    assert meta(md)['version'] == '1.2.0'                                   # bumped once


class Kill(BaseException):
    """Stands in for SIGKILL inside the swap."""


def test_a_crash_before_the_swap_rolls_back_and_merges_again(tmp_path, monkeypatch):
    md = make_dataset(tmp_path / 'store')
    old_sha = sha(md.with_suffix('.h5ad'))
    instance, s3, _git, _api = merger(tmp_path)
    publish_catalog(s3, md)
    original = ingest_pipeline.SwapJournal.commit

    def die(self):
        raise Kill()
    monkeypatch.setattr(ingest_pipeline.SwapJournal, 'commit', die)
    with pytest.raises(Kill):
        instance.run_pending()
    assert sha(md.with_suffix('.h5ad')) == old_sha and meta(md)['version'] == '1.1.0'
    monkeypatch.setattr(ingest_pipeline.SwapJournal, 'commit', original)
    results = instance.run_pending()
    assert [result.state for result in results] == ['merged']
    assert meta(md)['version'] == '1.2.0' and read_pending_cols(md.with_suffix('.duckdb')).empty


def test_a_store_h5ad_that_is_not_the_published_one_is_not_merged(tmp_path):
    md = make_dataset(tmp_path / 'store')
    md.write_text(md.read_text().replace(f'sha256: {sha(md.with_suffix(".h5ad"))}', 'sha256: ' + 'f' * 64))
    instance, s3, _git, _api = merger(tmp_path)
    result = instance.run_pending()[0]
    assert result.state == 'failed' and 'not the published one' in result.detail
    assert not read_pending_cols(md.with_suffix('.duckdb')).empty


def test_the_watchers_pending_check_reads_deposits_and_unsettled_items(tmp_path):
    import ingest_watch
    db = ingest_watch.StateDB(tmp_path / 'watch.sqlite')
    check = h5ad_merge.deposit_pending_check(tmp_path / 'nothing', db)
    assert not check('MG6/ds')
    db.upsert('artefact:dep9', state='held', db_id='obs:MG6/ds')
    assert check('MG6/ds')
    db.upsert('artefact:dep9', state='live', db_id='obs:MG6/ds')
    assert not check('MG6/ds')


def test_an_obs_deposit_then_its_merge_keep_the_version_chain_consistent(tmp_path):
    """1.1.0 (obs columns, served from the duckdb) -> 1.2.0 (merged into the h5ad): the deposit's minor step
    and the merge's minor step both carry the class line, so the purge policy reads them as additive."""
    md = make_dataset(tmp_path / 'store')
    text = md.read_text().replace('changes: [obs columns]', "changes: ['Version class minor: additive: obs "
                                                            "columns added', 'obs column state added']")
    md.write_text(text)
    instance, s3, _git, _api = merger(tmp_path)
    publish_catalog(s3, md)
    assert instance.run_pending()[0].version == '1.2.0'
    changelog = meta(md)['changelog']
    assert [entry['version'] for entry in changelog] == ['1.0.0', '1.1.0', '1.2.0']
    assert iv.successor_class(changelog, '1.0.0', '1.1.0') == 'minor'
    assert iv.successor_class(changelog, '1.1.0', '1.2.0') == 'minor'
    with h5py.File(md.with_suffix('.h5ad'), 'r') as handle:
        assert 'state' in handle['obs'] and 'score' in handle['obs']
