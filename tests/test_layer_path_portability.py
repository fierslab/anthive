"""Regression tests: a dataset keeps working after its store moves.

Each ``.duckdb`` persists ``layer__*`` views whose ``read_parquet`` call names
the absolute sidecar path of the host that converted it. When the store is
synced to a host with a different path, those views read nothing ("No files
found", or a permission error under ant-serve's ``allowed_directories``).
Served code must resolve the sidecar from the duckdb's own location at query
time instead. These tests build a small parquet-backed dataset the way
ant-convert does, move it (duckdb + sidecars) to another directory, confirm
the persisted view is really broken there, and then check that the query
layer, the analytics sandbox and the REST endpoints (gene_stats including
``top=``, cells, plots, raw SQL, pseudobulk, gene variance) all still work.

Expected values are computed independently in numpy/pandas from the same
synthetic inputs.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Dict

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))

from anthive_db import (  # noqa: E402
    AnthiveDB,
    install_portable_layer_views,
    layer_sidecar_path,
    portable_layer_views,
)

N_CELLS = 60
CELL_NAMES = [f"cell_{i:03d}" for i in range(N_CELLS)]
GENE_NAMES = ['APOE', 'TREM2', 'CX3CR1', 'P2RY12', 'CST3', 'SPP1', 'GPNMB', 'SILENT']
CELL_TYPES = ['micro', 'astro', 'oligo']


def _layer_frame(layer: str) -> pd.DataFrame:
    """Sparse (cell_id, gene_id, value), zeros omitted, SILENT never expressed."""
    rng = np.random.default_rng(17 if layer == 'X' else 23)
    rows = []
    for cell_id in range(N_CELLS):
        for gene_id in range(len(GENE_NAMES) - 1):
            if rng.random() < 0.5 + 0.05 * gene_id:
                if layer == 'counts':
                    value = float(rng.integers(1, 30))
                else:
                    value = float(np.round(rng.gamma(1.0 + gene_id, 0.5), 4))
                rows.append((cell_id, gene_id, value))
    return pd.DataFrame(rows, columns=['cell_id', 'gene_id', 'value'])


def build_converted_dataset(group_dir: Path) -> Path:
    """Write ds.duckdb + ds.md + layer sidecars the way ant-convert does.

    The layer views bake the absolute sidecar paths under ``group_dir``.
    """
    group_dir.mkdir(parents=True, exist_ok=True)
    db_path = group_dir / 'ds.duckdb'
    con = duckdb.connect(str(db_path))
    con.register('t_cells', pd.DataFrame({'cell_id': range(N_CELLS), 'cell_name': CELL_NAMES}))
    con.execute("CREATE TABLE __cells (cell_id INTEGER PRIMARY KEY, cell_name VARCHAR NOT NULL UNIQUE)")
    con.execute("INSERT INTO __cells SELECT * FROM t_cells")
    con.execute("CREATE VIEW cells AS SELECT * FROM __cells")
    con.register('t_genes', pd.DataFrame({'gene_id': range(len(GENE_NAMES)), 'gene_name': GENE_NAMES}))
    con.execute("CREATE TABLE __genes (gene_id INTEGER PRIMARY KEY, gene_name VARCHAR NOT NULL UNIQUE)")
    con.execute("INSERT INTO __genes SELECT * FROM t_genes")
    con.execute("CREATE VIEW genes AS SELECT * FROM __genes")

    obscat = pd.DataFrame({
        'cell_id': range(N_CELLS),
        'cell_type': [CELL_TYPES[i % 3] for i in range(N_CELLS)],
        'ant_group': [f"g{i % 2}" for i in range(N_CELLS)],
    })
    obsnum = pd.DataFrame({'cell_id': range(N_CELLS),
                           'n_counts': np.linspace(500.0, 2000.0, N_CELLS)})
    con.register('t_cat', obscat)
    con.execute("CREATE TABLE __obscat AS SELECT * FROM t_cat")
    con.register('t_num', obsnum)
    con.execute("CREATE TABLE __obsnum AS SELECT * FROM t_num")
    con.execute("CREATE VIEW obscat AS SELECT c.cell_name, o.* EXCLUDE (cell_id) "
                "FROM __obscat o JOIN __cells c USING (cell_id)")
    con.execute("CREATE VIEW obsnum AS SELECT c.cell_name, o.* EXCLUDE (cell_id) "
                "FROM __obsnum o JOIN __cells c USING (cell_id)")

    for layer in ('X', 'counts'):
        frame = _layer_frame(layer)
        con.register('t_layer', frame)
        by_gene = group_dir / f'ds.layer.{layer}.by_gene.parquet'
        by_cell = group_dir / f'ds.layer.{layer}.by_cell.parquet'
        con.execute(f"COPY (SELECT * FROM t_layer ORDER BY gene_id, cell_id) TO '{by_gene}' (FORMAT PARQUET)")
        con.execute(f"COPY (SELECT * FROM t_layer ORDER BY cell_id, gene_id) TO '{by_cell}' (FORMAT PARQUET)")
        con.unregister('t_layer')
        # Same three views ant-convert creates, with baked absolute paths.
        con.execute(f"""
            CREATE VIEW layer__{layer} AS
            SELECT c.cell_name, g.gene_name, l.value
            FROM read_parquet('{by_gene.resolve()}') l
            JOIN __cells c ON l.cell_id = c.cell_id
            JOIN __genes g ON l.gene_id = g.gene_id
        """)
        con.execute(f"CREATE VIEW layer__{layer}_by_gene AS SELECT * FROM layer__{layer}")
        con.execute(f"""
            CREATE VIEW layer__{layer}_by_cell AS
            SELECT c.cell_name, g.gene_name, l.value
            FROM read_parquet('{by_cell.resolve()}') l
            JOIN __cells c ON l.cell_id = c.cell_id
            JOIN __genes g ON l.gene_id = g.gene_id
        """)
    con.close()
    (group_dir / 'ds.md').write_text("---\ntitle: Portable layer test\ndefault_layer: X\n---\n")
    return db_path


def _expected_dense(layer: str = 'X') -> pd.DataFrame:
    """Dense cells x genes matrix (zeros filled), indexed by cell_name."""
    frame = _layer_frame(layer)
    dense = np.zeros((N_CELLS, len(GENE_NAMES)))
    dense[frame['cell_id'].to_numpy(), frame['gene_id'].to_numpy()] = frame['value'].to_numpy()
    return pd.DataFrame(dense, index=CELL_NAMES, columns=GENE_NAMES)


@pytest.fixture
def moved_store(tmp_path) -> Dict[str, Path]:
    """Convert under ``origin/``, then move the whole group to ``site/store``.

    Returns the new store root, the moved duckdb, and the old origin dir
    (which no longer exists, as on a host that never had that path).
    """
    origin_store = tmp_path / 'origin' / 'h5adstore'
    build_converted_dataset(origin_store / 'grp')
    site_store = tmp_path / 'site' / 'data' / 'h5adstore'
    site_store.mkdir(parents=True)
    shutil.move(str(origin_store / 'grp'), str(site_store / 'grp'))
    shutil.rmtree(tmp_path / 'origin')
    return {'store': site_store, 'db': site_store / 'grp' / 'ds.duckdb',
            'origin': origin_store}


def _hardened_cursor(db_path: Path, allowed_dirs):
    """Mirror ant-serve's ConnectionPool hardening for one dataset."""
    conn = duckdb.connect(':memory:')
    conn.execute(f"ATTACH '{db_path}' AS db (READ_ONLY)")
    dirs = ', '.join(f"'{str(d).rstrip('/')}/'" for d in allowed_dirs)
    conn.execute(f"SET allowed_directories = [{dirs}]")
    conn.execute("SET enable_external_access = false")
    conn.execute("SET lock_configuration = true")
    cursor = conn.cursor()
    cursor.execute("USE db")
    return conn, cursor


