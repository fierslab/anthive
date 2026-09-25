"""Freshness gating, atomic reload and locked upserts for the fleet indexes.

Covers ``lib/index_freshness.py``, ``lib/gene_stats_index.py``,
``lib/gene_variance_index.py`` and ``lib/scores_parquet.upsert_scores``.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import sys
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import index_freshness  # noqa: E402
from gene_stats_index import GeneStatsIndex  # noqa: E402
from gene_variance_index import GeneVarianceIndex  # noqa: E402
from index_freshness import (  # noqa: E402
    FINGERPRINT_COLUMN, STALE_FINGERPRINT, build_fingerprint,
)


@pytest.fixture(autouse=True)
def no_verdict_cache(monkeypatch):
    """Re-stat on every lookup so tests see file changes immediately."""
    monkeypatch.setattr(index_freshness, 'DEFAULT_TTL_SECONDS', 0.0)


def _make_source(root: Path, rel: str, payload: bytes = b'x' * 100) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _age(path: Path, seconds: float) -> None:
    """Shift a file's mtime ``seconds`` into the past."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns - int(seconds * 1e9)))


def _stats_frame(dataset_genes: dict, fingerprints: dict | None) -> pd.DataFrame:
    rows = []
    for dataset, genes in dataset_genes.items():
        for rank, gene in enumerate(genes):
            rows.append({
                'dataset': dataset, 'gene': gene, 'layer': 'X',
                'n_cells': 100, 'n_nonzero': 10 + rank, 'pct_nonzero': 0.1,
                'mean': 1.0 + rank, 'std': 0.5, 'cv': 0.5,
                'mean_nonzero': 2.0, 'std_nonzero': 0.1, 'median_nonzero': 2.0,
                'q10_nonzero': 1.0, 'q90_nonzero': 3.0, 'max': 4.0,
                'mean_expr_layer': 1.0, 'rank_mean': 0.5, 'scaled_mean': 0.5,
            })
    df = pd.DataFrame(rows)
    if fingerprints is not None:
        df[FINGERPRINT_COLUMN] = df['dataset'].map(fingerprints)
    return df


def _write_index(df: pd.DataFrame, path: Path) -> None:
    df.to_parquet(path, index=False)


# ---------------------------------------------------------------- freshness


class TestGeneStatsFreshness:

    def _setup(self, tmp_path: Path):
        source_a = _make_source(tmp_path, 'G/a.layer.X.by_gene.parquet')
        source_b = _make_source(tmp_path, 'G/b.duckdb')
        _age(source_a, 60)
        _age(source_b, 60)
        fingerprints = {
            'G/a': build_fingerprint(tmp_path, [source_a]),
            'G/b': build_fingerprint(tmp_path, [source_b]),
        }
        index_path = tmp_path / 'gene_stats_index.parquet'
        _write_index(
            _stats_frame({'G/a': ['APOE', 'TREM2'], 'G/b': ['APOE']}, fingerprints),
            index_path,
        )
        index = GeneStatsIndex(index_path)
        index.load()
        return index, source_a, source_b

    def test_fresh_hit_and_column_dropped(self, tmp_path):
        index, _, _ = self._setup(tmp_path)
        hit = index.lookup('G/a', 'apoe')
        assert hit is not None and hit['gene_found'] == 'APOE'
        assert FINGERPRINT_COLUMN not in index._state.df.columns
        assert index.stats()['has_fingerprints'] is True
        assert index.stats()['stale_datasets'] == []

    def test_size_change_is_stale(self, tmp_path):
        index, source_a, _ = self._setup(tmp_path)
        source_a.write_bytes(b'y' * 200)
        assert index.lookup('G/a', 'APOE') is None
        assert index.lookup('G/b', 'APOE') is not None
        assert index.stats()['stale_datasets'] == ['G/a']

    def test_newer_mtime_same_size_is_stale(self, tmp_path):
        index, source_a, _ = self._setup(tmp_path)
        source_a.write_bytes(b'z' * 100)   # same size, mtime = now > index mtime
        assert index.lookup('G/a', 'APOE') is None

    def test_pulled_host_mtime_older_than_index_is_fresh(self, tmp_path):
        # ant-sync pull rewrites mtimes to the upload time: they no longer
        # match the recorded value but stay older than the (later) index.
        index, source_a, _ = self._setup(tmp_path)
        _age(source_a, 5)
        assert index.lookup('G/a', 'APOE') is not None

    def test_missing_source_is_stale(self, tmp_path):
        index, source_a, _ = self._setup(tmp_path)
        source_a.unlink()
        assert index.lookup('G/a', 'APOE') is None

    def test_stale_sentinel_never_served(self, tmp_path):
        index_path = tmp_path / 'gene_stats_index.parquet'
        _write_index(
            _stats_frame({'G/a': ['APOE']}, {'G/a': STALE_FINGERPRINT}), index_path,
        )
        index = GeneStatsIndex(index_path)
        index.load()
        assert index.lookup('G/a', 'APOE') is None

    def test_legacy_parquet_without_column_is_fresh(self, tmp_path, caplog):
        index_path = tmp_path / 'gene_stats_index.parquet'
        _write_index(_stats_frame({'G/a': ['APOE']}, None), index_path)
        index = GeneStatsIndex(index_path)
        with caplog.at_level(logging.WARNING, logger='index_freshness'):
            index.load()
        assert index.lookup('G/a', 'APOE') is not None
        assert index.stats()['has_fingerprints'] is False
        assert any('freshness gating disabled' in rec.message for rec in caplog.records)

    def test_verdict_cached_within_ttl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(index_freshness, 'DEFAULT_TTL_SECONDS', 3600.0)
        index, source_a, _ = self._setup(tmp_path)
        assert index.lookup('G/a', 'APOE') is not None
        source_a.write_bytes(b'y' * 200)
        assert index.lookup('G/a', 'APOE') is not None   # cached verdict
        index.load()                                     # reload resets it
        assert index.lookup('G/a', 'APOE') is None


