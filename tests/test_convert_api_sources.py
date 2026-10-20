"""ant-convert stores every obs column the api: section reads, and takes the group id explicitly.

Regression for the first real deposit (Francken2026 PFC_Microglia_scvi_280926, 2026-10-20): attempts 1 and 2
left out the ``ant_donor_uid`` source ``donor`` (not in obscat_include), attempt 3 listed only ``donor`` and
dropped the ``obs_rename`` sources; and the work folder of a deposit into an existing group has no index.md,
so ``{group_id}`` was unknown and ``ant_donor_uid`` was not published.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import anndata
import duckdb
import numpy as np
import pandas as pd
import pytest

from obs_derive_forms import api_source_columns

CONVERT_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'ingest' / 'ant-convert'
N_DONORS = 1499                       # above every default categorical cardinality limit


def _convert_module():
    loader = importlib.machinery.SourceFileLoader('ant_convert_under_test', str(CONVERT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


API = {
    'obs_rename': {'sex_harmonised': 'ant_sex', 'age_harmonised': 'ant_age', 'donor': 'ant_donor'},
    'obs_value_rename': {'sex_harmonised': {'F': 'female', 'M': 'male'}},
    'obs_value_hidden': {'apoe_harmonised': ['NA']},
    'obs_derive': {'ant_donor_uid': {'source': 'donor', 'template': '{group_id}:{value}'}},
}


def _adata(n_cells: int = 3000) -> anndata.AnnData:
    rng = np.random.default_rng(0)
    obs = pd.DataFrame({
        'donor': pd.Categorical([f'D{index % N_DONORS:04d}' for index in range(n_cells)]),
        'sex_harmonised': pd.Categorical(rng.choice(['F', 'M'], n_cells)),
        'age_harmonised': rng.uniform(60, 90, n_cells),
        'apoe_harmonised': pd.Categorical(rng.choice(['33', '34', 'NA'], n_cells)),
        'cluster': pd.Categorical(rng.choice(['a', 'b'], n_cells)),
    }, index=[f'c{index}' for index in range(n_cells)])
    return anndata.AnnData(X=np.zeros((n_cells, 2), dtype=np.float32), obs=obs,
                           var=pd.DataFrame(index=['G1', 'G2']))


def test_api_source_columns_lists_every_reference_once():
    assert api_source_columns(API) == ['sex_harmonised', 'age_harmonised', 'donor', 'apoe_harmonised']
    assert api_source_columns(None) == []


@pytest.mark.parametrize('obscat, obsnum', [(['cluster'], []), (['donor'], []), ([], [])])
def test_mapping_sources_are_added_with_the_right_table(obscat, obsnum, capsys):
    convert = _convert_module()
    metadata = {'obscat_include': list(obscat), 'obsnum_include': list(obsnum), 'api': API}
    added = convert.add_api_source_columns(_adata(), metadata)
    assert set(metadata['obscat_include']) >= {'donor', 'sex_harmonised', 'apoe_harmonised'}
    assert 'age_harmonised' in metadata['obsnum_include'] and 'age_harmonised' not in metadata['obscat_include']
    assert added and 'stored because the api: section reads them' in capsys.readouterr().out
    assert all(not item.startswith(tuple(obscat)) for item in added)        # listed columns are not re-added


def test_columns_the_h5ad_lacks_are_left_to_the_checks():
    convert = _convert_module()
    metadata = {'obscat_include': ['cluster'], 'api': {'obs_rename': {'gone': 'ant_sex'}}}
    assert convert.add_api_source_columns(_adata(), metadata) == []
    assert metadata['obscat_include'] == ['cluster']


def test_converted_views_publish_the_mapped_facets_and_the_donor_uid(tmp_path):
    convert = _convert_module()
    adata = _adata()
    metadata = {'id': 'PFC', 'obscat_include': ['donor'], 'api': API}
    convert.add_api_source_columns(adata, metadata)
    conn = duckdb.connect(str(tmp_path / 'x.duckdb'))
    convert.initialize_database(conn)
    convert.create_cell_index(conn, adata)
    convert.convert_observations(conn, adata, metadata, group_id='F26')
    cat_columns = {row[0] for row in conn.execute("SELECT column_name FROM information_schema.columns "
                                                   "WHERE table_name = 'obscat'").fetchall()}
    num_columns = {row[0] for row in conn.execute("SELECT column_name FROM information_schema.columns "
                                                   "WHERE table_name = 'obsnum'").fetchall()}
    assert {'ant_sex', 'ant_donor', 'ant_donor_uid'} <= cat_columns and 'ant_age' in num_columns
    n_uids = conn.execute('SELECT count(DISTINCT ant_donor_uid) FROM obscat').fetchone()[0]
    assert n_uids == N_DONORS
    assert conn.execute('SELECT min(ant_donor_uid) FROM obscat').fetchone()[0].startswith('F26:')
    conn.close()


def test_fill_keeps_a_high_cardinality_mapping_source():
    import ingest_md_fill as md_fill

    profile = md_fill.H5adProfile.__new__(md_fill.H5adProfile)
    fields = {'n_obs': 10, 'n_vars': 2, 'layer_stats': {}, 'obsm_keys': [], 'obs_numeric': ['age_harmonised'],
              'obs_categorical': {'donor': N_DONORS, 'cell_barcode': 10 ** 6, 'cluster': 2}}
    for name, value in fields.items():
        object.__setattr__(profile, name, value)
    meta, _ = md_fill.fill_catalog_fields({'api': API}, profile, 'f' * 64, {}, 'x', 'G01', set(), '2026-10-20')
    assert 'donor' in meta['obscat_include'] and 'cluster' in meta['obscat_include']
    assert 'cell_barcode' not in meta['obscat_include']
