"""Tests for ant-ingest's DE-deposit scanning and target-h5ad resolution.

The bin/ingest/ant-ingest script is a PEP-723 uv-script; we import it as
a module via the import-machinery trick so we can exercise the unit-level
functions without the S3 / subprocess plumbing.

Covers:
  * _group_into_de_plans  — recognises mus 0.2.24 deposit layout
  * _resolve_target_h5ad  — resolves de_dataset_id and de_dataset_h5ad
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANT_INGEST = _REPO_ROOT / 'bin' / 'ingest' / 'ant-ingest'


def _import_ant_ingest():
    """Import bin/ingest/ant-ingest as a module under the name 'ant_ingest'.

    Two snags with this file specifically:
      1. It has no .py extension — spec_from_file_location returns None,
         so we attach an explicit SourceFileLoader.
      2. It defines @dataclass classes, and the dataclass decorator looks
         up sys.modules[cls.__module__] for InitVar resolution. We
         register the module in sys.modules BEFORE exec_module runs.
    """
    loader = importlib.machinery.SourceFileLoader('ant_ingest', str(_ANT_INGEST))
    spec = importlib.util.spec_from_loader('ant_ingest', loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['ant_ingest'] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def ant_ingest():
    return _import_ant_ingest()


# ─── _group_into_de_plans ─────────────────────────────────────────────


def _obj(key: str, size: int = 100) -> dict:
    return {'Key': key, 'Size': size}


def test_de_scanner_single_deposit(ant_ingest):
    """One DE deposit: __incoming/<dp>/de/<lp>/<file>.tsv + .mus."""
    objs = [
        _obj('__incoming/Sierksma2025/de/main/DAM_vs_homeo.tsv'),
        _obj('__incoming/Sierksma2025/de/main/DAM_vs_homeo.tsv.mus'),
    ]
    plans, malformed = ant_ingest._group_into_de_plans(objs)
    assert len(plans) == 1
    assert len(malformed) == 0
    p = plans[0]
    assert p.data_project == 'Sierksma2025'
    assert p.local_parent == 'main'
    assert p.basename == 'DAM_vs_homeo.tsv'
    assert p.incoming_data_key == '__incoming/Sierksma2025/de/main/DAM_vs_homeo.tsv'
    assert p.incoming_mus_key == '__incoming/Sierksma2025/de/main/DAM_vs_homeo.tsv.mus'


def test_de_scanner_missing_sidecar_is_malformed(ant_ingest):
    """A DE data file without its .mus companion → malformed, NOT a plan."""
    objs = [
        _obj('__incoming/Sierksma2025/de/main/DAM_vs_homeo.tsv'),
        # No .mus
    ]
    plans, malformed = ant_ingest._group_into_de_plans(objs)
    assert plans == []
    assert len(malformed) == 1
    assert 'missing companion .mus sidecar' in malformed[0]


def test_de_scanner_parquet_rejected(ant_ingest):
    """Parquet is rejected at the gate per mus contract — even with a sidecar."""
    objs = [
        _obj('__incoming/X/de/m/foo.parquet'),
        _obj('__incoming/X/de/m/foo.parquet.mus'),
    ]
    plans, malformed = ant_ingest._group_into_de_plans(objs)
    assert plans == []
    assert any('parquet not accepted' in m for m in malformed)


def test_de_scanner_multiple_files_per_parent(ant_ingest):
    """One folder = many contrasts (per-cluster DE) → multiple plans."""
    objs = [
        _obj('__incoming/X/de/per_cluster/DAM.tsv'),
        _obj('__incoming/X/de/per_cluster/DAM.tsv.mus'),
        _obj('__incoming/X/de/per_cluster/ARM.tsv'),
        _obj('__incoming/X/de/per_cluster/ARM.tsv.mus'),
        _obj('__incoming/X/de/per_cluster/Hom.tsv'),
        _obj('__incoming/X/de/per_cluster/Hom.tsv.mus'),
    ]
    plans, malformed = ant_ingest._group_into_de_plans(objs)
    assert len(plans) == 3
    assert len(malformed) == 0
    bases = sorted(p.basename for p in plans)
    assert bases == ['ARM.tsv', 'DAM.tsv', 'Hom.tsv']


def test_de_scanner_csv_and_gz_accepted(ant_ingest):
    """All 4 extensions in _DE_DATA_EXTS are accepted."""
    objs = []
    for ext in ('.tsv', '.csv', '.tsv.gz', '.csv.gz'):
        objs.append(_obj(f'__incoming/X/de/m/f{ext}'))
        objs.append(_obj(f'__incoming/X/de/m/f{ext}.mus'))
    plans, malformed = ant_ingest._group_into_de_plans(objs)
    assert len(plans) == 4
    assert len(malformed) == 0


def test_de_scanner_ignores_non_de_paths(ant_ingest):
    """h5ad files (or any path not under /de/) → not picked up as DE."""
    objs = [
        _obj('__incoming/X/some.h5ad'),                  # plain h5ad
        _obj('__incoming/X/some.h5ad.mus'),
        _obj('__incoming/X/static/figure.png'),          # static asset
        _obj('__incoming/X/de/main/real.tsv'),           # the only DE
        _obj('__incoming/X/de/main/real.tsv.mus'),
    ]
    plans, malformed = ant_ingest._group_into_de_plans(objs)
    assert len(plans) == 1
    assert plans[0].basename == 'real.tsv'
    assert malformed == []


def test_de_scanner_unknown_extension(ant_ingest):
    """A file under de/ with an unknown extension → malformed."""
    objs = [
        _obj('__incoming/X/de/m/notes.txt'),
        _obj('__incoming/X/de/m/notes.txt.mus'),
    ]
    plans, malformed = ant_ingest._group_into_de_plans(objs)
    assert plans == []
    assert any('unrecognized extension' in m for m in malformed)


# ─── _resolve_target_h5ad ─────────────────────────────────────────────


def _make_catalog(root: Path, project: str, basename: str,
                  *, id_code: str, nested: bool = False) -> Path:
    """Create a catalog dataset (h5ad + .md with id) under root/project."""
    if nested:
        ds_dir = root / project / basename
        ds_dir.mkdir(parents=True)
        md = ds_dir / f'{basename}.md'
        h5 = ds_dir / f'{basename}.h5ad'
    else:
        (root / project).mkdir(parents=True, exist_ok=True)
        md = root / project / f'{basename}.md'
        h5 = root / project / f'{basename}.h5ad'
    md.write_text(
        '---\n'
        f'title: {basename}\n'
        f'id: {id_code}\n'
        '---\n'
    )
    h5.write_bytes(b'fake h5ad')
    return h5


def test_resolve_by_dataset_id_flat(ant_ingest, tmp_path):
    h5 = _make_catalog(tmp_path, 'Preman2024', 'Microglia', id_code='MIC')
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'Preman2024', de_dataset_id='MIC', de_dataset_h5ad=None,
    )
    assert path == h5
    assert 'MIC' in reason


def test_resolve_by_dataset_id_nested(ant_ingest, tmp_path):
    h5 = _make_catalog(tmp_path, 'Sierksma2025', 'Single.Grafts',
                       id_code='SGM', nested=True)
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'Sierksma2025', de_dataset_id='SGM', de_dataset_h5ad=None,
    )
    assert path == h5
    assert 'SGM' in reason


def test_resolve_by_dataset_id_unknown(ant_ingest, tmp_path):
    _make_catalog(tmp_path, 'X', 'foo', id_code='FOO')
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'X', de_dataset_id='BAR', de_dataset_h5ad=None,
    )
    assert path is None
    assert "no .md frontmatter `id:` matches" in reason


def test_resolve_by_dataset_h5ad_flat(ant_ingest, tmp_path):
    h5 = _make_catalog(tmp_path, 'X', 'foo', id_code='FOO')
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'X', de_dataset_id=None,
        de_dataset_h5ad='foo.h5ad',
    )
    assert path == h5
    assert 'foo.h5ad' in reason


def test_resolve_by_dataset_h5ad_nested(ant_ingest, tmp_path):
    h5 = _make_catalog(tmp_path, 'X', 'foo', id_code='FOO', nested=True)
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'X', de_dataset_id=None,
        de_dataset_h5ad='foo.h5ad',
    )
    assert path == h5
    assert 'foo' in reason  # mentions the sub-folder match


def test_resolve_by_dataset_h5ad_unknown(ant_ingest, tmp_path):
    _make_catalog(tmp_path, 'X', 'foo', id_code='FOO')
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'X', de_dataset_id=None,
        de_dataset_h5ad='bar.h5ad',
    )
    assert path is None
    assert 'not found' in reason


def test_resolve_missing_data_project(ant_ingest, tmp_path):
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'DoesNotExist', de_dataset_id='X', de_dataset_h5ad=None,
    )
    assert path is None
    assert 'data_project folder not on disk' in reason


def test_resolve_neither_key_set(ant_ingest, tmp_path):
    _make_catalog(tmp_path, 'X', 'foo', id_code='FOO')
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'X', de_dataset_id=None, de_dataset_h5ad=None,
    )
    assert path is None
    assert 'neither de_dataset_id nor de_dataset_h5ad' in reason


def test_resolve_prefers_dataset_id_when_both_set(ant_ingest, tmp_path):
    """When both keys are present, de_dataset_id wins (the 3-char code
    is stable across roundtrip renames; the basename is mutable)."""
    h5 = _make_catalog(tmp_path, 'X', 'authoritative', id_code='AUT')
    _make_catalog(tmp_path, 'X', 'other', id_code='OTH')
    path, reason = ant_ingest._resolve_target_h5ad(
        tmp_path, 'X',
        de_dataset_id='AUT',
        de_dataset_h5ad='other.h5ad',
    )
    assert path == h5
    assert 'AUT' in reason
