"""Cache freshness of ``bin/annot/ant-gene-stats-precompute`` follows the expression source only.

Before 2026-10-13 a cache was stale as soon as the dataset's ``.md`` or ``.duckdb`` was newer than it, so an
``.md`` edit or an obs-only duckdb rewrite stamped 141 of 143 datasets stale in ``gene_stats_index.parquet``
and the low-detection pill was computed from the two that were left.
"""
import importlib.machinery
import importlib.util
import os
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip('tqdm')
pytest.importorskip('pyarrow')

SCRIPT = Path(__file__).resolve().parent.parent / 'bin' / 'annot' / 'ant-gene-stats-precompute'


def _load():
    loader = importlib.machinery.SourceFileLoader('ant_gene_stats_precompute', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module   # dataclasses look the module up while the script executes
    loader.exec_module(module)
    return module


precompute = _load()


def _touch(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _dataset(tmp_path: Path, layer: str = 'X', with_parquet: bool = True):
    duckdb_path = tmp_path / 'ds.duckdb'
    duckdb_path.write_bytes(b'duckdb')
    md_path = tmp_path / 'ds.md'
    md_path.write_text(f'---\ndefault_layer: {layer}\n---\n')
    by_gene = tmp_path / f'ds.layer.{layer}.by_gene.parquet'
    if with_parquet:
        by_gene.write_bytes(b'parquet-bytes')
    source = by_gene if with_parquet else duckdb_path
    return precompute.DatasetInputs(
        db_id='G/ds', duckdb_path=duckdb_path, md_path=md_path, layer=layer, by_gene_parquet=by_gene,
        source_mtime=source.stat().st_mtime, expression_source=source.resolve(),
        fingerprint=precompute.build_fingerprint(tmp_path, [source]))


def _rows(layer: str = 'X'):
    return [{'dataset': 'G/ds', 'gene': 'APOE', 'layer': layer, 'n_cells': 10, 'n_nonzero': 5, 'pct_nonzero': 50.0}]


def test_md_edit_and_obs_only_duckdb_rewrite_keep_the_cache_fresh(tmp_path):
    inputs = _dataset(tmp_path)
    old = time.time() - 1000
    _touch(inputs.expression_source, old)
    cache = tmp_path / 'cache' / 'genestats.parquet'
    precompute._write_cache(cache, _rows(), inputs)
    _touch(cache, old + 10)
    _touch(inputs.md_path, old + 500)            # .md edit after the cache
    inputs.duckdb_path.write_bytes(b'duckdb with a new obs column')
    assert precompute.cache_is_fresh(cache, inputs)


def test_changed_layer_value_or_rewritten_source_is_stale(tmp_path):
    inputs = _dataset(tmp_path)
    cache = tmp_path / 'cache' / 'genestats.parquet'
    precompute._write_cache(cache, _rows(), inputs)
    assert precompute.cache_is_fresh(cache, inputs)
    other_layer = precompute.DatasetInputs(**{**inputs.__dict__, 'layer': 'counts'})
    assert not precompute.cache_is_fresh(cache, other_layer)
    inputs.expression_source.write_bytes(b'a re-ingested layer, other size')
    assert not precompute.cache_is_fresh(cache, inputs)


def test_pulled_source_with_new_mtime_but_older_than_cache_is_fresh(tmp_path):
    inputs = _dataset(tmp_path)
    cache = tmp_path / 'cache' / 'genestats.parquet'
    precompute._write_cache(cache, _rows(), inputs)
    _touch(inputs.expression_source, time.time() - 5000)   # ant-sync pull resets mtimes, size unchanged
    assert precompute.cache_is_fresh(cache, inputs)
    _touch(inputs.expression_source, time.time() + 5000)   # same size but rewritten after the cache
    assert not precompute.cache_is_fresh(cache, inputs)


def test_cache_without_metadata_uses_the_conservative_rule(tmp_path):
    inputs = _dataset(tmp_path)
    cache = tmp_path / 'cache' / 'genestats.parquet'
    cache.parent.mkdir(parents=True)
    pd.DataFrame(_rows()).to_parquet(cache)              # a cache written before the metadata existed
    assert precompute._read_cache_meta(cache) is None
    now = time.time()
    _touch(cache, now)
    _touch(inputs.expression_source, now - 100)
    _touch(inputs.md_path, now + 100)                     # .md newer than the cache: does not matter
    assert precompute.cache_is_fresh(cache, inputs)
    other_layer = precompute.DatasetInputs(**{**inputs.__dict__, 'layer': 'counts'})
    assert not precompute.cache_is_fresh(cache, other_layer)  # rows say layer X
    _touch(inputs.expression_source, now + 100)            # expression source newer than the cache
    assert not precompute.cache_is_fresh(cache, inputs)


def test_duckdb_is_the_source_without_a_by_gene_parquet(tmp_path):
    inputs = _dataset(tmp_path, with_parquet=False)
    cache = tmp_path / 'cache' / 'genestats.parquet'
    precompute._write_cache(cache, _rows(), inputs)
    assert precompute.cache_is_fresh(cache, inputs)
    inputs.duckdb_path.write_bytes(b'rewritten duckdb')    # the duckdb holds the layer here, so it counts
    assert not precompute.cache_is_fresh(cache, inputs)


def test_missing_cache_is_not_fresh(tmp_path):
    assert not precompute.cache_is_fresh(tmp_path / 'none.parquet', _dataset(tmp_path))


def test_aggregate_stamps_only_truly_stale_caches(tmp_path):
    root = tmp_path
    inputs = _dataset(tmp_path)
    cache = precompute._cache_file(root, inputs.db_id)
    precompute._write_cache(cache, _rows(), inputs)
    _touch(inputs.md_path, time.time() + 100)
    [(path, db_id, fingerprint)] = precompute._cache_fingerprints(root, [inputs])
    assert db_id == 'G/ds' and fingerprint != precompute.STALE_FINGERPRINT
