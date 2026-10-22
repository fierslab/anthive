"""An h5ad written by this test environment reads back with the versions the image pins, and the other way round.

Guard for raising a cap in ``tests/test_dependency_caps.py`` (anndata, h5py, pandas, numpy): the test process uses
whatever ``make test`` resolves (unpinned anndata and h5py, so a new release shows up here first), the other side runs
in a uv environment with exactly the versions of ``docker/requirements-app.lock``. Both sides compare X (sparse),
``layers['counts']``, obs (categorical, float, string, boolean), var and ``obsm['X_umap']`` with a reference built from
the same seed. Skipped only when uv is not installed or cannot build the pinned environment (offline host).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PINNED = ('anndata', 'h5py', 'numpy', 'pandas', 'scipy')

# One program for both sides: `build` makes the reference object, `write PATH` writes it, `check PATH` reads a file
# and compares it with the reference; prints the versions it ran with.
PROGRAM = r'''
import json, sys, warnings
import numpy as np, pandas as pd, scipy.sparse as sp, anndata as ad, h5py
warnings.simplefilter('ignore')

def build():
    rng = np.random.default_rng(7)
    counts = sp.random(60, 40, density=0.2, format='csr', random_state=7, data_rvs=lambda n: rng.integers(1, 30, n))
    counts = counts.astype(np.int32)
    lognorm = counts.astype(np.float32).copy()
    lognorm.data = np.log1p(lognorm.data).astype(np.float32)
    obs = pd.DataFrame({
        'cell_type': pd.Categorical(rng.choice(['microglia', 'astrocyte', 'neuron'], 60)),
        'n_counts': np.asarray(counts.sum(axis=1)).ravel().astype(np.float64),
        'donor': [f'D{i % 7}' for i in range(60)],
        'keep': rng.random(60) > 0.3,
    }, index=[f'cell{i}' for i in range(60)])
    var = pd.DataFrame({'gene_ids': [f'ENSG{i:011d}' for i in range(40)], 'highly_variable': np.arange(40) % 3 == 0},
                       index=[f'GENE{i}' for i in range(40)])
    adata = ad.AnnData(X=lognorm, obs=obs, var=var)
    adata.layers['counts'] = counts
    adata.obsm['X_umap'] = rng.normal(size=(60, 2)).astype(np.float32)
    return adata

def check(path):
    want, got = build(), ad.read_h5ad(path)
    assert sp.issparse(got.X) and np.allclose(got.X.toarray(), want.X.toarray())
    assert np.array_equal(np.asarray(got.layers['counts'].toarray()), want.layers['counts'].toarray())
    assert list(got.obs_names) == list(want.obs_names) and list(got.var_names) == list(want.var_names)
    assert list(got.obs['cell_type'].cat.categories) == list(want.obs['cell_type'].cat.categories)
    assert list(got.obs['cell_type'].astype(str)) == list(want.obs['cell_type'].astype(str))
    assert np.allclose(got.obs['n_counts'].to_numpy(), want.obs['n_counts'].to_numpy())
    assert list(got.obs['donor'].astype(str)) == list(want.obs['donor'])
    assert list(got.obs['keep'].astype(bool)) == list(want.obs['keep'])
    assert list(got.var['gene_ids'].astype(str)) == list(want.var['gene_ids'])
    assert list(got.var['highly_variable'].astype(bool)) == list(want.var['highly_variable'])
    assert np.allclose(np.asarray(got.obsm['X_umap']), want.obsm['X_umap'])

if __name__ == '__main__':
    action, path = sys.argv[1], sys.argv[2]
    if action == 'write':
        build().write_h5ad(path)
    else:
        check(path)
    from importlib.metadata import version
    print(json.dumps({name: version(name) for name in ('anndata', 'h5py', 'numpy', 'pandas', 'scipy')}))
'''


def _locked() -> dict:
    pins = {}
    for line in (REPO / 'docker' / 'requirements-app.lock').read_text().splitlines():
        match = re.match(r'([A-Za-z0-9_.-]+)==([^\s;]+)', line)
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


@pytest.fixture(scope='module')
def program(tmp_path_factory):
    path = tmp_path_factory.mktemp('roundtrip') / 'h5ad_round_trip.py'
    path.write_text(PROGRAM)
    return path


def _pinned(program: Path, action: str, h5ad: Path) -> dict:
    if shutil.which('uv') is None:
        pytest.skip('uv is not installed')
    locked = _locked()
    command = ['uv', 'run', '--quiet', '--no-project', '--python', '3.11']
    for name in PINNED:
        command += ['--with', f'{name}=={locked[name]}']
    command += ['python', '-I', str(program), action, str(h5ad)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0 and ('Failed to fetch' in result.stderr or 'No solution found' in result.stderr
                                   or 'failed to download' in result.stderr.lower()):
        pytest.skip(f'cannot build the pinned environment: {result.stderr.strip()[-300:]}')
    assert result.returncode == 0, result.stderr[-3000:]
    versions = json.loads(result.stdout.strip().splitlines()[-1])
    assert versions == {name: locked[name] for name in PINNED}
    return versions


def _here(program: Path, action: str, h5ad: Path) -> None:
    result = subprocess.run([sys.executable, str(program), action, str(h5ad)], capture_output=True, text=True,
                            timeout=600)
    assert result.returncode == 0, result.stderr[-3000:]


def test_an_h5ad_written_here_reads_back_with_the_pinned_versions(program, tmp_path):
    h5ad = tmp_path / 'here.h5ad'
    _here(program, 'write', h5ad)
    _pinned(program, 'check', h5ad)


def test_an_h5ad_written_with_the_pinned_versions_reads_back_here(program, tmp_path):
    h5ad = tmp_path / 'pinned.h5ad'
    _pinned(program, 'write', h5ad)
    _here(program, 'check', h5ad)
