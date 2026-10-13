"""DE sidecar persistence: full study metadata, restore, freshness, repair tool.

Covers ``lib/de_storage.py`` (write_sidecar metadata, reimport_sidecar,
studies_missing_from_duckdb, DEWriter.restore_from_sidecar), the
``ant-de-restore`` scan/repair tool, ``ant-duckdb-check``'s sidecar check,
``ant-convert``'s loud DE reimport, and the seurat preset's avg_logFC scale.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

import de_storage
from de_presets import PRESETS

_REPO_ROOT = Path(__file__).resolve().parent.parent

GENES = ['g0', 'g1', 'g2', 'g3']
STUDY_ONE = dict(
    study_id='s1', name='Study one', description='first', method='glmm',
    target='gene', reference_type='vsgrandmean', covariates='["age", "sex"]',
    n_tests_total=8, n_cells_total=123, subset_filter='{"cell_type": "mg"}',
    source='ant-de-ingest:orig.csv', created_at=datetime(2026, 1, 2, 3, 4, 5),
    notes='some notes', cluster_obs_column='leiden',
)
STUDY_TWO = dict(STUDY_ONE, study_id='s2', name='Study two', covariates=None,
                 subset_filter=None, cluster_obs_column=None, notes=None)


def _load_tool(name: str, relative: str):
    loader = importlib.machinery.SourceFileLoader(name, str(_REPO_ROOT / relative))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def make_duckdb(path: Path, studies=(STUDY_ONE, STUDY_TWO), genes=GENES) -> Path:
    """A minimal dataset duckdb with DE schema, the given studies and 1 row per gene."""
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE __genes (gene_id INTEGER, gene_name VARCHAR)")
    for gene_id, gene_name in enumerate(genes):
        connection.execute("INSERT INTO __genes VALUES (?, ?)", [gene_id, gene_name])
    de_storage.ensure_schema(connection)
    for study in studies:
        columns = list(study)
        connection.execute(
            f"INSERT INTO __de_study ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})", list(study.values()))
        for gene_id in range(len(genes)):
            connection.execute(
                "INSERT INTO __de (study_id, term, contrast, gene_id, log2fc, pvalue, padj) "
                "VALUES (?, 't', 'c', ?, 0.5, 0.01, 0.02)", [study['study_id'], gene_id])
    connection.close()
    return path


def fresh_duckdb(path: Path, genes=GENES) -> Path:
    """A rebuilt duckdb: genes only, no DE tables."""
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE __genes (gene_id INTEGER, gene_name VARCHAR)")
    for gene_id, gene_name in enumerate(genes):
        connection.execute("INSERT INTO __genes VALUES (?, ?)", [gene_id, gene_name])
    connection.close()
    return path


@pytest.fixture
def dataset(tmp_path):
    """(h5ad_path, populated duckdb path, sidecar path) in a temp folder."""
    h5ad = tmp_path / 'ds.h5ad'
    source = make_duckdb(tmp_path / 'ds.duckdb')
    sidecar = de_storage.sidecar_path_for(h5ad)
    de_storage.write_sidecar(sidecar, source)
    return h5ad, source, sidecar


def study_rows(path: Path):
    connection = duckdb.connect(str(path), read_only=True)
    try:
        columns = ', '.join(de_storage.STUDY_META_COLUMNS)
        rows = connection.execute(f"SELECT {columns} FROM __de_study ORDER BY study_id").fetchall()
    finally:
        connection.close()
    return [dict(zip(de_storage.STUDY_META_COLUMNS, row)) for row in rows]


# ── sidecar metadata ─────────────────────────────────────────────────────────


def test_write_sidecar_embeds_full_study_rows(dataset):
    _, _, sidecar = dataset
    meta = de_storage.read_sidecar_study_meta(sidecar)
    assert set(meta) == {'s1', 's2'}
    assert meta['s1']['covariates'] == '["age", "sex"]'
    assert meta['s1']['n_cells_total'] == 123
    assert meta['s1']['created_at'] == '2026-01-02T03:04:05'


def test_read_meta_missing_file_and_old_sidecar(tmp_path):
    assert de_storage.read_sidecar_study_meta(tmp_path / 'none.de.parquet') == {}
    old = tmp_path / 'old.de.parquet'
    duckdb.connect().execute(f"COPY (SELECT 1 AS study_id) TO '{old}' (FORMAT PARQUET)")
    assert de_storage.read_sidecar_study_meta(old) == {}


def test_read_meta_malformed_json(tmp_path):
    bad = tmp_path / 'bad.de.parquet'
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS study_id) TO '{bad}' (FORMAT PARQUET, "
        f"KV_METADATA {{{de_storage.STUDY_META_KEY}: 'not json'}})")
    assert de_storage.read_sidecar_study_meta(bad) == {}


def test_write_sidecar_quotes_in_metadata(tmp_path):
    tricky = dict(STUDY_ONE, notes="it's a \"quoted\" note")
    source = make_duckdb(tmp_path / 'q.duckdb', studies=(tricky,))
    sidecar = tmp_path / 'q.de.parquet'
    de_storage.write_sidecar(sidecar, source)
    assert de_storage.read_sidecar_study_meta(sidecar)['s1']['notes'] == "it's a \"quoted\" note"


def test_write_sidecar_only_study_keeps_other_studies_meta(dataset, tmp_path):
    _, source, sidecar = dataset
    connection = duckdb.connect(str(source))
    connection.execute("UPDATE __de_study SET notes = 'updated' WHERE study_id = 's1'")
    connection.execute("UPDATE __de_study SET notes = 'ignored' WHERE study_id = 's2'")
    connection.close()
    de_storage.write_sidecar(sidecar, source, only_study='s1')
    meta = de_storage.read_sidecar_study_meta(sidecar)
    assert meta['s1']['notes'] == 'updated'
    assert meta['s2']['notes'] is None   # untouched study keeps its embedded row
    assert {s['study_id'] for s in de_storage.read_sidecar_studies(sidecar)} == {'s1', 's2'}


def test_write_sidecar_only_study_without_existing_sidecar(tmp_path):
    source = make_duckdb(tmp_path / 'o.duckdb')
    sidecar = tmp_path / 'o.de.parquet'
    assert de_storage.write_sidecar(sidecar, source, only_study='s2') == len(GENES)
    assert set(de_storage.read_sidecar_study_meta(sidecar)) == {'s2'}


def test_write_sidecar_empty_writes_nothing(tmp_path):
    source = make_duckdb(tmp_path / 'e.duckdb', studies=())
    sidecar = tmp_path / 'e.de.parquet'
    assert de_storage.write_sidecar(sidecar, source) == 0
    assert not sidecar.exists()


# ── reimport ─────────────────────────────────────────────────────────────────


def test_reimport_restores_every_study_field(dataset, tmp_path):
    _, _, sidecar = dataset
    rebuilt = fresh_duckdb(tmp_path / 'rebuilt.duckdb')
    assert de_storage.reimport_sidecar(rebuilt, sidecar) == {'s1': 4, 's2': 4}
    restored = study_rows(rebuilt)
    assert restored == [dict(STUDY_ONE), dict(STUDY_TWO)]


def test_reimport_old_sidecar_falls_back_to_row_columns(dataset, tmp_path):
    _, source, _ = dataset
    old = tmp_path / 'legacy.de.parquet'
    connection = duckdb.connect()
    connection.execute(f"ATTACH '{source}' AS src (READ_ONLY)")
    connection.execute(f"COPY (SELECT * FROM src.de) TO '{old}' (FORMAT PARQUET)")
    connection.close()
    rebuilt = fresh_duckdb(tmp_path / 'rebuilt.duckdb')
    assert de_storage.reimport_sidecar(rebuilt, old) == {'s1': 4, 's2': 4}
    first = study_rows(rebuilt)[0]
    assert first['name'] == 'Study one' and first['method'] == 'glmm'
    assert first['source'] == 'reimport_sidecar:legacy.de.parquet'
    assert first['covariates'] is None


def test_reimport_missing_sidecar_is_noop(tmp_path):
    assert de_storage.reimport_sidecar(fresh_duckdb(tmp_path / 'x.duckdb'),
                                       tmp_path / 'none.de.parquet') == {}


def test_reimport_with_no_matching_genes_raises(dataset, tmp_path):
    _, _, sidecar = dataset
    rebuilt = fresh_duckdb(tmp_path / 'other.duckdb', genes=['x0', 'x1'])
    with pytest.raises(de_storage.DEReimportError, match='0 rows restored'):
        de_storage.reimport_sidecar(rebuilt, sidecar)


def test_reimport_partial_gene_overlap_returns_fewer_rows(dataset, tmp_path):
    _, _, sidecar = dataset
    rebuilt = fresh_duckdb(tmp_path / 'part.duckdb', genes=['g0', 'g1'])
    assert de_storage.reimport_sidecar(rebuilt, sidecar) == {'s1': 2, 's2': 2}


def test_reimport_is_repeatable(dataset, tmp_path):
    _, _, sidecar = dataset
    rebuilt = fresh_duckdb(tmp_path / 'again.duckdb')
    de_storage.reimport_sidecar(rebuilt, sidecar)
    assert de_storage.reimport_sidecar(rebuilt, sidecar) == {'s1': 4, 's2': 4}


# ── freshness ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize('state, expected', [
    ('no_de_tables', ['s1', 's2']),
    ('complete', []),
    ('one_study', ['s2']),
])
def test_studies_missing_from_duckdb(dataset, tmp_path, state, expected):
    _, source, sidecar = dataset
    if state == 'no_de_tables':
        target = fresh_duckdb(tmp_path / 't.duckdb')
    else:
        target = source
    if state == 'one_study':
        connection = duckdb.connect(str(target))
        connection.execute("DELETE FROM __de WHERE study_id = 's2'")
        connection.close()
    connection = duckdb.connect(str(target), read_only=True)
    try:
        assert de_storage.studies_missing_from_duckdb(connection, sidecar) == expected
    finally:
        connection.close()


def test_studies_missing_without_sidecar_is_empty(tmp_path):
    connection = duckdb.connect(str(fresh_duckdb(tmp_path / 'n.duckdb')))
    assert de_storage.studies_missing_from_duckdb(connection, tmp_path / 'n.de.parquet') == []
    connection.close()


def test_duckdb_check_reports_gap(dataset, tmp_path):
    check = _load_tool('ant_duckdb_check', 'bin/ingest/ant-duckdb-check')
    h5ad, _, original_sidecar = dataset
    folder = tmp_path / 'gap'
    folder.mkdir()
    target = fresh_duckdb(folder / 'ds.duckdb')
    de_storage.sidecar_path_for(target.with_suffix('.h5ad')).write_bytes(
        original_sidecar.read_bytes())
    report = check.DuckDBCheckReport()
    connection = duckdb.connect(str(target), read_only=True)
    check.check_de_sidecar(connection, target, report)
    connection.close()
    assert report.de_sidecar_gaps == ['s1', 's2']
    assert report.has_errors()
    assert any('DE study' in message for message in report.collect_log_messages())


def test_duckdb_check_clean_when_restored(dataset):
    check = _load_tool('ant_duckdb_check', 'bin/ingest/ant-duckdb-check')
    _, source, _ = dataset
    report = check.DuckDBCheckReport()
    connection = duckdb.connect(str(source), read_only=True)
    check.check_de_sidecar(connection, source, report)
    connection.close()
    assert report.de_sidecar_gaps == []


# ── DEWriter + ant-de-restore ────────────────────────────────────────────────


def test_writer_restore_only_missing_studies(dataset, tmp_path):
    _, source, sidecar = dataset
    target = fresh_duckdb(tmp_path / 'w.duckdb')
    with de_storage.DEWriter(target) as writer:
        assert writer.restore_from_sidecar(sidecar, ['s2']) == {'s2': 4}
    assert [row['study_id'] for row in study_rows(target)] == ['s2']
    assert study_rows(target)[0]['name'] == 'Study two'


@pytest.fixture
def restore_tool():
    return _load_tool('ant_de_restore', 'bin/annot/ant-de-restore')


def _entry(restore_tool, h5ad: Path, duckdb_path: Path):
    return restore_tool.H5adEntry(
        db_id='G/D', md_path=h5ad.with_suffix('.md'), h5ad_path=h5ad,
        duckdb_path=duckdb_path, group_id='G', file_id='D', study='Grp', stem='ds')


def test_restore_tool_scan_and_fix(dataset, tmp_path, restore_tool, monkeypatch, capsys):
    h5ad, source, sidecar = dataset
    source.unlink()
    fresh_duckdb(source)
    entry = _entry(restore_tool, h5ad, source)
    monkeypatch.setattr(restore_tool, 'get_config', lambda: {})
    monkeypatch.setattr(restore_tool, 'get_api_url', lambda cfg: 'http://127.0.0.1:1')
    monkeypatch.setattr(restore_tool, 'get_data_folder', lambda cfg: tmp_path)
    monkeypatch.setattr(restore_tool, 'iter_h5adstore', lambda root: iter([entry]))

    assert restore_tool.main([]) == 1                      # scan only: gap -> exit 1
    assert 'GAP   G/D' in capsys.readouterr().out
    with pytest.raises(duckdb.CatalogException):           # scan did not modify
        study_rows(source)
    assert restore_tool.main(['--fix']) == 0
    assert 'FIXED G/D: s1' in capsys.readouterr().out
    assert study_rows(source) == [dict(STUDY_ONE), dict(STUDY_TWO)]
    assert restore_tool.main([]) == 0                      # nothing left to do


def test_restore_tool_skips_datasets_without_sidecar_or_duckdb(tmp_path, restore_tool):
    h5ad = tmp_path / 'a.h5ad'
    assert restore_tool.find_gap(_entry(restore_tool, h5ad, tmp_path / 'a.duckdb')) is None
    fresh_duckdb(tmp_path / 'a.duckdb')
    assert restore_tool.find_gap(_entry(restore_tool, h5ad, tmp_path / 'a.duckdb')) is None


def test_restore_tool_unknown_dataset(restore_tool, monkeypatch, tmp_path):
    monkeypatch.setattr(restore_tool, 'get_config', lambda: {})
    monkeypatch.setattr(restore_tool, 'get_api_url', lambda cfg: 'http://x')
    monkeypatch.setattr(restore_tool, 'get_data_folder', lambda cfg: tmp_path)
    monkeypatch.setattr(restore_tool, 'find_h5ad', lambda root, ident: None)
    assert restore_tool.main(['NOPE/X']) == 2


def test_restore_tool_reports_failed_repair(dataset, tmp_path, restore_tool, monkeypatch):
    h5ad, source, _ = dataset
    source.unlink()
    fresh_duckdb(source, genes=['zz'])                     # no gene matches: restore fails
    entry = _entry(restore_tool, h5ad, source)
    monkeypatch.setattr(restore_tool, 'get_config', lambda: {})
    monkeypatch.setattr(restore_tool, 'get_api_url', lambda cfg: 'http://x')
    monkeypatch.setattr(restore_tool, 'get_data_folder', lambda cfg: tmp_path)
    monkeypatch.setattr(restore_tool, 'iter_h5adstore', lambda root: iter([entry]))
    assert restore_tool.main(['--fix']) == 1


# ── ant-convert ──────────────────────────────────────────────────────────────


def _make_h5ad_dataset(folder: Path, sidecar_genes):
    """A tiny h5ad + md + DE sidecar (study s1 over ``sidecar_genes``) in ``folder``."""
    import anndata
    import numpy as np
    import pandas as pd
    import scipy.sparse as sparse

    rng = np.random.default_rng(0)
    matrix = sparse.csr_matrix(rng.poisson(1, (20, 6)).astype('float32'))
    obs = pd.DataFrame({'grp': ['a', 'b'] * 10, 'score': rng.random(20)},
                       index=[f'c{i}' for i in range(20)])
    var = pd.DataFrame(index=[f'g{i}' for i in range(6)])
    adata = anndata.AnnData(X=matrix, obs=obs, var=var)
    adata.layers['counts'] = matrix.copy()
    adata.obsm['X_umap'] = rng.random((20, 2))
    adata.write_h5ad(folder / 't.h5ad')
    (folder / 't.md').write_text(
        "---\ntitle: t\nid: T\nversion: 0.1.0\nobsnum_include: [score]\n"
        "obscat_include: [grp]\nlayers_include: [X, counts]\nobsm_include: [X_umap]\n---\n")
    source = make_duckdb(folder / 'src.duckdb', studies=(STUDY_ONE,),
                         genes=sidecar_genes)
    de_storage.write_sidecar(folder / 't.de.parquet', source)
    source.unlink()
    return folder / 't.h5ad'


@pytest.fixture(scope='module')
def convert_tool():
    pytest.importorskip('anndata')
    return _load_tool('ant_convert', 'bin/ingest/ant-convert')


@pytest.mark.parametrize('staged', [False, True])
def test_convert_restores_full_de_studies(tmp_path, convert_tool, monkeypatch, staged):
    h5ad = _make_h5ad_dataset(tmp_path, [f'g{i}' for i in range(6)])
    if staged:
        monkeypatch.setenv('ANTHIVE_CONVERT_STAGING_DIR', str(tmp_path / 'stage'))
    convert_tool.convert_h5ad_to_duckdb(h5ad, convert_tool.get_config())
    assert study_rows(tmp_path / 't.duckdb') == [dict(STUDY_ONE)]


@pytest.mark.parametrize('staged', [False, True])
def test_convert_fails_loudly_when_sidecar_cannot_be_restored(
        tmp_path, convert_tool, monkeypatch, staged):
    h5ad = _make_h5ad_dataset(tmp_path, ['x0', 'x1'])      # genes not in the h5ad
    previous = fresh_duckdb(tmp_path / 't.duckdb')
    if staged:
        monkeypatch.setenv('ANTHIVE_CONVERT_STAGING_DIR', str(tmp_path / 'stage'))
    with pytest.raises(convert_tool.ConversionError, match='reimport failed'):
        convert_tool.convert_h5ad_to_duckdb(h5ad, convert_tool.get_config())
    if staged:
        assert previous.exists()                            # served file untouched
        assert not list((tmp_path / 'stage').glob('*.building'))
    else:
        assert not previous.exists()                        # no half-restored duckdb left


def test_convert_fails_fast_on_unreadable_sidecar(tmp_path, convert_tool):
    h5ad = _make_h5ad_dataset(tmp_path, GENES)
    (tmp_path / 't.de.parquet').write_bytes(b'not a parquet file')
    previous = fresh_duckdb(tmp_path / 't.duckdb')
    with pytest.raises(convert_tool.ConversionError, match='unreadable'):
        convert_tool.convert_h5ad_to_duckdb(h5ad, convert_tool.get_config())
    assert previous.exists()


# ── seurat preset: avg_logFC is natural log ──────────────────────────────────


@pytest.fixture(scope='module')
def de_ingest_tool():
    return _load_tool('ant_de_ingest', 'bin/annot/ant-de-ingest')


def _stage_seurat(tool, csv_path: Path, **overrides):
    preset = PRESETS['seurat']
    connection, view, _ = tool._build_stage_view(
        csv_path, None, preset.column_map, overrides, preset.column_transforms)
    try:
        return connection.execute(f"SELECT gene, log2fc FROM {view} ORDER BY gene").fetchall()
    finally:
        connection.close()


def test_seurat_avg_logfc_is_converted_to_log2(tmp_path, de_ingest_tool):
    import math
    csv_path = tmp_path / 'old.csv'
    csv_path.write_text(f"gene,avg_logFC,p_val,p_val_adj\nA,{math.log(2) * 3},0.1,0.2\n"
                        f"B,{-math.log(2)},0.1,0.2\n")
    assert _stage_seurat(de_ingest_tool, csv_path) == [
        ('A', pytest.approx(3.0)), ('B', pytest.approx(-1.0))]


def test_seurat_avg_log2fc_is_kept(tmp_path, de_ingest_tool):
    csv_path = tmp_path / 'new.csv'
    csv_path.write_text("gene,avg_log2FC,p_val,p_val_adj\nA,1.5,0.1,0.2\n")
    assert _stage_seurat(de_ingest_tool, csv_path) == [('A', 1.5)]


def test_seurat_preset_documents_transform():
    from de_presets import format_preset_help
    assert 'ln(2)' in format_preset_help(PRESETS['seurat'])
    assert all(not preset.column_transforms for name, preset in PRESETS.items() if name != 'seurat')


def test_volcano_label_says_raw_p():
    import facet_results
    assert 'raw' in facet_results.VOLCANO_YLABEL and '0.05' in facet_results.VOLCANO_YLABEL


# ── embedding metadata into legacy sidecars ──────────────────────────────────


def _legacy_sidecar(source: Path, destination: Path) -> None:
    connection = duckdb.connect()
    connection.execute(f"ATTACH '{source}' AS src (READ_ONLY)")
    connection.execute(f"COPY (SELECT * FROM src.de) TO '{destination}' (FORMAT PARQUET)")
    connection.close()


def test_sidecar_studies_without_metadata(dataset, tmp_path):
    _, source, sidecar = dataset
    assert de_storage.sidecar_studies_without_metadata(sidecar) == []
    legacy = tmp_path / 'legacy.de.parquet'
    _legacy_sidecar(source, legacy)
    assert de_storage.sidecar_studies_without_metadata(legacy) == ['s1', 's2']


def test_duckdb_covers_sidecar(dataset, tmp_path):
    _, source, sidecar = dataset
    connection = duckdb.connect(str(source), read_only=True)
    assert de_storage.duckdb_covers_sidecar(connection, sidecar)
    connection.close()
    shrunk = fresh_duckdb(tmp_path / 'shrunk.duckdb', genes=['g0', 'g1'])
    de_storage.reimport_sidecar(shrunk, sidecar)
    connection = duckdb.connect(str(shrunk), read_only=True)
    assert not de_storage.duckdb_covers_sidecar(connection, sidecar)
    connection.close()
    empty = duckdb.connect(str(fresh_duckdb(tmp_path / 'empty.duckdb')), read_only=True)
    assert not de_storage.duckdb_covers_sidecar(empty, sidecar)
    empty.close()


def test_restore_tool_embeds_metadata_in_legacy_sidecar(dataset, restore_tool):
    h5ad, source, sidecar = dataset
    _legacy_sidecar(source, sidecar)                       # replace with a legacy sidecar
    entry = _entry(restore_tool, h5ad, source)
    assert 'EMBED' in restore_tool.embed_metadata(entry)
    assert de_storage.read_sidecar_study_meta(sidecar)['s1']['covariates'] == '["age", "sex"]'
    assert restore_tool.embed_metadata(entry) is None      # already embedded


def test_restore_tool_embed_skips_when_duckdb_lacks_rows(dataset, tmp_path, restore_tool):
    h5ad, source, sidecar = dataset
    _legacy_sidecar(source, sidecar)
    source.unlink()
    fresh_duckdb(source)
    assert restore_tool.embed_metadata(_entry(restore_tool, h5ad, source)).startswith('SKIP')
    assert de_storage.sidecar_studies_without_metadata(sidecar) == ['s1', 's2']


def test_restore_tool_embed_nothing_to_do_without_sidecar(tmp_path, restore_tool):
    assert restore_tool.embed_metadata(
        _entry(restore_tool, tmp_path / 'a.h5ad', tmp_path / 'a.duckdb')) is None


def test_restore_tool_main_embed_mode(dataset, tmp_path, restore_tool, monkeypatch, capsys):
    h5ad, source, sidecar = dataset
    _legacy_sidecar(source, sidecar)
    entry = _entry(restore_tool, h5ad, source)
    monkeypatch.setattr(restore_tool, 'get_config', lambda: {})
    monkeypatch.setattr(restore_tool, 'get_api_url', lambda cfg: 'http://x')
    monkeypatch.setattr(restore_tool, 'get_data_folder', lambda cfg: tmp_path)
    monkeypatch.setattr(restore_tool, 'iter_h5adstore', lambda root: iter([entry]))
    assert restore_tool.main(['--embed-metadata']) == 0
    assert 'EMBED G/D' in capsys.readouterr().out
    assert de_storage.sidecar_studies_without_metadata(sidecar) == []


def test_write_sidecar_from_schema_v1_duckdb(tmp_path):
    source = make_duckdb(tmp_path / 'v1.duckdb', studies=(STUDY_ONE,))
    connection = duckdb.connect(str(source))
    connection.execute("DROP VIEW de")
    connection.execute("ALTER TABLE __de_study DROP COLUMN cluster_obs_column")
    connection.execute(de_storage._CREATE_DE_VIEW)
    connection.close()
    sidecar = tmp_path / 'v1.de.parquet'
    assert de_storage.write_sidecar(sidecar, source) == len(GENES)
    meta = de_storage.read_sidecar_study_meta(sidecar)['s1']
    assert meta['cluster_obs_column'] is None and meta['covariates'] == '["age", "sex"]'


def test_study_summary_counts_up_and_down_per_contrast(tmp_path):
    path = make_duckdb(tmp_path / 'sum.duckdb', studies=(STUDY_ONE,))
    connection = duckdb.connect(str(path))
    connection.execute("UPDATE __de SET effsize = CASE WHEN gene_id % 2 = 0 THEN 1.0 ELSE -1.0 END, efftype = 'log2fc'")
    connection.execute("UPDATE __de SET padj = 0.5 WHERE gene_id = 0")
    summary = de_storage.study_summary(connection, STUDY_ONE['study_id'])
    connection.close()
    row = summary['contrasts'][0]
    n = len(GENES)
    expected_up = sum(1 for g in range(n) if g % 2 == 0 and g != 0)
    assert (row['term'], row['contrast'], row['n_genes']) == ('t', 'c', n)
    assert row['n_sig'] == n - 1 and row['n_up'] == expected_up and row['n_down'] == n - 1 - expected_up
    assert row['efftype'] == 'log2fc' and summary['padj_max'] == 0.05 and 'subset_filter' in summary
    assert de_storage.study_summary(duckdb.connect(str(path), read_only=True), 'nope') is None
