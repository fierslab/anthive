"""ant-sync: --dry-run changes nothing on any path, and priority mode bundles sidecars (2.46.1).

The anthive-intern site ran `ant-sync pull --include-duckdb --no-library --evict --dry-run` on a
priority (slave) site: --include-duckdb bypassed the priority driver and the generic path ignored
--dry-run, so raw .h5ad files came down and duckdbs were overwritten under a live api. These tests
drive every pull/push/evict/plan/library path against a fake S3 client that only allows listing and
HEAD, and assert that no transfer, delete, copy, local file change or ant-serve call happens.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'bin' / 'ops' / 'ant-sync'

READ_ONLY_CALLS = {'get_paginator', 'list_objects_v2', 'head_object'}
MTIME = 1_760_000_000


class RecordingS3:
    """Lists ``objects`` ({key: bytes}); every call other than listing / HEAD is recorded as a write."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)
        self.writes: list[tuple] = []

    def get_paginator(self, name):
        assert name == 'list_objects_v2'
        outer = self

        class _Pages:
            def paginate(self, Bucket, Prefix=''):  # noqa: N803
                yield {'Contents': [
                    {'Key': key, 'Size': len(body), 'ETag': '"x"',
                     'LastModified': dt.datetime.fromtimestamp(MTIME, dt.timezone.utc)}
                    for key, body in sorted(outer.objects.items()) if key.startswith(Prefix)]}
        return _Pages()

    def list_objects_v2(self, Bucket, Prefix='', **_):  # noqa: N803
        page = next(self.get_paginator('list_objects_v2').paginate(Bucket=Bucket, Prefix=Prefix))
        return {**page, 'IsTruncated': False}

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)
        return {'ContentLength': len(self.objects[Key]), 'ETag': '"x"', 'Metadata': {'original-mtime': str(MTIME)}}

    def download_file(self, bucket, key, filename, **_):
        self.writes.append(('download_file', key))
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        Path(filename).write_bytes(self.objects[key])

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        def _write(*args, **kwargs):
            self.writes.append((name, args, kwargs))
            return {}
        return _write


