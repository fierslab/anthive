"""ant-sync 2.46.5: only catalogued datasets are pulled, the glob filter holds in priority mode, and
same-content files are never downloaded again.

Found on the intern server (`ant sync pull --dry-run` on 2.46.4, sync_policy.mode: priority, role
slave): the plan pulled 43 retired datasets (S3 .duckdb objects whose .md was deleted from the
h5adstore, about 40 GB), ignored the positional glob ('Francken2026/*'), and overwrote 346
_facet_tests/ and _module_variability_cache/ files whose local copy had the S3 size and content
(the primary had re-pushed identical bytes, so S3's LastModified moved past the local mtime).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from tests.test_ant_sync_dry_run import MTIME, RecordingS3, _args, _patch, _snapshot, ant_sync  # noqa: F401


def _md(store: Path, rel: str) -> None:
    path = store / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\nid: x\n---\n')


def _catalogued_store(tmp_path: Path) -> Path:
    """G1/a, G1/b and G2/c catalogued; G1/off inactive; G1/retired has no .md; _staging/s is in a _ folder."""
    store = tmp_path / 'store'
    for rel in ('G1/index.md', 'G1/a.md', 'G1/b.md', 'G1/off.md', 'G2/index.md', 'G2/c.md', '_staging/s.md'):
        _md(store, rel)
    (store / 'G1' / 'off.inactive').write_text('')
    return store


OBJECTS = {
    'G1/a.h5ad': b'a h5ad',
    'G1/a.duckdb': b'a duckdb',
    'G1/a.layer.X.by_gene.parquet': b'a layer',
    'G1/b.duckdb': b'b duckdb',
    'G2/c.h5ad': b'c h5ad',
    'G2/c.duckdb': b'c duckdb',
    'G2/c.de.parquet': b'c de',
    'G1/off.h5ad': b'inactive h5ad',
    'G1/off.duckdb': b'inactive duckdb',
    'G1/retired.h5ad': b'retired h5ad bytes',
    'G1/retired.duckdb': b'retired duckdb bytes',
    'G1/retired.layer.X.by_gene.parquet': b'retired layer',
    'G1/static/supp.xlsx': b'supp',
    'gene_stats_index.parquet': b'gsi',
    '_facet_tests/G1/a.facet_tests.parquet': b'facet results',
    '_module_variability_cache/x.parquet': b'mv cache',
}
RETIRED = {'G1/retired.h5ad', 'G1/retired.duckdb', 'G1/retired.layer.X.by_gene.parquet', 'G1/off.h5ad',
           'G1/off.duckdb'}


def _pulled(client) -> set:
    return {call[1] for call in client.writes if call[0] == 'download_file'}


def _no_s3_writes_but_downloads(client) -> bool:
    return all(call[0] == 'download_file' for call in client.writes)


# ── bug 1: retired / inactive datasets ───────────────────────────────────────

def test_catalogue_is_the_catalog_scan(ant_sync, tmp_path):
    store = _catalogued_store(tmp_path)
    catalogued, inactive = ant_sync._catalogue(store)
    assert catalogued == {'G1/a', 'G1/b', 'G2/c'}
    assert inactive == {'G1/off'}


def test_dataset_stem(ant_sync):
    assert ant_sync._dataset_stem('G1/a.h5ad') == 'G1/a'
    assert ant_sync._dataset_stem('G1/a.duckdb') == 'G1/a'
    assert ant_sync._dataset_stem('G/101010sn.1.astro.duckdb') == 'G/101010sn.1.astro'
    assert ant_sync._dataset_stem('G1/a.layer.counts.by_cell.parquet') == 'G1/a'
    assert ant_sync._dataset_stem('G1/a.de.parquet') == 'G1/a'
    assert ant_sync._dataset_stem('G1/notes.txt') is None


def test_priority_never_pulls_uncatalogued_datasets_and_reports_them(ant_sync, monkeypatch, tmp_path, capsys):
    store = _catalogued_store(tmp_path)
    (store / 'G1' / 'retired.duckdb').write_bytes(b'old local retired copy')
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, catalogued=None)
    monkeypatch.setattr(ant_sync, '_read_priorities', lambda folder, default: {})
    ant_sync.cmd_pull(_args(dry_run=False, evict=True), {'bucket': 'bk'}, store)
    pulled = _pulled(client)
    assert not pulled & RETIRED
    assert {'G1/a.duckdb', 'G1/a.layer.X.by_gene.parquet', 'G1/b.duckdb', 'G2/c.duckdb', 'G2/c.de.parquet'} <= pulled
    assert _no_s3_writes_but_downloads(client)                       # nothing deleted or written on S3
    assert (store / 'G1' / 'retired.duckdb').read_bytes() == b'old local retired copy'   # left alone
    out = capsys.readouterr().out
    assert 'on S3 but not catalogued (retired?): 2 dataset(s)' in out
    assert 'G1/retired  (2 object(s); no .md in the checkout; a local copy exists, left alone)' in out
    assert 'G1/off  (1 object(s); inactive (.inactive sidecar))' in out


def test_priority_dry_run_reports_uncatalogued_and_changes_nothing(ant_sync, monkeypatch, tmp_path, capsys):
    store = _catalogued_store(tmp_path)
    client = RecordingS3(OBJECTS)
    admin_calls, _git = _patch(ant_sync, monkeypatch, client, store, catalogued=None)
    before = _snapshot(store)
    ant_sync.cmd_pull(_args(), {'bucket': 'bk'}, store)
    assert client.writes == [] and admin_calls == [] and _snapshot(store) == before
    out = capsys.readouterr().out
    assert 'retired' in out.split('not catalogued')[1]
    assert 'PULL' not in [line.split()[5] for line in out.splitlines()
                          if line.rstrip().endswith('G1/retired.duckdb') and len(line.split()) > 6]


@pytest.mark.parametrize('flags', [dict(include_duckdb=True), dict(only='.h5ad'), dict()])
def test_generic_pull_never_pulls_uncatalogued_datasets(ant_sync, monkeypatch, tmp_path, capsys, flags):
    store = _catalogued_store(tmp_path)
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, mode='all', catalogued=None)
    ant_sync.cmd_pull(_args(dry_run=False, **flags), {'bucket': 'bk'}, store)
    pulled = _pulled(client)
    assert not pulled & RETIRED
    assert 'G1/a.h5ad' in pulled and 'G2/c.h5ad' in pulled
    if not flags.get('only'):
        assert {'gene_stats_index.parquet', '_facet_tests/G1/a.facet_tests.parquet', 'G1/static/supp.xlsx'} <= pulled
    assert _no_s3_writes_but_downloads(client)
    assert 'on S3 but not catalogued (retired?)' in capsys.readouterr().out


# ── bug 2: the glob filter in priority mode ──────────────────────────────────

def test_priority_honours_the_glob_filter(ant_sync, monkeypatch, tmp_path, capsys):
    store = _catalogued_store(tmp_path)
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, catalogued=None)
    ant_sync.cmd_pull(_args(dry_run=False, filter='G2/*'), {'bucket': 'bk'}, store)
    assert _pulled(client) == {'G2/c.duckdb', 'G2/c.de.parquet'}
    assert 'is ignored' not in capsys.readouterr().out


def test_priority_filter_on_a_sidecar_brings_the_whole_dataset_and_shared_files_match_too(ant_sync, monkeypatch,
                                                                                          tmp_path):
    store = _catalogued_store(tmp_path)
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, catalogued=None)
    ant_sync.cmd_pull(_args(dry_run=False, filter='*.layer.*'), {'bucket': 'bk'}, store)
    assert _pulled(client) == {'G1/a.duckdb', 'G1/a.layer.X.by_gene.parquet'}
    client.writes.clear()
    ant_sync.cmd_pull(_args(dry_run=False, filter='_facet_tests/*'), {'bucket': 'bk'}, store)
    assert _pulled(client) == {'_facet_tests/G1/a.facet_tests.parquet'}


def test_priority_filter_limits_eviction_and_the_dry_run_plan(ant_sync, monkeypatch, tmp_path, capsys):
    store = _catalogued_store(tmp_path)
    (store / 'G1' / 'b.duckdb').write_bytes(OBJECTS['G1/b.duckdb'])
    (store / 'G2' / 'c.duckdb').write_bytes(OBJECTS['G2/c.duckdb'])
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, catalogued=None, max_local_gb=1e-12)   # nothing fits
    ant_sync.cmd_pull(_args(dry_run=True, filter='G2/*'), {'bucket': 'bk'}, store)
    filtered = capsys.readouterr().out
    ant_sync.cmd_pull(_args(dry_run=True), {'bucket': 'bk'}, store)
    unfiltered = capsys.readouterr().out
    assert filtered != unfiltered
    assert 'G2/c.duckdb' in filtered and 'G1/b.duckdb' not in filtered
    assert 'G1/b.duckdb' in unfiltered
    ant_sync.cmd_pull(_args(dry_run=False, evict=True, filter='G2/*'), {'bucket': 'bk'}, store)
    assert not (store / 'G2' / 'c.duckdb').exists()
    assert (store / 'G1' / 'b.duckdb').exists()                  # out of scope: not evicted


def test_generic_pull_honours_the_glob_filter(ant_sync, monkeypatch, tmp_path):
    store = _catalogued_store(tmp_path)
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, mode='all', catalogued=None)
    ant_sync.cmd_pull(_args(dry_run=False, filter='G2/*'), {'bucket': 'bk'}, store)
    assert _pulled(client) == {'G2/c.h5ad', 'G2/c.de.parquet'}


# ── bug 3: same size and same content is in sync ─────────────────────────────

FACET = '_facet_tests/G1/a.facet_tests.parquet'
CACHE = '_module_variability_cache/x.parquet'


def _same_content_store(tmp_path: Path, local_mtime: int) -> Path:
    store = _catalogued_store(tmp_path)
    for rel in (FACET, CACHE, 'G1/a.layer.X.by_gene.parquet'):
        (store / rel).parent.mkdir(parents=True, exist_ok=True)
        (store / rel).write_bytes(OBJECTS[rel])
        os.utime(store / rel, (local_mtime, local_mtime))
    return store


@pytest.mark.parametrize('local_mtime', [MTIME - 3600, MTIME + 3600])   # S3 re-pushed later / local newer
def test_priority_slave_never_downloads_same_content_files(ant_sync, monkeypatch, tmp_path, capsys, local_mtime):
    store = _same_content_store(tmp_path, local_mtime)
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, catalogued=None)
    ant_sync.cmd_pull(_args(dry_run=True), {'bucket': 'bk'}, store)
    out = capsys.readouterr().out
    assert 'OVERWRITE' not in out
    assert '2 same content (mtime realigned only, no download)' in out
    ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    pulled = _pulled(client)
    assert FACET not in pulled and CACHE not in pulled and 'G1/a.layer.X.by_gene.parquet' not in pulled
    assert int((store / FACET).stat().st_mtime) == MTIME                   # realigned
    client.writes.clear()
    ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    assert client.writes == []                                              # second run is a no-op


def test_priority_slave_overwrites_other_content_and_says_why(ant_sync, monkeypatch, tmp_path, capsys):
    store = _same_content_store(tmp_path, MTIME + 3600)
    changed = b'X' * len(OBJECTS[FACET])                                    # same size, other bytes, newer
    (store / FACET).write_bytes(changed)
    os.utime(store / FACET, (MTIME + 3600, MTIME + 3600))
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, catalogued=None)
    ant_sync.cmd_pull(_args(dry_run=True), {'bucket': 'bk'}, store)
    out = capsys.readouterr().out
    assert (f'would OVERWRITE {FACET}' in out
            and 'same size but other content (ETag), local newer than S3: S3 is canonical on a slave' in out)
    assert '1 of them newer locally: a slave takes S3 as canonical' in out
    assert f'would OVERWRITE {CACHE}' not in out
    ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    assert FACET in _pulled(client) and (store / FACET).read_bytes() == OBJECTS[FACET]


def test_priority_primary_keeps_other_content(ant_sync, monkeypatch, tmp_path):
    store = _same_content_store(tmp_path, MTIME + 3600)
    (store / FACET).write_bytes(b'X' * len(OBJECTS[FACET]))
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, catalogued=None)
    monkeypatch.setattr(ant_sync, 'is_slave', lambda cfg=None: False)
    ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    assert FACET not in _pulled(client)
    assert (store / FACET).read_bytes() == b'X' * len(OBJECTS[FACET])


def test_file_status_uses_content_identity(ant_sync, tmp_path):
    path = tmp_path / 'f'
    path.write_bytes(b'abc')
    os.utime(path, (MTIME - 10, MTIME - 10))
    md5 = hashlib.md5(b'abc').hexdigest()
    assert ant_sync._file_status(path, 3, MTIME - 10, cheap_repull=True, etag=md5)[0] == 'HERE'
    assert ant_sync._file_status(path, 3, MTIME, cheap_repull=True, etag=md5)[0] == 'TOUCH'
    assert ant_sync._file_status(path, 3, MTIME, cheap_repull=True, etag='0' * 32)[0] == 'PULL*'
    assert ant_sync._file_status(path, 3, MTIME, cheap_repull=True, etag=None)[0] == 'PULL*'     # no ETag: mtime rule
    assert ant_sync._file_status(path, 3, MTIME - 20, cheap_repull=True, etag=None)[0] == 'TOUCH'
    assert ant_sync._file_status(path, 4, MTIME, cheap_repull=True, etag=md5)[0] == 'PULL*'
    assert ant_sync._same_content(path, md5, sha256=hashlib.sha256(b'abc').hexdigest()) is True
    assert ant_sync._same_content(path, md5, sha256='0' * 64) is False   # sha256 wins over the ETag
    assert ant_sync._same_content(path, 'ffff-2') is None                  # multipart, unknown part size


def test_generic_slave_pull_never_downloads_same_content_and_stamps_mtime(ant_sync, monkeypatch, tmp_path, capsys):
    """The old rule ('slave: mtime drift - overwriting') re-downloaded every same-size file whose local mtime
    was not the pushed original-mtime, and the generic pull never stamped one: every run re-pulled."""
    store = _same_content_store(tmp_path, MTIME + 3600)
    (store / 'G1' / 'a.h5ad').write_bytes(OBJECTS['G1/a.h5ad'])            # same content, download-time mtime
    (store / 'G2').mkdir(exist_ok=True)
    (store / 'G2' / 'c.h5ad').write_bytes(b'C' * len(OBJECTS['G2/c.h5ad']))   # same size, other content
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, mode='all', catalogued=None)
    before = _snapshot(store)
    ant_sync.cmd_pull(_args(dry_run=True), {'bucket': 'bk'}, store)
    out = capsys.readouterr().out
    assert client.writes == [] and _snapshot(store) == before
    assert '[would REALIGN] G1/a.h5ad' in out
    assert '[would UPDATE] G2/c.h5ad' in out and 'same size but other content' in out
    ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    pulled = _pulled(client)
    assert 'G1/a.h5ad' not in pulled and FACET not in pulled and CACHE not in pulled
    assert 'G2/c.h5ad' in pulled and (store / 'G2' / 'c.h5ad').read_bytes() == OBJECTS['G2/c.h5ad']
    assert int((store / 'G2' / 'c.h5ad').stat().st_mtime) == MTIME          # downloads are stamped
    client.writes.clear()
    ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    assert client.writes == []                                              # second run is a no-op