# ── the scenario is real ─────────────────────────────────────────────────────


def test_persisted_view_is_broken_after_the_move(moved_store):
    """Guard against a vacuous test: the baked view really fails here."""
    con = duckdb.connect(str(moved_store['db']), read_only=True)
    try:
        with pytest.raises(duckdb.IOException):
            con.execute("SELECT COUNT(*) FROM layer__X").fetchone()
    finally:
        con.close()


# ── lib/anthive_db ───────────────────────────────────────────────────────────


def test_sidecar_path_is_resolved_next_to_the_moved_duckdb(moved_store):
    found = layer_sidecar_path(moved_store['db'], 'X')
    assert found == str(moved_store['db'].parent / 'ds.layer.X.by_gene.parquet')
    assert layer_sidecar_path(moved_store['db'], 'X', 'by_cell').endswith('ds.layer.X.by_cell.parquet')
    assert layer_sidecar_path(moved_store['db'], 'nope') is None


@pytest.mark.parametrize('layer', ['../X', 'X/../../etc', "X'", '', '.hidden'])
def test_sidecar_path_rejects_unsafe_layer_keys(moved_store, layer):
    assert layer_sidecar_path(moved_store['db'], layer) is None


def test_cell_table_and_gene_stats_on_moved_dataset(moved_store):
    expected = _expected_dense('X')
    with AnthiveDB(db_path=moved_store['db']) as db:
        table = db.get_cell_table(genes=['APOE', 'SILENT'], metadata_fields=['cell_type'],
                                  layer='X').set_index('cell_name')
        np.testing.assert_allclose(table.loc[CELL_NAMES, 'APOE'].to_numpy(),
                                   expected['APOE'].to_numpy(), atol=1e-9)
        assert (table['SILENT'] == 0).all()

        # No __gene_bins: get_gene_stats takes the full-layer scan path.
        stats = {s['gene']: s for s in db.get_gene_stats(['TREM2', 'SPP1'], layer='X')}
    for gene in ('TREM2', 'SPP1'):
        values = expected[gene].to_numpy()
        assert stats[gene]['n_nonzero'] == int((values > 0).sum())
        assert stats[gene]['mean'] == pytest.approx(values.mean(), abs=1e-5)
        assert stats[gene]['max'] == pytest.approx(values.max(), abs=1e-5)
    all_means = expected.mean(axis=0)
    assert stats['SPP1']['mean_expr_layer'] == pytest.approx(all_means['SPP1'], abs=1e-5)


