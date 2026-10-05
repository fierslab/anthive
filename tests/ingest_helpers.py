"""Builders for the ingest-check tests: tiny h5ad files, DuckDB files, submissions and a store."""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

import anndata
import duckdb
import numpy as np
import pandas as pd
import scipy.sparse as sp
import yaml

from ingest_check.context import CheckContext, Settings

DESCRIPTION = ('Single-nucleus RNA-seq of human dorsolateral prefrontal cortex from a synthetic cohort, built for the '
               'ingest-check tests. It exists only in the test suite and describes nothing real at all, but it is '
               'long enough to pass the description length rule of the required keys check.')

GOOD_MUS = textwrap.dedent("""\
    organism=human
    tissue=dorsolateral prefrontal cortex
    cell_type=microglial cell
    technology=10x_chromium
    suspension_type=nucleus
    access=internal
    source=external
    license=CC-BY-4.0
    data_use=open
    disease=Alzheimer disease,normal
    condition=control
    sex=female,male
    age=60-80
    strain=na
    doi=10.1000/test
    pmid=1
    accession=GSE0
    year=2026
    contributor=Test Author
    title=Synthetic microglia
    link=https://example.org
    source_description=synthetic test data
    """)


def barcode(index: int) -> str:
    """A unique 16-mer nucleotide barcode with a -1 style suffix."""
    digits = ''.join('ACGT'[(index >> shift) & 3] for shift in range(0, 32, 2))
    return f'{digits}-1'