@pytest.fixture
def ant_sync():
    loader = importlib.machinery.SourceFileLoader('ant_sync_dry_run_test', str(SCRIPT))
    spec = importlib.util.spec_from_loader('ant_sync_dry_run_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _store(tmp_path: Path) -> Path:
    store = tmp_path / 'store'
    (store / 'G1').mkdir(parents=True)
    # a local duckdb that differs from S3 (would be overwritten) and a local-only one below the cutoff
    (store / 'G1' / 'a.duckdb').write_bytes(b'old')
    (store / 'G1' / 'gone.duckdb').write_bytes(b'local only')
    os.utime(store / 'G1' / 'a.duckdb', (MTIME - 100, MTIME - 100))
    return store


S3_OBJECTS = {
    'G1/a.h5ad': b'raw h5ad bytes',
    'G1/a.duckdb': b'duckdb v2 bytes',
    'G1/a.layer.X.by_gene.parquet': b'layer by gene',
    'G1/a.layer.X.by_cell.parquet': b'layer by cell',
    'G1/a.de.parquet': b'de',
    'G1/a.scores.parquet': b'scores',
    'G1/b.duckdb': b'b' * 50,
    'G1/b.layer.X.by_gene.parquet': b'B' * 20,
    'G1/static/supp.xlsx': b'supp',
    'G1/static/extra.h5ad': b'never',
    'gene_stats_index.parquet': b'gsi',
    'module_variability.parquet': b'mv',
    '_facet_tests/G1/a.facet_tests.parquet': b'ft',
    '_gene_stats_cache/G1/a.parquet': b'gsc',
    '_module_variability_cache/x.parquet': b'mvc',
}


def _patch(ant_sync, monkeypatch, client, store, *, mode='priority', max_local_gb=None):
    admin_calls: list = []
    config = {'sync_policy': {'mode': mode, 'role': 'slave', 'max_local_gb': max_local_gb},
              's3': {'bucket': 'bk'}}
    monkeypatch.setattr(ant_sync, '_s3_client', lambda cfg: client)
    monkeypatch.setattr(ant_sync, 'get_config', lambda: config)
    monkeypatch.setattr(ant_sync, '_admin_post', lambda *a, **k: admin_calls.append(a) or 200)
    monkeypatch.setattr(ant_sync, '_read_priorities', lambda folder, default: {'G1/b.duckdb': 90, 'G1/a.duckdb': 80})
    monkeypatch.setattr(ant_sync, '_free_space', lambda path: 10 ** 12)
    monkeypatch.setattr(ant_sync, 'require_primary', lambda **kw: None)
    monkeypatch.setattr(ant_sync, 'is_slave', lambda cfg=None: True)
    monkeypatch.setattr(ant_sync, '_rebuild_module_variability_if_stale',
                        lambda *a, **k: pytest.fail('module_variability rebuilt during a dry run'))
    git_calls: list = []
    monkeypatch.setattr(ant_sync, '_git_pull_h5adstore', lambda folder: git_calls.append(folder))
    return admin_calls, git_calls


def _args(**overrides):
    base = dict(filter=None, include_duckdb=False, only=None, max_bandwidth=None, max_concurrency=1,
                evict=False, dry_run=True, no_library=True, library_only=False, skip_no_mtime=False,
                no_rebuild_variability=False, local=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _snapshot(store: Path) -> dict:
    return {str(p.relative_to(store)): (p.read_bytes(), p.stat().st_mtime_ns) for p in store.rglob('*') if p.is_file()}


PULL_VARIANTS = [
    ('priority', dict()),
    ('priority', dict(include_duckdb=True, evict=True)),           # the intern command
    ('priority', dict(only='.duckdb')),
    ('priority', dict(only='.h5ad', evict=True)),
    ('all', dict()),
    ('all', dict(include_duckdb=True, evict=True)),
    ('all', dict(only='.duckdb,.h5ad')),
    ('duckdb', dict()),
    ('duckdb', dict(filter='G1/*')),
]


@pytest.mark.parametrize('mode,flags', PULL_VARIANTS)
def test_pull_dry_run_transfers_and_deletes_nothing(ant_sync, monkeypatch, tmp_path, capsys, mode, flags):
    store = _store(tmp_path)
    client = RecordingS3(S3_OBJECTS)
    admin_calls, git_calls = _patch(ant_sync, monkeypatch, client, store, mode=mode, max_local_gb=1e-7)
    before = _snapshot(store)
    ant_sync.cmd_pull(_args(**flags), {'bucket': 'bk'}, store)
    assert client.writes == []
    assert _snapshot(store) == before
    assert admin_calls == []
    assert git_calls == []
    assert 'dry-run' in capsys.readouterr().out


def test_push_dry_run_uploads_nothing(ant_sync, monkeypatch, tmp_path, capsys):
    store = _store(tmp_path)
    client = RecordingS3({})
    _patch(ant_sync, monkeypatch, client, store, mode='all')
    ant_sync.cmd_push(_args(), {'bucket': 'bk'}, store)
    assert client.writes == []
    out = capsys.readouterr().out
    assert '[would UPLOAD] G1/a.duckdb' in out and 'nothing uploaded' in out


def test_evict_and_plan_dry_run_delete_nothing(ant_sync, monkeypatch, tmp_path):
    store = _store(tmp_path)
    (store / 'G1' / 'b.duckdb').write_bytes(b'b' * 50)       # local, below a tiny cap -> EVICT candidate
    client = RecordingS3(S3_OBJECTS)
    admin_calls, _git = _patch(ant_sync, monkeypatch, client, store, max_local_gb=1e-9)
    before = _snapshot(store)
    ant_sync.cmd_evict(_args(), {'bucket': 'bk'}, store)
    ant_sync.cmd_plan(_args(), {'bucket': 'bk'}, store)
    assert client.writes == [] and admin_calls == [] and _snapshot(store) == before


def test_library_dry_run_changes_nothing(ant_sync, monkeypatch, tmp_path):
    import config as config_module
    library = tmp_path / 'library'
    (library / 'papers' / 'reading').mkdir(parents=True)
    (library / 'papers' / 'reading' / 'local.pdf').write_bytes(b'%PDF local')
    client = RecordingS3({'_library/papers/reading/remote.pdf': b'%PDF remote',
                          '_library_index/library_chunks.parquet': b'idx'})
    _patch(ant_sync, monkeypatch, client, tmp_path)
    monkeypatch.setattr(config_module, 'get_library_dir', lambda cfg=None: library)
    monkeypatch.setattr(config_module, 'get_library_index_dir', lambda cfg=None: tmp_path / 'index')
    before = _snapshot(library)
    ant_sync._sync_library(_args(no_git=True, propagate_deletes=True), {'bucket': 'bk'}, push=True)
    assert client.writes == []
    assert _snapshot(library) == before
    assert not (tmp_path / 'index').exists()


def test_priority_pull_bundles_sidecars_and_shared_files_never_h5ad(ant_sync, monkeypatch, tmp_path):
    store = _store(tmp_path)
    client = RecordingS3(S3_OBJECTS)
    admin_calls, git_calls = _patch(ant_sync, monkeypatch, client, store)
    ant_sync.cmd_pull(_args(dry_run=False, include_duckdb=True), {'bucket': 'bk'}, store)
    pulled = {call[1] for call in client.writes if call[0] == 'download_file'}
    assert pulled == {
        'G1/a.duckdb', 'G1/a.layer.X.by_gene.parquet', 'G1/a.layer.X.by_cell.parquet', 'G1/a.de.parquet',
        'G1/a.scores.parquet', 'G1/b.duckdb', 'G1/b.layer.X.by_gene.parquet', 'G1/static/supp.xlsx',
        'gene_stats_index.parquet', 'module_variability.parquet', '_facet_tests/G1/a.facet_tests.parquet',
        '_gene_stats_cache/G1/a.parquet', '_module_variability_cache/x.parquet'}
    assert not any(key.endswith('.h5ad') for key in pulled)
    assert (store / 'G1' / 'a.duckdb').read_bytes() == b'duckdb v2 bytes'
    assert int((store / 'G1' / 'a.duckdb').stat().st_mtime) == MTIME
    assert git_calls == [store]
    # the overwritten duckdb was released before and restored after, then a rescan
    paths = [call[1] for call in admin_calls]
    assert any(p.startswith('/admin/release/G1/a') for p in paths)
    assert '/admin/restore/G1/a' in paths and paths[-1] == '/admin/rescan'
    # a second run is a no-op
    client.writes.clear()
    ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    assert client.writes == []


def test_priority_budget_counts_sidecars_and_evicts_them(ant_sync, monkeypatch, tmp_path):
    store = _store(tmp_path)
    for rel in ('G1/a.layer.X.by_gene.parquet', 'G1/a.de.parquet'):
        (store / rel).write_bytes(S3_OBJECTS[rel])
    client = RecordingS3(S3_OBJECTS)
    # cap: room for b (50 + 20 bytes) but not for a's bundle on top
    _patch(ant_sync, monkeypatch, client, store, max_local_gb=80 / 1024 ** 3)
    ant_sync.cmd_pull(_args(dry_run=False, evict=True), {'bucket': 'bk'}, store)
    assert (store / 'G1' / 'b.duckdb').exists() and (store / 'G1' / 'b.layer.X.by_gene.parquet').exists()
    assert not (store / 'G1' / 'a.duckdb').exists()
    assert not (store / 'G1' / 'a.layer.X.by_gene.parquet').exists()
    assert not (store / 'G1' / 'a.de.parquet').exists()
    assert (store / 'G1' / 'gone.duckdb').exists()          # not on S3: never a candidate
    assert (store / 'gene_stats_index.parquet').exists()   # shared files ignore the budget


def test_bundle_owner_needs_the_exact_stem(ant_sync):
    stems = {'G': {'foo', 'foo.v2'}}
    assert ant_sync._bundle_owner('G/foo.v2.layer.X.by_gene.parquet', stems) == 'G/foo.v2.duckdb'
    assert ant_sync._bundle_owner('G/foo.layer.counts.by_cell.parquet', stems) == 'G/foo.duckdb'
    assert ant_sync._bundle_owner('G/foo.de.parquet', stems) == 'G/foo.duckdb'
    assert ant_sync._bundle_owner('G/foo.v2.scores.parquet', stems) == 'G/foo.v2.duckdb'
    assert ant_sync._bundle_owner('G/foobar.de.parquet', stems) is None
    assert ant_sync._bundle_owner('G/foo.h5ad', stems) is None
    # review C5: a sidecar of another dataset (no foo.sub.duckdb) is never given to foo
    assert ant_sync._bundle_owner('G/foo.sub.de.parquet', stems) is None
    split = ant_sync._split_priority_index({'G/foo.duckdb': (1, 1), 'G/foo.sub.de.parquet': (1, 1)})
    assert split[1]['G/foo.duckdb'] == {}


# ── security review of 2.46.1 ────────────────────────────────────────────────

def test_primary_priority_never_evicts_or_overwrites_sidecars_and_shared_files(ant_sync, monkeypatch, tmp_path):
    """S2: on a primary the local scores / DE / fleet indexes / caches may be work not pushed yet."""
    store = _store(tmp_path)
    (store / 'G1' / 'a.scores.parquet').write_bytes(b'LOCAL NEWER SCORES, NOT PUSHED')
    (store / 'module_variability.parquet').write_bytes(b'LOCAL REBUILT, NOT PUSHED')
    (store / 'gene_stats_index.parquet').write_bytes(b'gsi')       # same size as S3, older: would be re-stamped
    os.utime(store / 'gene_stats_index.parquet', (MTIME - 50, MTIME - 50))
    client = RecordingS3(S3_OBJECTS)
    _patch(ant_sync, monkeypatch, client, store)
    monkeypatch.setattr(ant_sync, 'is_slave', lambda cfg=None: False)
    ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    assert (store / 'G1' / 'a.scores.parquet').read_bytes() == b'LOCAL NEWER SCORES, NOT PUSHED'
    assert (store / 'module_variability.parquet').read_bytes() == b'LOCAL REBUILT, NOT PUSHED'
    assert int((store / 'gene_stats_index.parquet').stat().st_mtime) == MTIME - 50
    pulled = {call[1] for call in client.writes if call[0] == 'download_file'}
    assert '_facet_tests/G1/a.facet_tests.parquet' in pulled and 'G1/b.duckdb' in pulled      # missing: fetched
    assert 'G1/a.scores.parquet' not in pulled and 'module_variability.parquet' not in pulled
    # dataset a has a kept local sidecar: its local version stays whole (no new duckdb, no new
    # sidecar next to layers of another version)
    assert 'G1/a.duckdb' not in pulled and 'G1/a.de.parquet' not in pulled
    assert (store / 'G1' / 'a.duckdb').read_bytes() == b'old'
    # evict on a primary: the duckdb goes as before, its sidecars stay
    client.writes.clear()
    _patch(ant_sync, monkeypatch, client, store, max_local_gb=80 / 1024 ** 3)
    monkeypatch.setattr(ant_sync, 'is_slave', lambda cfg=None: False)
    ant_sync.cmd_pull(_args(dry_run=False, evict=True), {'bucket': 'bk'}, store)
    assert not (store / 'G1' / 'a.duckdb').exists()
    assert (store / 'G1' / 'a.scores.parquet').read_bytes() == b'LOCAL NEWER SCORES, NOT PUSHED'


def test_primary_budget_never_counts_kept_sidecars_as_freed(ant_sync, tmp_path):
    """Re-review 2: a primary never evicts sidecars, so their bytes are not in the budget."""
    store = tmp_path / 'store'
    (store / 'G').mkdir(parents=True)
    (store / 'G' / 'a.duckdb').write_bytes(b'd' * 10)
    (store / 'G' / 'a.layer.X.by_gene.parquet').write_bytes(b'L' * 1000)     # big local sidecar
    s3_meta = {'G/a.duckdb': (10, MTIME), 'G/b.duckdb': (500, MTIME)}
    bundles = {'G/a.duckdb': {'G/a.layer.X.by_gene.parquet': (1000, MTIME)}, 'G/b.duckdb': {}}
    policy = {'mode': 'priority', 'max_local_bytes': None, 'min_free_bytes': 100, 'priority_default': 50}
    priorities = {'G/b.duckdb': 90, 'G/a.duckdb': 10}
    slave_rows, slave_base = ant_sync._build_priority_plan(s3_meta, priorities, store, policy, 200, bundles)
    primary_rows, primary_base = ant_sync._build_priority_plan(s3_meta, priorities, store, policy, 200, bundles,
                                                               primary=True)
    assert slave_base == 200 + 10 + 1000 and primary_base == 200 + 10
    # on the slave evicting a's 1 KB layer makes room for b; on the primary it does not
    assert [r['status'] for r in slave_rows] == ['PULL', 'EVICT']
    assert slave_rows[1]['sidecars'][0][3] == 'EVICT'
    assert primary_rows[0]['status'] == 'SKIP' and primary_rows[0]['projected_free'] >= 100
    assert primary_rows[1]['sidecars'][0][3] == 'KEEP'


def test_min_free_is_checked_before_every_pull_even_with_evict(ant_sync, monkeypatch, tmp_path):
    store = _store(tmp_path)
    client = RecordingS3(S3_OBJECTS)
    _patch(ant_sync, monkeypatch, client, store)
    config = ant_sync.get_config()
    config['sync_policy']['min_free_space'] = 1                       # GB
    monkeypatch.setattr(ant_sync, '_free_space', lambda path: 10 ** 12 if not client.writes else 10)
    ant_sync.cmd_pull(_args(dry_run=False, evict=True), {'bucket': 'bk'}, store)
    # the plan saw plenty of room; once the disk is "full" every later file is skipped
    assert len([c for c in client.writes if c[0] == 'download_file']) == 1


@pytest.mark.parametrize('mode', ['duckdb', 'priority'])
def test_a_failed_download_restores_the_released_duckdb_and_rescans(ant_sync, monkeypatch, tmp_path, mode):
    """C2: release/restore always pair up."""
    store = _store(tmp_path)
    client = RecordingS3(S3_OBJECTS)
    admin_calls, _git = _patch(ant_sync, monkeypatch, client, store, mode=mode)

    def failing_download(bucket, key, filename, **_):
        if key == 'G1/a.duckdb':
            raise OSError('network')
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        Path(filename).write_bytes(S3_OBJECTS[key])
    client.download_file = failing_download
    with pytest.raises(OSError):
        ant_sync.cmd_pull(_args(dry_run=False), {'bucket': 'bk'}, store)
    paths = [call[1] for call in admin_calls]
    release = next(i for i, p in enumerate(paths) if p.startswith('/admin/release/G1/a'))
    assert '/admin/restore/G1/a' in paths[release:]
    assert paths[-1] == '/admin/rescan'


def test_container_only_api_url_is_warned_on_the_host(ant_sync, monkeypatch, capsys):
    monkeypatch.delenv('ANTHIVE_API_URL', raising=False)
    monkeypatch.setitem(ant_sync._API_URL_WARNED, 'done', False)
    assert ant_sync._ant_serve_url({'api_url': 'http://api:8080'}) == 'http://api:8080'
    assert 'resolves only inside the container network' in capsys.readouterr().err
    monkeypatch.setitem(ant_sync._API_URL_WARNED, 'done', False)
    assert ant_sync._ant_serve_url({'rest_api_port': 9000}) == 'http://localhost:9000'
    assert capsys.readouterr().err == ''
