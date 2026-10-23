"""2.47.2: ``ant-sync pull`` never fetches the git-tracked files of the artefact store (``artefact.md`` and
``v<N>/manifest.json`` under ``<Group>/static/artefacts/``). On the production host a pull left them as
untracked files, which then blocked ``git pull --ff-only`` of a read-only h5adstore clone.
"""
from __future__ import annotations

import pytest

import artefact_sync
from tests.test_ant_sync_catalogue import _catalogued_store, _pulled
from tests.test_ant_sync_dry_run import RecordingS3, _args, _patch, ant_sync  # noqa: F401

ARTEFACT_BASE = 'G1/static/artefacts/SLM/figure/volcano'
RECORDS = {f'{ARTEFACT_BASE}/artefact.md': b'---\nid: volcano\n---\n',
           f'{ARTEFACT_BASE}/v1/manifest.json': b'{"schema": "anthive.artefact/1"}',
           'G1/static/artefacts/_group/table/atlas/v12/manifest.json': b'{}'}
PAYLOAD = {f'{ARTEFACT_BASE}/v1/files/volcano.png': b'png bytes',
           f'{ARTEFACT_BASE}/v1/preview/thumb.png': b'thumb'}
OBJECTS = {'G1/a.h5ad': b'a', 'G1/a.duckdb': b'a duckdb', 'G1/static/supp.xlsx': b'supp', **RECORDS, **PAYLOAD}


@pytest.mark.parametrize('mode,flags', [('priority', dict()), ('priority', dict(include_duckdb=True, evict=True)),
                                        ('priority', dict(filter='G1/static/*')), ('all', dict()),
                                        ('all', dict(include_duckdb=True)), ('all', dict(filter='G1/static/*')),
                                        ('duckdb', dict())])
def test_pull_never_downloads_head_records_or_manifests(ant_sync, monkeypatch, tmp_path, mode, flags):
    store = _catalogued_store(tmp_path)
    client = RecordingS3(OBJECTS)
    _patch(ant_sync, monkeypatch, client, store, mode=mode, catalogued=None)
    ant_sync.cmd_pull(_args(dry_run=False, no_artefacts=True, **flags), {'bucket': 'bk'}, store)
    pulled = _pulled(client)
    assert not pulled & set(RECORDS)
    assert not any(rel.startswith('G1/static/artefacts/') for rel in pulled)
    assert not list(store.rglob('artefact.md')) and not list(store.rglob('manifest.json'))


@pytest.mark.parametrize('rel,expected', [
    (f'{ARTEFACT_BASE}/artefact.md', True),
    (f'{ARTEFACT_BASE}/v1/manifest.json', True),
    ('G1/static/artefacts/_group/table/atlas/v12/manifest.json', True),
    (f'{ARTEFACT_BASE}/v1/files/volcano.png', False),
    (f'{ARTEFACT_BASE}/v1/files/manifest.json', False),
    (f'{ARTEFACT_BASE}/manifest.json', False),
    (f'{ARTEFACT_BASE}/vx/manifest.json', False),
])
def test_git_tracked_record_names(rel, expected):
    assert artefact_sync.is_git_tracked_record(rel) is expected


def test_apply_refuses_to_download_a_record(tmp_path):
    store = tmp_path / 'store'
    store.mkdir()
    rel = f'{ARTEFACT_BASE}/artefact.md'
    client = RecordingS3({rel: b'x'})
    action = artefact_sync.Action('download', rel, rel, 1, 'sha')
    messages: list = []
    counts = artefact_sync.apply([action], store, client, 'bk', log=messages.append)
    assert counts['failed'] == 1 and counts['downloaded'] == 0
    assert client.writes == [] and not (store / rel).exists()
    assert any('come from the h5adstore git repository' in message for message in messages)