def donor_obs(n_cells: int, n_donors: int = 8, seed: int = 0) -> pd.DataFrame:
    """Per-cell obs with donor, sex, age, braak, disease columns (author names)."""
    rng = np.random.default_rng(seed)
    donors = np.array([f'D{index % n_donors:02d}' for index in range(n_cells)])
    frame = pd.DataFrame({'donor': donors}, index=[barcode(index) for index in range(n_cells)])
    per_donor = pd.DataFrame({
        'donor': [f'D{index:02d}' for index in range(n_donors)],
        'sex_raw': ['F', 'M'] * (n_donors // 2) + (['F'] if n_donors % 2 else []),
        'age': [60.0 + 3 * index for index in range(n_donors)],
        'braak': [index % 7 for index in range(n_donors)],
        'dx': ['AD', 'CTRL'] * (n_donors // 2) + (['AD'] if n_donors % 2 else []),
        'pmi': [4.0 + index for index in range(n_donors)]})
    merged = frame.reset_index(names='cell').merge(per_donor, on='donor', how='left').set_index('cell')
    merged['cell_type_raw'] = rng.choice(['microglia', 'astro'], size=n_cells)
    return merged


def make_h5ad(path: Path, n_obs: int = 60, n_vars: int = 40, x_kind: str = 'lognorm', counts: bool = True,
              layout: str = 'csr', gene_names: Optional[List[str]] = None, obs: Optional[pd.DataFrame] = None,
              seed: int = 0, x_noise: float = 0.0) -> Path:
    """Write a small AnnData with raw counts in ``layers['counts']`` and X of the requested kind.

    ``x_kind``: ``lognorm`` (log1p CP10k), ``counts`` (integers), ``scaled`` (z-scores), ``big`` (values above 20).
    ``layout``: ``csr``, ``csc`` or ``dense``. ``x_noise`` adds Gaussian noise to the log-normalised X.
    """
    rng = np.random.default_rng(seed)
    raw = rng.poisson(1.5, size=(n_obs, n_vars)).astype(np.float32)
    raw[:, 0] += 5  # no empty rows
    sums = raw.sum(axis=1, keepdims=True)
    lognorm = np.log1p(raw / sums * 1e4).astype(np.float32)
    if x_noise:
        lognorm = np.clip(lognorm + rng.normal(0, x_noise, lognorm.shape), 0, None).astype(np.float32)
    expression = {'lognorm': lognorm, 'counts': raw.copy(), 'big': lognorm * 40 + 25,
                  'scaled': (lognorm - lognorm.mean(axis=0)) / (lognorm.std(axis=0) + 1e-6)}[x_kind]

    def wrap(matrix: np.ndarray) -> Any:
        return {'csr': sp.csr_matrix, 'csc': sp.csc_matrix, 'dense': np.asarray}[layout](matrix)

    names = gene_names or [f'GENE{index}' for index in range(n_vars)]
    adata = anndata.AnnData(X=wrap(expression),
                            obs=obs if obs is not None else donor_obs(n_obs),
                            var=pd.DataFrame(index=names))
    if counts:
        adata.layers['counts'] = wrap(raw)
    adata.obsm['X_umap'] = rng.normal(size=(n_obs, 2)).astype(np.float32)
    adata.write_h5ad(path)
    return path


def write_md(path: Path, **front: Any) -> Path:
    """Write a dataset ``.md`` with YAML front matter (keys in ``front``) and an empty body."""
    path.write_text('---\n' + yaml.safe_dump(front, sort_keys=False) + '---\n\n')
    return path


def good_api() -> Dict[str, Any]:
    """An ``api:`` section that maps the donor_obs columns onto core facets."""
    return {'obs_rename': {'donor': 'ant_donor', 'age': 'ant_age', 'braak': 'ant_braak', 'pmi': 'ant_pmi',
                           'sex_raw': 'ant_sex', 'dx': 'ant_disease'},
            'obs_value_rename': {'sex_raw': {'F': 'f', 'M': 'm'}},
            'obs_derive': {'ant_donor_uid': {'source': 'donor', 'template': '{group_id}:{value}'}}}


def good_md_front(sha256: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """Front matter of a clean new dataset (version 1.0.0, one changelog entry)."""
    front: Dict[str, Any] = {
        'id': 'ABC', 'title': 'Synthetic microglia', 'description': DESCRIPTION, 'version': '1.0.0',
        'replicate_unit': 'donor', 'replicate_column': 'ant_donor', 'disease_basis': 'clinical_dx',
        'changelog': [{'version': '1.0.0', 'date': '2026-10-05', 'sha256': sha256 or '0' * 64,
                       'changes': ['initial version']}],
        'api': good_api()}
    front.update(extra)
    return front


def write_index_md(folder: Path, group: Optional[str] = None, group_id: str = 'ZZ1', **extra: Any) -> Path:
    """Write a complete group ``index.md``."""
    front = {'group': group or folder.name, 'group_id': group_id, 'doi': '10.1000/test', 'year': 2026,
             'first_author': 'Test Author', 'default_dataset': 'ABC'}
    front.update(extra)
    return write_md(folder / 'index.md', **front)


def make_duckdb(path: Path, obs: pd.DataFrame, genes: Optional[List[str]] = None, numeric: Optional[List[str]] = None) -> Path:
    """Create the tables the checks read: ``__cells``, ``__genes``, ``__obscat``, ``__obsnum``."""
    numeric = numeric or [name for name in obs.columns if pd.api.types.is_numeric_dtype(obs[name])]
    categorical = [name for name in obs.columns if name not in numeric]
    path.unlink(missing_ok=True)
    connection = duckdb.connect(str(path))
    cells = pd.DataFrame({'cell_id': np.arange(len(obs)), 'cell_name': list(obs.index)})
    genes = genes or [f'GENE{index}' for index in range(10)]
    gene_frame = pd.DataFrame({'gene_id': np.arange(len(genes)), 'gene_name': genes})
    cat_frame = obs[categorical].astype(object).reset_index(drop=True)
    cat_frame.insert(0, 'cell_id', np.arange(len(obs)))
    num_frame = obs[numeric].astype(float).reset_index(drop=True)
    num_frame.insert(0, 'cell_id', np.arange(len(obs)))
    for name, frame in (('__cells', cells), ('__genes', gene_frame), ('__obscat', cat_frame), ('__obsnum', num_frame)):
        connection.register('frame', frame)
        connection.execute(f'CREATE TABLE {name} AS SELECT * FROM frame')
        connection.unregister('frame')
    connection.close()
    return path


class Submission:
    """A submission folder inside a temporary tree, with a store and scratch dirs beside it."""

    def __init__(self, root: Path, group: str = 'TestGroup', basename: str = 'dataset') -> None:
        self.root = root
        self.group_dir = root / 'incoming' / group
        self.group_dir.mkdir(parents=True)
        self.store = root / 'store'
        self.store.mkdir()
        self.scratch = root / 'scratch'
        self.index_dir = root / 'index'
        self.basename = basename
        self.obs = donor_obs(60)

    def build(self, with_index_md: bool = True, md: Optional[Dict[str, Any]] = None, mus: str = GOOD_MUS,
              **h5ad_options: Any) -> 'Submission':
        """Write h5ad, .md (sha256 filled), .h5ad.mus and index.md."""
        from checksum import calculate_sha256

        h5ad_options.setdefault('n_obs', len(self.obs))
        h5ad = make_h5ad(self.group_dir / f'{self.basename}.h5ad', obs=self.obs, **h5ad_options)
        front = good_md_front(calculate_sha256(h5ad))
        front.update(md or {})
        write_md(self.group_dir / f'{self.basename}.md', **front)
        (self.group_dir / f'{self.basename}.h5ad.mus').write_text(mus)
        if with_index_md:
            write_index_md(self.group_dir)
        return self

    def duckdb(self, genes: Optional[List[str]] = None) -> Path:
        """A matching ``.duckdb`` outside the submission folder."""
        return make_duckdb(self.root / 'prebuilt.duckdb', self.obs, genes)

    def context(self, duckdb_path: Optional[Path] = None, convert: bool = False, **options: Any) -> CheckContext:
        """A ``CheckContext`` on this submission (no scratch convert unless ``convert``)."""
        return CheckContext(self.group_dir, self.basename, self.store, self.scratch, self.index_dir,
                            options.pop('settings', Settings()), duckdb_path=duckdb_path, convert=convert, **options)


def snapshot(folder: Path) -> Dict[str, Any]:
    """``{relative path: (size, mtime_ns)}`` of every file below ``folder`` (to prove nothing changed)."""
    return {str(path.relative_to(folder)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(folder.rglob('*')) if path.is_file()}


def seed_index(index_dir: Path, db_key: str, obs: pd.DataFrame, api: Dict[str, Any], genes: Optional[List[str]] = None,
               organism: str = 'human', workdir: Optional[Path] = None) -> None:
    """Add a "stored" dataset (built from ``obs`` and ``api``) to the overlap index in ``index_dir``."""
    from ingest_check.dataset_view import DatasetView
    from ingest_check.overlap_index import OverlapIndex, digests_from_view

    workdir = workdir or index_dir.parent
    path = make_duckdb(workdir / f'seed_{db_key.replace("/", "_")}.duckdb', obs, genes)
    group_id = db_key.split('/')[0]
    view = DatasetView(path, api, group_id, db_label=db_key)
    try:
        digests = digests_from_view(view, db_key, group_id)
    finally:
        view.close()
    index = OverlapIndex(index_dir)
    index.write_dataset(digests, {'seed': 1}, organism)
    index.rebuild_combined()