def test_hardened_instance_reads_only_inside_allowed_directories(moved_store):
    """The resolved sidecar lies inside the duckdb's directory, which is
    all a pooled instance is allowed to read."""
    conn, cursor = _hardened_cursor(moved_store['db'], [moved_store['db'].parent])
    try:
        db = AnthiveDB(db_path=str(moved_store['db']), conn=cursor, owns_conn=False)
        table = db.get_cell_table(genes=['GPNMB'], layer='X').set_index('cell_name')
        np.testing.assert_allclose(table.loc[CELL_NAMES, 'GPNMB'].to_numpy(),
                                   _expected_dense('X')['GPNMB'].to_numpy(), atol=1e-9)
        assert db.get_gene_stats(['APOE'], layer='counts')[0]['n_nonzero'] > 0
        # The hardening itself is unchanged: reading elsewhere still fails.
        with pytest.raises(duckdb.PermissionException):
            cursor.execute(f"SELECT * FROM read_parquet('{moved_store['store']}/../x.parquet')")
    finally:
        cursor.close()
        conn.close()


def test_symlinked_duckdb_finds_sidecars_beside_its_target(moved_store, tmp_path):
    """A duckdb symlinked into another store finds sidecars that only exist
    next to the link target, and reads them through an allowed directory."""
    review = tmp_path / 'review' / 'grp'
    review.mkdir(parents=True)
    link = review / 'ds.duckdb'
    link.symlink_to(moved_store['db'])
    assert layer_sidecar_path(link, 'X') == str(moved_store['db'].parent / 'ds.layer.X.by_gene.parquet')
    allowed = [review, moved_store['db'].parent]   # what ConnectionPool allows for a symlink
    conn, cursor = _hardened_cursor(link, allowed)
    try:
        db = AnthiveDB(db_path=str(link), conn=cursor, owns_conn=False)
        assert db.get_gene_stats(['CST3'], layer='X')[0]['n_nonzero'] > 0
    finally:
        cursor.close()
        conn.close()