# ---------------------------------------------------------------- variance


def _variance_frame(fingerprint: str | None) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for dataset in ('G/a', 'G/b'):
        for facet in ('ant_disease', 'ant_sex'):
            for gene_idx in range(30):
                signal = float(rng.choice([0.1, 0.2, 0.3, np.nan]))  # many ties
                rows.append({
                    'dataset': dataset, 'gene': f'G{gene_idx}', 'facet': facet,
                    'facet_type': 'categorical', 'layer': 'X',
                    'n_cells': 100, 'n_groups': 2,
                    'eta_squared': signal, 'F_stat': 1.0, 'signal': signal,
                    'total_var': 1.0,
                    'group_means_json': '{"AD": 1.0, "control": 0.5}',
                })
    df = pd.DataFrame(rows).sample(frac=1.0, random_state=3).reset_index(drop=True)
    if fingerprint is not None:
        df[FINGERPRINT_COLUMN] = fingerprint
    return df


def _old_top_by_facet(df: pd.DataFrame, dataset: str, facet: str, top: int):
    """Reference: the pre-refactor per-pair mask + stable sort."""
    mask = (df['dataset'] == dataset) & (df['facet'] == facet)
    sub = df.loc[mask].sort_values(
        'signal', ascending=False, na_position='last', kind='mergesort',
    )
    return list(sub.head(top)['gene'])


class TestGeneVarianceIndex:

    def test_top_order_matches_old_implementation(self, tmp_path):
        df = _variance_frame(None)
        index_path = tmp_path / 'gene_variance_index.parquet'
        df.to_parquet(index_path, index=False)
        index = GeneVarianceIndex(index_path)
        index.load()
        for dataset in ('G/a', 'G/b'):
            for facet in ('ant_disease', 'ant_sex'):
                got = [entry['gene'] for entry in index.top_by_facet(dataset, facet, 25)]
                assert got == _old_top_by_facet(df, dataset, facet, 25)
        assert index.top_by_facet('G/a', 'ant_missing', 5) is None
        assert index.has_pair('G/a', 'ant_sex')
        assert not index.has_pair('G/zzz', 'ant_sex')

    def test_entry_shape_and_nan(self, tmp_path):
        df = _variance_frame(None)
        index_path = tmp_path / 'gene_variance_index.parquet'
        df.to_parquet(index_path, index=False)
        index = GeneVarianceIndex(index_path)
        index.load()
        entries = index.top_by_facet('G/a', 'ant_disease', 1000)
        assert set(entries[0]) == {
            'gene', 'signal', 'eta_squared', 'F_stat', 'total_var', 'group_means',
        }
        assert entries[0]['group_means'] == {'AD': 1.0, 'control': 0.5}
        assert entries[-1]['signal'] is None       # NaN sorted last, as null
        meta = index.facet_metadata('G/a', 'ant_disease')
        assert meta['groups'] == ['AD', 'control'] and meta['n_groups'] == 2

    def test_stale_dataset_misses_everywhere(self, tmp_path):
        duckdb_file = _make_source(tmp_path, 'G/a.duckdb')
        _age(duckdb_file, 60)
        df = _variance_frame(build_fingerprint(tmp_path, [duckdb_file]))
        index_path = tmp_path / 'gene_variance_index.parquet'
        df.to_parquet(index_path, index=False)
        index = GeneVarianceIndex(index_path)
        index.load()
        assert index.has_pair('G/a', 'ant_sex')
        duckdb_file.write_bytes(b'q' * 500)
        assert not index.has_pair('G/a', 'ant_sex')
        assert index.top_by_facet('G/a', 'ant_sex', 5) is None
        assert index.facet_metadata('G/a', 'ant_sex') is None
        assert index.lookup('G/a', 'G1', 'ant_sex') is None


