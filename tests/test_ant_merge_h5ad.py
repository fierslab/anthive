"""Integration tests for bin/ant-merge-h5ad.

Builds a synthetic h5ad + paired duckdb, exercises the merge path end-to-end,
verifies columns appear in the h5ad with correct AnnData encoding.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import duckdb
import h5py
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))
sys.path.insert(0, str(REPO_ROOT / 'tests'))

from test_annotation_writer import build_synthetic_duckdb, CELL_NAMES  # noqa: E402
from anthive_db import AnnotationWriter  # noqa: E402

ANT_MERGE = REPO_ROOT / 'bin' / 'ingest' / 'ant-merge-h5ad'


def build_synthetic_h5ad(path: Path, obs_keys: list[str] | None = None) -> None:
    """Minimal h5ad with the AnnData obs/ encoding ant-merge-h5ad expects."""
    if obs_keys is None:
        obs_keys = ['sample', 'n_counts']
    with h5py.File(path, 'w') as f:
        obs = f.create_group('obs')
        # Cell-name index dataset (variable-length strings)
        str_dtype = h5py.string_dtype(encoding='utf-8')
        obs.create_dataset('_index',
                           data=np.array(CELL_NAMES, dtype=object),
                           dtype=str_dtype)
        # Existing numerical: n_counts
        if 'n_counts' in obs_keys:
            ds = obs.create_dataset(
                'n_counts',
                data=np.arange(len(CELL_NAMES), dtype='f8') + 100.0,
            )
            ds.attrs['encoding-type'] = 'array'
            ds.attrs['encoding-version'] = '0.2.0'
        # Existing categorical: sample
        if 'sample' in obs_keys:
            grp = obs.create_group('sample')
            cats = ['S0', 'S1', 'S2']
            grp.create_dataset('categories',
                               data=np.array(cats, dtype=object),
                               dtype=str_dtype)
            grp.create_dataset(
                'codes',
                data=np.array([i % 3 for i in range(len(CELL_NAMES))],
                              dtype='int8'),
            )
            grp.attrs['encoding-type'] = 'categorical'
            grp.attrs['encoding-version'] = '0.2.0'
            grp.attrs['ordered'] = False
        # obs/ attrs
        obs.attrs['_index'] = '_index'
        obs.attrs['column-order'] = np.array(obs_keys, dtype=object)
        obs.attrs['encoding-type'] = 'dataframe'
        obs.attrs['encoding-version'] = '0.2.0'


def _write_md_stub(md_path: Path) -> None:
    """Minimal .md so ant-merge-h5ad's path resolution works."""
    md_path.write_text("---\nname: test\n---\nstub\n")


def _run_merge(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(ANT_MERGE), *args],
        capture_output=True, text=True, timeout=60,
    )


def _setup_dataset(tmp_path: Path) -> Path:
    """Create matched .h5ad + .duckdb + .md; return the .md path."""
    md = tmp_path / "dataset.md"
    h5 = tmp_path / "dataset.h5ad"
    db = tmp_path / "dataset.duckdb"
    build_synthetic_h5ad(h5)
    build_synthetic_duckdb(db)
    _write_md_stub(md)
    return md


# ---------------------------------------------------------------------------

def test_clean_dataset_skipped(tmp_path):
    md = _setup_dataset(tmp_path)
    result = _run_merge(str(md))
    assert result.returncode == 0
    assert 'clean' in result.stdout.lower()


def test_merge_numerical_round_trip(tmp_path):
    md = _setup_dataset(tmp_path)
    db = md.with_suffix('.duckdb')
    h5 = md.with_suffix('.h5ad')

    values = pd.Series({name: float(i) * 0.1
                        for i, name in enumerate(CELL_NAMES)})
    with AnnotationWriter(db, annotator='ant-score', run_id='r1',
                          catalog='WGCNA_M3') as w:
        w.add_numerical('module_score_M3', values)
        w.commit()

    result = _run_merge(str(md))
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'merged 1 columns' in result.stdout

    with h5py.File(h5, 'r') as f:
        assert 'module_score_M3' in f['obs']
        arr = f['obs']['module_score_M3'][:]
        assert arr.dtype == np.float64
        assert arr.shape == (len(CELL_NAMES),)
        # First value should be 0.0
        assert arr[0] == 0.0
        # Last should be 0.1 * 19 = 1.9 (within float tolerance)
        np.testing.assert_allclose(arr[-1], 1.9)
        # column-order updated
        order = [v.decode() if isinstance(v, bytes) else v
                 for v in f['obs'].attrs['column-order']]
        assert 'module_score_M3' in order

    # __pending_cols should now be empty
    con = duckdb.connect(str(db), read_only=True)
    try:
        pending = con.execute("SELECT COUNT(*) FROM __pending_cols").fetchone()[0]
    finally:
        con.close()
    assert pending == 0