def test_portable_views_shadow_the_persisted_ones(moved_store):
    expected = _expected_dense('X')
    conn, cursor = _hardened_cursor(moved_store['db'], [moved_store['db'].parent])
    try:
        specs = portable_layer_views(cursor, str(moved_store['db']))
        names = [name for name, _, _ in specs]
        assert set(names) == {f'layer__{lay}{suffix}' for lay in ('X', 'counts')
                              for suffix in ('', '_by_gene', '_by_cell')}
        for _, sql, paths in specs:
            assert str(moved_store['origin']) not in sql
            for path in paths:
                assert path.startswith(str(moved_store['db'].parent))
        installed = install_portable_layer_views(cursor, str(moved_store['db']))
        assert sorted(installed) == sorted(names)
        for view in ('layer__X', 'layer__X_by_gene', 'layer__X_by_cell'):
            rows = dict(cursor.execute(
                f"SELECT gene_name, SUM(value) FROM {view} GROUP BY 1").fetchall())
            assert rows['APOE'] == pytest.approx(expected['APOE'].sum(), abs=1e-6)
        # TEMP views are per connection: a fresh cursor still sees the
        # persisted (broken) definition, so nothing leaks between requests.
        other = conn.cursor()
        other.execute("USE db")
        with pytest.raises(duckdb.Error):
            other.execute("SELECT COUNT(*) FROM layer__X").fetchone()
        other.close()
    finally:
        cursor.close()
        conn.close()


def test_renamed_dataset_uses_current_stem(moved_store):
    """Datasets get renamed after conversion (SerranoPozo2024_* became
    Wachter2024_* in the dev store); the sidecars are renamed too while the
    views keep the old file name. Both read paths must follow the stem."""
    group = moved_store['db'].parent
    for path in list(group.iterdir()):
        path.rename(group / path.name.replace('ds.', 'renamed.', 1))
    renamed = group / 'renamed.duckdb'
    assert layer_sidecar_path(renamed, 'X') == str(group / 'renamed.layer.X.by_gene.parquet')
    conn, cursor = _hardened_cursor(renamed, [group])
    try:
        db = AnthiveDB(db_path=str(renamed), conn=cursor, owns_conn=False)
        assert db.get_gene_stats(['APOE'], layer='X')[0]['n_nonzero'] > 0
        install_portable_layer_views(cursor, str(renamed))
        total = cursor.execute("SELECT SUM(value) FROM layer__counts_by_cell").fetchone()[0]
        assert total == pytest.approx(_layer_frame('counts')['value'].sum())
    finally:
        cursor.close()
        conn.close()


def test_analytics_sandbox_reads_layer_views_after_the_move(moved_store, tmp_path, monkeypatch):
    import analytics
    monkeypatch.setattr(analytics, '_get_tmp_dir', lambda: tmp_path)
    result = analytics.run_query(
        str(moved_store['db']),
        "SELECT gene_name, SUM(value) AS total FROM layer__X GROUP BY 1 ORDER BY 1",
    )
    preview = {row['gene_name']: row['total'] for row in result['preview']}
    assert preview['APOE'] == pytest.approx(_expected_dense('X')['APOE'].sum(), abs=1e-6)


# ── ant-serve endpoints (in-process) ─────────────────────────────────────────


def _load_ant_serve():
    for module in ('uvicorn', 'matplotlib', 'seaborn', 'fastapi', 'httpx'):
        pytest.importorskip(module)
    name = 'ant_serve_layer_portability'
    if name in sys.modules:
        return sys.modules[name]
    loader = importlib.machinery.SourceFileLoader(name, str(REPO_ROOT / 'bin' / 'api' / 'ant-serve'))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