# ---------------------------------------------------------------- reload race


def _hammer(read_once, stop: threading.Event, failures: list) -> None:
    while not stop.is_set():
        problem = read_once()
        if problem:
            failures.append(problem)


def test_gene_stats_reload_serves_consistent_rows(tmp_path):
    """A reload that reorders rows must never pair a new frame with old dicts."""
    index_path = tmp_path / 'gene_stats_index.parquet'
    genes = [f'GENE{i}' for i in range(20000)]
    frames = [
        _stats_frame({'G/a': genes}, None),
        _stats_frame({'G/a': genes[::-1]}, None),
    ]
    _write_index(frames[0], index_path)
    index = GeneStatsIndex(index_path)
    index.load()

    def read_once():
        hit = index.lookup('G/a', 'GENE123')
        if hit is None:
            return 'miss'
        if 'gene_found' in hit:
            return f"wrong row: {hit['gene_found']}"
        return None

    stop, failures = threading.Event(), []
    reader = threading.Thread(target=_hammer, args=(read_once, stop, failures))
    reader.start()
    try:
        for round_idx in range(12):
            _write_index(frames[(round_idx + 1) % 2], index_path)
            index.load()
    finally:
        stop.set()
        reader.join()
    assert failures == []


def _variance_version(marker: str) -> pd.DataFrame:
    df = pd.concat([_variance_frame(None)] * 150, ignore_index=True)
    top = df.iloc[[0]].copy()
    top[['dataset', 'facet', 'gene', 'signal']] = ['G/b', 'ant_sex', marker, 99.0]
    return pd.concat([df, top], ignore_index=True)


def test_gene_variance_reload_never_misses_or_serves_old_data(tmp_path):
    """Readers during a reload see no empty window, and no old slice survives it."""
    index_path = tmp_path / 'gene_variance_index.parquet'
    versions = [_variance_version('OLD'), _variance_version('NEW')]
    versions[0].to_parquet(index_path, index=False)
    index = GeneVarianceIndex(index_path)
    index.load()

    def read_once():
        if index.top_by_facet('G/b', 'ant_sex', 3) is None:
            return 'top miss'
        if index.lookup('G/a', 'G1', 'ant_sex') is None:
            return 'lookup miss'
        return None

    for round_idx in range(8):
        marker = 'NEW' if round_idx % 2 == 0 else 'OLD'
        versions[1 if marker == 'NEW' else 0].to_parquet(index_path, index=False)
        stop, failures = threading.Event(), []
        reader = threading.Thread(target=_hammer, args=(read_once, stop, failures))
        reader.start()
        try:
            index.load()
        finally:
            stop.set()
            reader.join()
        assert failures == []
        assert index.top_by_facet('G/b', 'ant_sex', 1)[0]['gene'] == marker


# ---------------------------------------------------------------- upsert lock


def _upsert_worker(duckdb_path: str, catalog: str, n_rounds: int) -> None:
    import scores_parquet  # noqa: PLC0415
    rows = pd.DataFrame({
        'cell_id': np.arange(500), 'module': 'm1', 'score': np.linspace(0, 1, 500),
    })
    for _ in range(n_rounds):
        scores_parquet.upsert_scores(Path(duckdb_path), 'geneset', catalog, rows)


def test_concurrent_upserts_keep_every_catalog(tmp_path):
    import scores_parquet  # noqa: PLC0415
    duckdb_path = tmp_path / 'ds.duckdb'
    catalogs = [f'cat{i}' for i in range(6)]
    context = multiprocessing.get_context('fork')
    workers = [
        context.Process(target=_upsert_worker, args=(str(duckdb_path), catalog, 8))
        for catalog in catalogs
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
        assert worker.exitcode == 0
    summary = scores_parquet.summarize(duckdb_path)
    assert sorted(summary['catalog']) == catalogs
    assert set(summary['n_cells']) == {500}
    assert not list(tmp_path.glob('*.tmp'))
