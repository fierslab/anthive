"""The persisted metadata cache must notice .md-only, .h5ad.mus, .env and index.md edits."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mus  # noqa: E402
from test_connection_pool import _load_serve_parts  # noqa: E402

DB_ID = 'Grp/ds'


@pytest.fixture
def store(tmp_path):
    """A one-dataset store plus a registry whose loader reads the .md like ant-serve."""
    serve = _load_serve_parts(mus=mus)
    data_folder = tmp_path / 'store'
    folder = data_folder / 'Grp'
    folder.mkdir(parents=True)
    (folder / 'ds.duckdb').write_bytes(b'')
    md_path = folder / 'ds.md'
    md_path.write_text('replicate_unit: none\n')
    (folder / 'index.md').write_text('title: one\n')

    registry = serve.DatabaseRegistry.__new__(serve.DatabaseRegistry)
    registry.data_folder = data_folder
    registry.databases = {DB_ID: str(folder / 'ds.duckdb')}
    registry._db_md_bases = {DB_ID: md_path}
    registry._db_extra = {DB_ID: {'rel_path': 'Grp/ds.duckdb'}}
    registry.metadata_cache = {}
    registry._databases_list_cache = None
    registry._cache_file_path = lambda: tmp_path / 'cache.json'
    registry._load_database_metadata = lambda db_id, extra: {
        'replicate_unit': md_path.read_text().strip(),
        'env': (folder / '.env').read_text() if (folder / '.env').exists() else '',
        'index': (folder / 'index.md').read_text(),
        'mus': (folder / 'ds.h5ad.mus').read_text() if (folder / 'ds.h5ad.mus').exists() else '',
    }
    registry.folder = folder
    return registry


def _restart(registry):
    """Simulate a restart: drop in-memory state, keep the on-disk cache."""
    registry.metadata_cache = {}
    registry._cache_all_metadata()
    return registry.metadata_cache[DB_ID]


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def test_unchanged_files_are_served_from_cache(store):
    store._cache_all_metadata()
    store._load_database_metadata = lambda *a: pytest.fail('cache should have been used')
    assert _restart(store)['replicate_unit'] == 'replicate_unit: none'


def test_md_only_change_is_seen_after_restart(store):
    store._cache_all_metadata()
    (store.folder / 'ds.md').write_text('replicate_unit: donor\n')
    _bump_mtime(store.folder / 'ds.md')
    assert _restart(store)['replicate_unit'] == 'replicate_unit: donor'


@pytest.mark.parametrize('name, key, content', [
    ('ds.h5ad.mus', 'mus', 'organism: human\n'),
    ('.env', 'env', 'tissue: brain\n'),
    ('index.md', 'index', 'title: two\n'),
])
def test_annotation_inputs_invalidate(store, name, key, content):
    store._cache_all_metadata()
    target = store.folder / name
    existed = target.exists()
    target.write_text(content)
    if existed:
        _bump_mtime(target)
    assert _restart(store)[key] == content


def test_env_in_data_folder_root_invalidates(store):
    store._cache_all_metadata()
    (store.data_folder / '.env').write_text('x: y\n')
    store._load_database_metadata = lambda *a: {'reloaded': True}
    assert _restart(store) == {'reloaded': True}


def test_entry_from_older_cache_without_signature_is_invalid(store):
    fingerprint = store._get_entry_fingerprint(DB_ID)
    entry = {key: value for key, value in fingerprint.items() if key != 'annotation_sig'}
    assert not store._is_cache_entry_valid(DB_ID, entry, fingerprint)
