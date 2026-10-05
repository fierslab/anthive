"""Determinism of the donor-level facet tests across thread counts and repeats.

Background: between two reruns of the same input the module-level tests
(``ant-facet-test-modules``) changed in the last bits of the donor-level means
because DuckDB's parallel hash aggregate added the per-cell scores in a thread
and scheduling dependent order (floating point addition is not associative).
The summaries then differed by 1e-17 in some columns. These tests pin:

* the per-cell score means (variant A) are bitwise identical for 1, 2, 4 and 8
  DuckDB threads on a sidecar large enough for the aggregate to run in parallel,
  with scores spanning many orders of magnitude (where order matters most),
* the pseudobulk count sums are identical and arrive sorted,
* the whole module tool and the whole gene tool give identical output tables
  with 1 and 4 threads on a synthetic dataset.

Run with:
    uv run --with pytest --with duckdb --with pandas --with numpy --with scipy \
           --with pyarrow --with pyyaml --no-project \
           env PYTHONPATH=lib python -m pytest tests/test_facet_determinism.py -q
(the gene tool test also needs an Rscript with limma and edgeR, see test_facet_test.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_facet_test import RSCRIPT, aft, make_dataset, needs_r  # noqa: E402
from test_facet_test_modules import make_module_dataset, mod  # noqa: E402

THREAD_COUNTS = (1, 2, 4, 8)


def _wide_range_sidecar(tmp_path: Path, n_cells: int = 90_000, n_modules: int = 16,
                        n_samples: int = 30, seed: int = 5):
    """Scores spanning ~12 orders of magnitude plus a cell to sample map."""
    rng = np.random.default_rng(seed)
    cell_ids = np.arange(n_cells, dtype=np.int64)
    rows = []
    for module_index in range(n_modules):
        magnitude = 10.0 ** rng.uniform(-6, 6, size=n_cells)
        rows.append(pd.DataFrame({
            'cell_id': cell_ids, 'source': 'geneset', 'catalog': 'Syn/Exp',
            'module': f'M{module_index:02d}',
            'score': rng.normal(size=n_cells) * magnitude}))
    sidecar = pd.concat(rows, ignore_index=True)
    sidecar = sidecar.sample(frac=1.0, random_state=seed).reset_index(drop=True)  # unsorted file
    sidecar_path = tmp_path / 'wide.scores.parquet'
    sidecar.to_parquet(sidecar_path, index=False, row_group_size=50_000)
    sample_map = pd.DataFrame({'cell_id': cell_ids,
                               'sample_idx': rng.integers(0, n_samples, size=n_cells)})
    return sidecar_path, sample_map


def test_cell_mean_scores_are_bitwise_identical_for_any_thread_count(tmp_path):
    sidecar_path, sample_map = _wide_range_sidecar(tmp_path)
    sample_sizes = sample_map.groupby('sample_idx').size()
    reference = None
    for threads in THREAD_COUNTS:
        for _repeat in range(2):
            conn = duckdb.connect(':memory:', config={'threads': threads})
            means, coverage = mod.cell_mean_scores(conn, sidecar_path, sample_map, sample_sizes,
                                                   ['geneset'])
            conn.close()
            if reference is None:
                reference = (means, coverage)
                continue
            # exact equality, no tolerance: this is the point of the test
            pd.testing.assert_frame_equal(means, reference[0], check_exact=True)
            pd.testing.assert_frame_equal(coverage, reference[1], check_exact=True)


def test_pseudobulk_sums_are_exact_sorted_and_thread_independent(tmp_path):
    rng = np.random.default_rng(3)
    n_cells, n_genes = 20_000, 40
    cell_ids = np.repeat(np.arange(n_cells, dtype=np.int32), n_genes)
    gene_ids = np.tile(np.arange(n_genes, dtype=np.int32), n_cells)
    values = rng.poisson(3.0, size=n_cells * n_genes).astype(np.int64)
    counts = pd.DataFrame({'cell_id': cell_ids, 'gene_id': gene_ids, 'value': values})
    counts = counts[counts['value'] > 0].sample(frac=1.0, random_state=3)
    counts_path = tmp_path / 'syn.layer.counts.by_gene.parquet'
    counts.to_parquet(counts_path, index=False, row_group_size=40_000)
    sample_map = pd.DataFrame({'cell_id': np.arange(n_cells, dtype=np.int32),
                               'sample_idx': rng.integers(0, 12, size=n_cells)})
    expected = (counts.merge(sample_map, on='cell_id').groupby(['sample_idx', 'gene_id'])['value']
                .sum().rename('count').reset_index())
    for threads in THREAD_COUNTS:
        conn = duckdb.connect(':memory:', config={'threads': threads})
        got = aft.pseudobulk(conn, counts_path, sample_map)
        conn.close()
        assert got[['sample_idx', 'gene_id']].equals(
            got[['sample_idx', 'gene_id']].sort_values(['sample_idx', 'gene_id']).reset_index(drop=True))
        pd.testing.assert_frame_equal(
            got.astype({'count': 'int64'}).reset_index(drop=True),
            expected.astype({'count': 'int64'}), check_dtype=False, check_exact=True)


def _module_tables(tmp_path: Path, threads: int) -> pd.DataFrame:
    store, geneset_root, entry = make_module_dataset(tmp_path / f't{threads}')
    registry = mod.GenesetRegistry(geneset_root)
    registry.scan()
    out_root = tmp_path / f't{threads}' / 'out'
    mod.process_dataset(entry, store, out_root, ['sex'], registry, ['geneset', 'ucell'],
                        n_permutations=200, threads=threads, memory_limit_gb=2)
    return pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')


def test_module_tool_output_is_identical_for_one_and_four_threads(tmp_path):
    single = _module_tables(tmp_path, 1)
    parallel = _module_tables(tmp_path, 4)
    assert len(single) > 0
    pd.testing.assert_frame_equal(single, parallel, check_exact=True)


def _gene_table(tmp_path: Path, threads: int) -> pd.DataFrame:
    store, entry = make_dataset(tmp_path / f't{threads}', n_f=10, n_m=10, samples_per_donor=2)
    out_root = tmp_path / f't{threads}' / 'out'
    aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=threads,
                        memory_limit_gb=1)
    return pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')


@needs_r
def test_gene_tool_output_is_identical_for_one_and_four_threads(tmp_path):
    single = _gene_table(tmp_path, 1)
    parallel = _gene_table(tmp_path, 4)
    assert len(single) > 0
    pd.testing.assert_frame_equal(single, parallel, check_exact=True)


def test_manifest_is_current_tracks_the_md_file(tmp_path):
    md_path = tmp_path / 'x.md'
    md_path.write_text('---\napi: {}\n---\n')
    manifest_path = tmp_path / 'x.facet_run_manifest.json'
    assert not aft.manifest_is_current(manifest_path, md_path)
    import json
    manifest_path.write_text(json.dumps({'md_sha256': aft.md_file_sha256(md_path)}))
    assert aft.manifest_is_current(manifest_path, md_path)
    md_path.write_text('---\napi: {obs_rename: {a: ant_age}}\n---\n')
    assert not aft.manifest_is_current(manifest_path, md_path)
    manifest_path.write_text(json.dumps({'status': 'done'}))      # old manifest without the field
    assert not aft.manifest_is_current(manifest_path, md_path)