@pytest.fixture
def serve_client(moved_store, tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHIVE_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('ANTHIVE_TMP_DIR', str(tmp_path / 'tmp'))
    monkeypatch.setenv('ANTHIVE_CATALOG_SCAN', 'local')
    serve = _load_ant_serve()
    from fastapi.testclient import TestClient
    pool = serve.ConnectionPool(threads=1, memory_limit='256MB',
                                allowed_directories=[str(moved_store['store'])])
    monkeypatch.setattr(serve, 'connection_pool', pool)
    monkeypatch.setattr(serve, 'registry', serve.DatabaseRegistry(moved_store['store'], pool))
    assert len(serve.registry.databases) == 1
    db_id = next(iter(serve.registry.databases))   # generated id, e.g. GXX/DSX
    # no context manager: lifespan tasks not started. A signed-in caller: the synthetic dataset has no
    # `access: public`, so an anonymous one would get 404 (lib/dataset_access.py, tested in test_api_access.py).
    client = TestClient(serve.app, headers={'Remote-User': 'tester'})
    yield client, db_id
    pool.close_all()


def test_gene_stats_endpoint_including_top(serve_client):
    serve_client, db_id = serve_client
    expected = _expected_dense('X')
    response = serve_client.get(f'/databases/{db_id}/gene_stats', params={'genes': 'APOE,TREM2'})
    assert response.status_code == 200, response.text
    stats = {s['gene']: s for s in response.json()['stats']}
    assert stats['APOE']['mean'] == pytest.approx(expected['APOE'].mean(), abs=1e-5)

    # The fleet-wide 500: no __gene_bins, so the top-N candidates come from
    # the layer fallback in _get_top_genes_by_mean.
    response = serve_client.get(f'/databases/{db_id}/gene_stats', params={'top': 20})
    assert response.status_code == 200, response.text
    returned = [s['gene'] for s in response.json()['stats']]
    expressed = [g for g in GENE_NAMES if g != 'SILENT']
    assert set(expressed) <= set(returned)


def test_top_genes_fallback_ranks_by_mean_over_all_cells(serve_client):
    serve_client, db_id = serve_client
    serve = sys.modules['ant_serve_layer_portability']
    with serve._pooled_db(db_id) as db:
        top = serve._get_top_genes_by_mean(db, 3, 'X')
    means = _expected_dense('X').mean(axis=0).sort_values(ascending=False)
    assert top == list(means.index[:3])


def test_cells_endpoint_returns_expression(serve_client):
    serve_client, db_id = serve_client
    response = serve_client.get(f'/databases/{db_id}/cells',
                                params={'genes': 'SPP1', 'metadata': 'cell_type'})
    assert response.status_code == 200, response.text
    payload = response.json()
    rows = payload['data'] if isinstance(payload, dict) and 'data' in payload else payload
    frame = pd.DataFrame(rows).set_index('cell_name')
    np.testing.assert_allclose(frame.loc[CELL_NAMES, 'SPP1'].to_numpy(dtype=float),
                               _expected_dense('X')['SPP1'].to_numpy(), atol=1e-6)


@pytest.mark.parametrize('kind,params', [
    ('violin', {'x': 'cell_type', 'y': 'APOE'}),
    ('histogram', {'x': 'TREM2'}),
])
def test_plot_endpoints_render(serve_client, kind, params):
    serve_client, db_id = serve_client
    response = serve_client.get(f'/databases/{db_id}/plot/{kind}', params=params)
    assert response.status_code == 200, response.text[:500]
    assert response.content[:8] == b'\x89PNG\r\n\x1a\n'


def test_raw_sql_and_analytics_schema_use_local_sidecars(serve_client):
    serve_client, db_id = serve_client
    response = serve_client.post(f'/databases/{db_id}/query/sql',
                                 json={'query': 'SELECT COUNT(*) AS n FROM layer__X_by_cell'})
    assert response.status_code == 200, response.text
    assert response.json()['data'][0]['n'] == len(_layer_frame('X'))

    response = serve_client.get(f'/databases/{db_id}/analytics/schema')
    assert response.status_code == 200, response.text
    tables = response.json()
    tables = tables.get('tables', tables) if isinstance(tables, dict) else tables
    layer_entry = next(t for t in tables if t['table'] == 'layer__X')
    assert layer_entry['row_count'] == len(_layer_frame('X'))


def test_pseudobulk_counts_on_moved_dataset(serve_client):
    serve_client, db_id = serve_client
    response = serve_client.get(f'/databases/{db_id}/pseudobulk',
                                params={'group_by': 'cell_type', 'layer': 'counts',
                                        'min_cells': 1, 'format': 'csv'})
    assert response.status_code == 200, response.text[:500]
    table = pd.read_csv(pd.io.common.StringIO(response.text), comment='#').set_index('gene_name')
    frame = _layer_frame('counts')
    frame['cell_type'] = [CELL_TYPES[c % 3] for c in frame['cell_id']]
    frame['gene_name'] = [GENE_NAMES[g] for g in frame['gene_id']]
    expected = frame.groupby(['gene_name', 'cell_type'])['value'].sum()
    for (gene, cell_type), total in expected.items():
        assert int(table.loc[gene, cell_type]) == int(total)


def test_gene_variance_by_facet_on_moved_dataset(serve_client):
    serve_client, db_id = serve_client
    response = serve_client.get(f'/databases/{db_id}/gene-variance-by-facet',
                                params={'facet': 'ant_group', 'top': 5})
    assert response.status_code == 200, response.text[:500]
    assert len(response.json()['genes']) == 5


@pytest.fixture
def renamed_obs_store(moved_store):
    """Publish ``cell_type`` under a renamed public name, as an api
    ``obs_rename`` does: the obscat view carries the public name while
    ``__obscat`` keeps the stored one."""
    con = duckdb.connect(str(moved_store['db']))
    con.execute("CREATE OR REPLACE VIEW obscat AS "
                "SELECT c.cell_name, o.cell_type AS ant_cell_type, o.ant_group "
                "FROM __obscat o JOIN __cells c USING (cell_id)")
    con.close()
    return moved_store


def test_pseudobulk_groups_and_filters_on_public_column_names(renamed_obs_store, serve_client):
    serve_client, db_id = serve_client
    response = serve_client.get(f'/databases/{db_id}/pseudobulk',
                                params={'group_by': 'ant_cell_type', 'layer': 'counts',
                                        'subset': 'ant_group:g0',
                                        'min_cells': 1, 'format': 'csv'})
    assert response.status_code == 200, response.text[:500]
    table = pd.read_csv(pd.io.common.StringIO(response.text), comment='#').set_index('gene_name')
    frame = _layer_frame('counts')
    frame = frame[frame['cell_id'] % 2 == 0]
    frame['cell_type'] = [CELL_TYPES[c % 3] for c in frame['cell_id']]
    frame['gene_name'] = [GENE_NAMES[g] for g in frame['gene_id']]
    expected = frame.groupby(['gene_name', 'cell_type'])['value'].sum()
    for (gene, cell_type), total in expected.items():
        assert int(table.loc[gene, cell_type]) == int(total)


@pytest.fixture
def ant_sex_store(moved_store):
    """Expose a canonical ``ant_sex`` column so ``sex`` aliases to it."""
    con = duckdb.connect(str(moved_store['db']))
    con.execute("CREATE OR REPLACE VIEW obscat AS "
                "SELECT c.cell_name, o.cell_type, o.ant_group AS ant_sex "
                "FROM __obscat o JOIN __cells c USING (cell_id)")
    con.close()
    return moved_store


def test_deprecated_alias_header_survives_the_plot_cache(ant_sex_store, serve_client):
    serve_client, db_id = serve_client
    params = {'x': 'sex', 'y': 'APOE', 'format': 'png'}
    first = serve_client.get(f'/databases/{db_id}/plot/violin', params=params)
    second = serve_client.get(f'/databases/{db_id}/plot/violin', params=params)
    assert first.status_code == 200 and second.status_code == 200, first.text[:300]
    assert second.headers.get('X-Plot-Cache') == 'HIT'
    for response in (first, second):
        assert 'ant_sex' in response.headers.get('X-Anthive-Deprecated-Alias', '')