def test_merge_categorical_round_trip(tmp_path):
    md = _setup_dataset(tmp_path)
    db = md.with_suffix('.duckdb')
    h5 = md.with_suffix('.h5ad')

    values = pd.Series({name: ('A' if i < 7 else 'B' if i < 15 else 'C')
                        for i, name in enumerate(CELL_NAMES)})
    with AnnotationWriter(db, annotator='ant-starcat', run_id='sc-1',
                          catalog='MICROGLIA_V1') as w:
        w.add_categorical('starcat__MICROGLIA_V1__Identity_Label', values)
        w.commit()

    result = _run_merge(str(md))
    assert result.returncode == 0, (result.stdout, result.stderr)

    with h5py.File(h5, 'r') as f:
        grp = f['obs']['starcat__MICROGLIA_V1__Identity_Label']
        assert grp.attrs['encoding-type'] == 'categorical'
        cats = [c.decode() if isinstance(c, bytes) else c
                for c in grp['categories'][:]]
        assert sorted(cats) == ['A', 'B', 'C']
        codes = grp['codes'][:]
        assert codes.shape == (len(CELL_NAMES),)
        # 7 As, 8 Bs, 5 Cs (no -1 / NaN cells)
        assert (codes == -1).sum() == 0
        # Codes correspond to category indices
        cat_idx = {c: i for i, c in enumerate(cats)}
        for i, code in enumerate(codes):
            expected = 'A' if i < 7 else 'B' if i < 15 else 'C'
            assert code == cat_idx[expected]


def test_dry_run_does_not_write(tmp_path):
    md = _setup_dataset(tmp_path)
    db = md.with_suffix('.duckdb')
    h5 = md.with_suffix('.h5ad')

    values = pd.Series({name: 1.0 for name in CELL_NAMES})
    with AnnotationWriter(db, annotator='a', run_id='r1') as w:
        w.add_numerical('dryrun_col', values)
        w.commit()

    result = _run_merge('--dry-run', str(md))
    assert result.returncode == 0
    assert 'dry-run' in result.stdout

    # h5ad unchanged
    with h5py.File(h5, 'r') as f:
        assert 'dryrun_col' not in f['obs']
    # __pending_cols still has the row
    con = duckdb.connect(str(db), read_only=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM __pending_cols").fetchone()[0]
    finally:
        con.close()
    assert n == 1


def test_divergent_state_refused(tmp_path):
    """duckdb has a column not in __pending_cols → refuse."""
    md = _setup_dataset(tmp_path)
    db = md.with_suffix('.duckdb')

    # Manually inject a column bypassing AnnotationWriter
    con = duckdb.connect(str(db), read_only=False)
    con.execute('ALTER TABLE __obsnum ADD COLUMN sneaky DOUBLE')
    con.close()

    result = _run_merge(str(md))
    assert result.returncode == 2
    assert 'DIVERGENT' in result.stdout


def test_cell_name_mismatch_refused(tmp_path):
    """h5ad and duckdb agree on obs cols but disagree on cell_names → refuse.

    Note: divergent obs-column state is checked first, so this test gives the
    h5ad the same obs cols as the synthetic duckdb (sample, n_counts) and only
    differs on cell names.
    """
    md = tmp_path / "mismatch.md"
    h5 = md.with_suffix('.h5ad')
    db = md.with_suffix('.duckdb')

    # h5ad has matching obs cols but different cell names
    different_names = [f"OTHER_{i:04d}" for i in range(20)]
    with h5py.File(h5, 'w') as f:
        obs = f.create_group('obs')
        str_dtype = h5py.string_dtype(encoding='utf-8')
        obs.create_dataset('_index', data=np.array(different_names, dtype=object),
                           dtype=str_dtype)
        # Add sample + n_counts to match the synthetic duckdb so the divergent
        # state check passes — we want to test the cell-name check specifically.
        grp = obs.create_group('sample')
        grp.create_dataset('categories', data=np.array(['X'], dtype=object),
                           dtype=str_dtype)
        grp.create_dataset('codes', data=np.zeros(20, dtype='int8'))
        grp.attrs['encoding-type'] = 'categorical'
        grp.attrs['encoding-version'] = '0.2.0'
        grp.attrs['ordered'] = False
        ds = obs.create_dataset('n_counts', data=np.ones(20, dtype='f8'))
        ds.attrs['encoding-type'] = 'array'
        ds.attrs['encoding-version'] = '0.2.0'

        obs.attrs['_index'] = '_index'
        obs.attrs['column-order'] = np.array(['sample', 'n_counts'], dtype=object)
        obs.attrs['encoding-type'] = 'dataframe'
        obs.attrs['encoding-version'] = '0.2.0'

    build_synthetic_duckdb(db)
    _write_md_stub(md)

    # Add a pending col to trigger merge attempt
    values = pd.Series({name: 1.0 for name in CELL_NAMES})
    with AnnotationWriter(db, annotator='a', run_id='r1') as w:
        w.add_numerical('foo', values)
        w.commit()

    result = _run_merge(str(md))
    assert result.returncode == 3
    assert 'cell-name mismatch' in result.stdout
