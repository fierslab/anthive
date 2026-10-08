"""Cell type scoped module scoring (2026-10-08).

Covers the geneset registry rules for ``celltypes`` / ``role``, the scope
resolution (``lib/celltype_scope.py``), the sidecar delete, ``score_module``
on a cell subset, and the scoring tools ``ant-score-ext`` and ``ant-ucell``
end to end on a small synthetic store with three cell type groups
(microglia, astrocyte, one unmapped label).

Run with ``make test`` (or ``PYTHONPATH=lib python -m pytest tests/test_celltype_scope.py``).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))

from anthive_db import AnthiveDB  # noqa: E402
import celltype_scope  # noqa: E402
from geneset import GenesetRegistry  # noqa: E402
from h5adstore import H5adEntry  # noqa: E402
from scores_parquet import delete_scores, read_scores, scores_path, upsert_scores  # noqa: E402


def load_tool(name: str):
    """Import ``bin/annot/<name>`` (hyphenated script) as a module."""
    path = REPO_ROOT / 'bin' / 'annot' / name
    loader = importlib.machinery.SourceFileLoader(name.replace('-', '_'), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Geneset registry rules
# ---------------------------------------------------------------------------

def write_experiment(root: Path, study: str, experiment: str, scope_lines: str, genesets=None) -> Path:
    """Write an experiment folder with an index.md (scope_lines appended to the front matter)."""
    folder = root / study / experiment
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'index.md').write_text(f'---\norganism: human\nmethod: manual\n{scope_lines}---\n')
    for name, genes in (genesets or {'m1': ['A', 'B', 'C', 'D', 'E', 'F']}).items():
        (folder / f'{name}.grp').write_text('\n'.join(genes) + '\n')
    return folder


def scan(root: Path) -> GenesetRegistry:
    registry = GenesetRegistry(root)
    registry.scan()
    return registry


@pytest.mark.parametrize('scope_lines, fragment', [
    ('', "missing required field 'celltypes'"),
    ('celltypes: []\n', "'celltypes' is empty"),
    ('celltypes:\n- all\n- astrocyte\n', "mixes 'all'"),
    ('celltypes:\n- ALL\n', "spelled 'all'"),
    ('celltypes:\n- not a cell type\n', 'not in the cell_type vocabulary'),
    ('celltypes:\n- dendritic cell\n', 'maps to no cell type group'),
    ('celltypes:\n- all\nrole: qc\n', "role 'qc' is not one of"),
])
def test_registry_refuses_an_unusable_celltypes_declaration(tmp_path, scope_lines, fragment):
    write_experiment(tmp_path, 'S', 'E', scope_lines)
    registry = scan(tmp_path)
    assert any(fragment in error for error in registry.errors), registry.errors
    assert 'S/E' not in registry.experiments


@pytest.mark.parametrize('scope_lines', ['celltypes:\n- all\n', 'celltypes: [microglial cell, astrocyte]\n',
                                         'celltypes: astrocyte\n', 'celltypes:\n- all\nrole: markers\n'])
def test_registry_accepts_valid_declarations(tmp_path, scope_lines):
    write_experiment(tmp_path, 'S', 'E', scope_lines)
    registry = scan(tmp_path)
    assert not registry.errors, registry.errors
    assert 'S/E' in registry.experiments


def test_geneset_md_may_override_celltypes_and_role_and_is_validated(tmp_path):
    folder = write_experiment(tmp_path, 'S', 'E', 'celltypes:\n- microglial cell\n',
                              genesets={'plain': list('ABCDEF'), 'special': list('ABCDEF'), 'qc': list('ABCDEF')})
    (folder / 'special.md').write_text('---\ncelltypes:\n- astrocyte\n---\n')
    (folder / 'qc.md').write_text('---\nrole: markers\ncelltypes:\n- all\n---\n')
    registry = scan(tmp_path)
    assert not registry.errors, registry.errors
    plain, special, qc = (registry.get(f'S/E/{name}') for name in ('plain', 'special', 'qc'))
    assert plain.celltypes == ['microglial cell'] and plain.role is None
    assert special.celltypes == ['astrocyte'] and special.experiment.celltypes == ['microglial cell']
    assert qc.role == 'markers' and qc.celltypes == ['all']
    assert special.to_summary()['celltypes'] == ['astrocyte'] and qc.to_summary()['role'] == 'markers'

    (folder / 'bad.grp').write_text('A\nB\nC\n')
    (folder / 'bad.md').write_text('---\nrole: nonsense\n---\n')
    assert any('role' in error for error in scan(tmp_path).errors)


def test_the_live_store_layout_decisions_hold():
    """The shipped genesetstore (when present) declares celltypes everywhere; markers panels are flagged."""
    store = REPO_ROOT / 'data' / 'genesetstore'
    if not store.exists():
        pytest.skip('no genesetstore checkout')
    registry = scan(store.resolve())
    assert not registry.errors, registry.errors
    markers = {e.experiment_id for e in registry.experiments.values() if e.role == 'markers'}
    assert markers == {'LuChen2025/nbb_celltypes', 'PanglaoDB2019/Glia_markers',
                       'PanglaoDB2019/Neuron_markers', 'PanglaoDB2019/Vasculature_markers'}
    assert registry.get_experiment('GWAS0000/GWAS').celltypes == ['all']
    assert registry.get_experiment('Mancuso2024/Cell_states').celltypes == ['microglial cell']


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------

def test_scope_rules():
    micro = celltype_scope.scope_from_labels(['microglial cell'])
    assert micro.groups == frozenset({'microglia'}) and not micro.applies_everywhere
    assert micro.matches_label('Microglia') and micro.matches_label('Mic P2RY12')
    assert not micro.matches_label('astrocyte') and not micro.matches_label(None)
    everything = celltype_scope.scope_from_labels(['all'])
    assert everything.applies_everywhere and everything.matches_label('whatever') and everything.matches_group(None)
    panel = celltype_scope.scope_from_labels(['all'], role='markers')
    assert panel.is_markers and panel.applies_everywhere
    panel_with_labels = celltype_scope.scope_from_labels(['astrocyte'], role='markers')
    assert panel_with_labels.applies_everywhere             # markers are scored on all cells
    both = celltype_scope.scope_from_labels(['microglial cell', 'astrocyte'])
    assert both.groups == frozenset({'microglia', 'astrocyte'})
    assert micro.key() != both.key() != everything.key()
    groups = pd.Series(['microglia', 'astrocyte', None, 'unmapped'])
    assert micro.cell_mask(groups).tolist() == [True, False, False, False]
    assert everything.cell_mask(groups).tolist() == [True] * 4


# ---------------------------------------------------------------------------
# Synthetic dataset
# ---------------------------------------------------------------------------

N_GENES = 60
LABELS = ['microglial cell'] * 20 + ['astrocyte'] * 10 + ['Weird label'] * 10     # 40 cells


def build_dataset(tmp_path: Path, labels=LABELS, celltype_in_obs: bool = True, mus_text: str = 'organism=human\n'):
    """Synthetic store: duckdb (cells, genes, legacy __layer__X table, gene bins, obs) + md + mus."""
    store = tmp_path / 'store'
    group_dir = store / 'Syn2026'
    group_dir.mkdir(parents=True)
    (group_dir / 'index.md').write_text('---\ngroup: Syn2026\ngroup_id: SY1\n---\n')
    rng = np.random.default_rng(3)
    n_cells = len(labels)
    cell_names = [f'cell{i:02d}' for i in range(n_cells)]
    base = rng.uniform(0.5, 4.0, size=N_GENES)
    expression = rng.poisson(base * np.where(np.array(labels)[:, None] == 'astrocyte', 1.5, 1.0)).astype(float)
    triples = [(cell, gene, expression[cell, gene]) for cell in range(n_cells) for gene in range(N_GENES)
               if expression[cell, gene] > 0]
    database_path = group_dir / 'Syn.duckdb'
    conn = duckdb.connect(str(database_path))
    conn.execute('CREATE TABLE __cells AS SELECT * FROM (VALUES ' + ','.join(
        f"({i}, '{name}')" for i, name in enumerate(cell_names)) + ') t(cell_id, cell_name)')
    conn.execute('CREATE TABLE __genes AS SELECT * FROM (VALUES ' + ','.join(
        f"({i}, 'GENE{i}')" for i in range(N_GENES)) + ') t(gene_id, gene_name)')
    conn.register('triples_df', pd.DataFrame(triples, columns=['cell_id', 'gene_id', 'value']))
    conn.execute('CREATE TABLE __layer__X AS SELECT * FROM triples_df')
    conn.execute("""CREATE TABLE __gene_bins AS
        WITH gene_means AS (SELECT g.gene_id, COALESCE(AVG(l.value), 0.0) AS mean_expr
                            FROM __genes g LEFT JOIN __layer__X l USING (gene_id) GROUP BY g.gene_id)
        SELECT gene_id, mean_expr, NTILE(25) OVER (ORDER BY mean_expr) AS expr_bin FROM gene_means""")
    obscat = pd.DataFrame({'cell_id': range(n_cells), 'celltype': labels, 'donor': 'D1'})
    if not celltype_in_obs:
        obscat = obscat.drop(columns='celltype')
    conn.register('obscat_df', obscat)
    conn.execute('CREATE TABLE __obscat AS SELECT * FROM obscat_df')
    conn.execute('CREATE TABLE __obsnum AS SELECT cell_id FROM __cells')
    conn.close()
    rename = '    celltype: ant_cell_type\n' if celltype_in_obs else ''
    (group_dir / 'Syn.md').write_text(
        '---\nid: SYN\ntitle: Syn\nversion: 0.1.0\nsha256: deadbeef\ndefault_layer: X\n'
        f'api:\n  obs_rename:\n{rename}    donor: ant_donor\n---\n' if celltype_in_obs else
        '---\nid: SYN\ntitle: Syn\nversion: 0.1.0\nsha256: deadbeef\ndefault_layer: X\n---\n')
    (group_dir / 'Syn.h5ad.mus').write_text(mus_text)
    entry = H5adEntry(db_id='SY1/SYN', md_path=group_dir / 'Syn.md', h5ad_path=group_dir / 'Syn.h5ad',
                      duckdb_path=database_path, group_id='SY1', file_id='SYN', study='Syn2026', stem='Syn')
    return store, entry, cell_names, expression


def build_genesetstore(tmp_path: Path) -> Path:
    """Three catalogs: microglia-only, everywhere, markers panel; GENE0..5 / GENE10..15 / GENE20..25."""
    root = tmp_path / 'genesetstore'
    for experiment, scope, genes in (('Micro', 'celltypes:\n- microglial cell\n', range(0, 6)),
                                     ('Everywhere', 'celltypes:\n- all\n', range(10, 16)),
                                     ('Panel', 'celltypes:\n- all\nrole: markers\n', range(20, 26))):
        write_experiment(root, 'Syn', experiment, scope, {'mod': [f'GENE{g}' for g in genes]})
    return root


# ---------------------------------------------------------------------------
# Per-cell cell type labels
# ---------------------------------------------------------------------------

def test_cell_type_groups_read_the_obs_mapping(tmp_path):
    store, entry, cell_names, _ = build_dataset(tmp_path)
    cells = celltype_scope.cell_type_groups(entry, store)
    assert cells['cell_id'].tolist() == list(range(40)) and cells['cell_name'].tolist() == cell_names
    assert celltype_scope.group_summary(cells) == {'microglia': 20, 'astrocyte': 10, 'unmapped': 10}
    scope = celltype_scope.scope_from_labels(['astrocyte'])
    assert celltype_scope.scope_cell_ids(scope, cells).tolist() == list(range(20, 30))
    assert len(celltype_scope.scope_cell_ids(celltype_scope.scope_from_labels(['all']), cells)) == 40


def test_cell_type_groups_fall_back_to_the_mus_constant(tmp_path):
    store, entry, _, _ = build_dataset(tmp_path, celltype_in_obs=False,
                                       mus_text='organism=human\ncell_type=astrocyte\n')
    cells = celltype_scope.cell_type_groups(entry, store)
    assert celltype_scope.group_summary(cells) == {'astrocyte': 40}


def test_cell_type_groups_without_any_information_match_all_scope_only(tmp_path):
    store, entry, _, _ = build_dataset(tmp_path, celltype_in_obs=False)
    cells = celltype_scope.cell_type_groups(entry, store)
    assert celltype_scope.group_summary(cells) == {'none': 40}
    assert celltype_scope.scope_from_labels(['microglial cell']).cell_mask(cells['group']).sum() == 0
    assert celltype_scope.scope_from_labels(['all']).cell_mask(cells['group']).all()


# ---------------------------------------------------------------------------
# Sidecar delete
# ---------------------------------------------------------------------------

def sidecar_rows(catalog: str, source: str = 'geneset', cells=(0, 1)):
    return pd.DataFrame({'cell_id': list(cells), 'module': 'm', 'score': 0.5, 'catalog': catalog, 'source': source})


def test_delete_scores_removes_one_catalog_and_the_empty_sidecar(tmp_path):
    database = tmp_path / 'x.duckdb'
    assert delete_scores(database, 'geneset', 'A/B') == 0                           # no sidecar: no-op
    upsert_scores(database, 'geneset', 'A/B', sidecar_rows('A/B'))
    upsert_scores(database, 'ucell', 'A/B', sidecar_rows('A/B', 'ucell'))
    upsert_scores(database, 'geneset', 'C/D', sidecar_rows('C/D'))
    assert delete_scores(database, 'geneset', 'A/B') == 2
    remaining = read_scores(database)
    assert sorted(zip(remaining['source'], remaining['catalog'])) == [('geneset', 'C/D'), ('geneset', 'C/D'),
                                                                      ('ucell', 'A/B'), ('ucell', 'A/B')]
    assert delete_scores(database, 'geneset', 'A/B') == 0                           # already gone
    delete_scores(database, 'ucell', 'A/B')
    delete_scores(database, 'geneset', 'C/D')
    assert not scores_path(database).exists()                                       # last rows gone: file removed
    assert not list(tmp_path.glob('*.tmp'))


# ---------------------------------------------------------------------------
# score_module on a cell subset
# ---------------------------------------------------------------------------

def test_score_module_on_a_subset_scores_exactly_those_cells(tmp_path):
    store, entry, cell_names, _ = build_dataset(tmp_path)
    genes = [f'GENE{g}' for g in range(0, 6)]
    with AnthiveDB(str(entry.duckdb_path)) as db:
        full, _ = db.score_module(genes, seed=7)
        subset_ids = list(range(0, 20))
        subset, meta = db.score_module(genes, seed=7, cell_ids=subset_ids)
        again, _ = db.score_module(genes, seed=7, cell_ids=subset_ids)
        everyone, _ = db.score_module(genes, seed=7, cell_ids=list(range(40)))
        with pytest.raises(ValueError, match='no cell to score'):
            db.score_module(genes, cell_ids=[])
    assert subset['cell_name'].tolist() == cell_names[:20] and len(full) == 40
    assert subset['module_score'].tolist() == again['module_score'].tolist()        # reproducible, cached bins
    np.testing.assert_allclose(everyone['module_score'], full['module_score'])      # all cells = stored bins
    assert meta['n_control_genes'] > 0


def test_score_module_subset_bins_come_from_the_subset_cells(tmp_path):
    """Genes ranked by expression in the subset, not in the whole dataset, define the control bins."""
    store, entry, _, _ = build_dataset(tmp_path)
    genes = ['GENE0', 'GENE1', 'GENE2']
    with AnthiveDB(str(entry.duckdb_path)) as db:
        db.score_module(genes, cell_ids=list(range(20, 30)))
        tables = [row[0] for row in db._conn.execute("SELECT table_name FROM duckdb_tables() WHERE temporary").fetchall()]
        bins_table = next(name for name in tables if name.startswith('__score_bins_'))
        subset_bins = db._conn.execute(f'SELECT gene_id, expr_bin FROM {bins_table} ORDER BY gene_id').df()
        expected = db._conn.execute("""
            SELECT gene_id, NTILE(25) OVER (ORDER BY mean_expr, gene_id) AS expr_bin FROM (
                SELECT g.gene_id, COALESCE(AVG(l.value), 0.0) AS mean_expr FROM __genes g
                LEFT JOIN (SELECT * FROM __layer__X WHERE cell_id BETWEEN 20 AND 29) l USING (gene_id)
                GROUP BY g.gene_id)""").df()
        stored = db._conn.execute('SELECT gene_id, expr_bin FROM __gene_bins ORDER BY gene_id').df()
    assert not (stored['expr_bin'].to_numpy() == subset_bins['expr_bin'].to_numpy()).all()   # not the whole-dataset bins
    merged = subset_bins.merge(expected[['gene_id', 'expr_bin']], on='gene_id', suffixes=('', '_expected'))
    assert (merged['expr_bin'] == merged['expr_bin_expected']).all()


# ---------------------------------------------------------------------------
# ant-score-ext end to end
# ---------------------------------------------------------------------------

def run_score_ext(store: Path, geneset_root: Path, *arguments: str, monkeypatch) -> int:
    tool = load_tool('ant-score-ext')
    monkeypatch.setattr(sys, 'argv', ['ant-score-ext', *arguments, '--h5adstore-root', str(store),
                                      '--genesetstore-root', str(geneset_root)])
    return tool.main()


def test_ant_score_ext_scores_each_module_on_its_cell_type_scope_only(tmp_path, monkeypatch):
    store, entry, _, _ = build_dataset(tmp_path)
    geneset_root = build_genesetstore(tmp_path)
    assert run_score_ext(store, geneset_root, 'SY1/SYN', 'Syn/Micro', 'Syn/Everywhere', 'Syn/Panel',
                         monkeypatch=monkeypatch) == 0
    scores = read_scores(entry.duckdb_path)
    by_catalog = {catalog: set(frame['cell_id']) for catalog, frame in scores.groupby('catalog')}
    assert by_catalog['Syn/Micro'] == set(range(0, 20))                  # microglia cells only
    assert by_catalog['Syn/Everywhere'] == set(range(40))                # every cell, also the unmapped label
    assert by_catalog['Syn/Panel'] == set(range(40))                     # markers: every cell
    assert set(scores['source']) == {'geneset'} and scores['score'].notna().all()


def test_ant_score_ext_removes_stale_rows_when_no_cell_matches(tmp_path, monkeypatch):
    store, entry, _, _ = build_dataset(tmp_path, labels=['astrocyte'] * 40)
    geneset_root = build_genesetstore(tmp_path)
    upsert_scores(entry.duckdb_path, 'geneset', 'Syn/Micro', sidecar_rows('Syn/Micro'))
    assert run_score_ext(store, geneset_root, 'SY1/SYN', 'Syn/Micro', 'Syn/Everywhere', monkeypatch=monkeypatch) == 0
    scores = read_scores(entry.duckdb_path)
    assert set(scores['catalog']) == {'Syn/Everywhere'}                  # the stale microglia rows are gone


def test_ant_score_ext_refuses_a_store_without_celltypes(tmp_path, monkeypatch):
    store, entry, _, _ = build_dataset(tmp_path)
    geneset_root = tmp_path / 'broken'
    write_experiment(geneset_root, 'Syn', 'Micro', '', {'mod': ['GENE0', 'GENE1', 'GENE2']})
    assert run_score_ext(store, geneset_root, 'SY1/SYN', 'Syn/Micro', monkeypatch=monkeypatch) == 1
    assert not scores_path(entry.duckdb_path).exists()


def test_ant_score_ext_scores_all_cells_without_stored_gene_bins(tmp_path, monkeypatch):
    store, entry, _, _ = build_dataset(tmp_path)
    conn = duckdb.connect(str(entry.duckdb_path))
    conn.execute('DROP TABLE __gene_bins')
    conn.close()
    geneset_root = build_genesetstore(tmp_path)
    assert run_score_ext(store, geneset_root, 'SY1/SYN', 'Syn/Everywhere', monkeypatch=monkeypatch) == 0
    assert set(read_scores(entry.duckdb_path)['cell_id']) == set(range(40))


# ---------------------------------------------------------------------------
# ant-ucell
# ---------------------------------------------------------------------------

def test_ant_ucell_writes_rows_for_the_scope_cells_only(tmp_path):
    anndata = pytest.importorskip('anndata')
    tool = load_tool('ant-ucell')
    store, entry, cell_names, expression = build_dataset(tmp_path)
    geneset_root = build_genesetstore(tmp_path)
    registry = scan(geneset_root)
    cells = celltype_scope.cell_type_groups(entry, store)
    adata = anndata.AnnData(X=expression.astype('float32'),
                            obs=pd.DataFrame(index=cell_names),
                            var=pd.DataFrame(index=[f'GENE{i}' for i in range(N_GENES)]))
    for experiment_id, expected_cells in (('Syn/Micro', set(range(0, 20))), ('Syn/Everywhere', set(range(40))),
                                          ('Syn/Panel', set(range(40)))):
        members = [g for g in registry.list_genesets() if g.experiment.experiment_id == experiment_id]
        result = tool._ucell_one_experiment(adata, registry.get_experiment(experiment_id), members, entry,
                                            min_n=3, verbose=False, dry_run=False, cells=cells)
        assert result['status'] == 'ok', result
        rows = read_scores(entry.duckdb_path, source='ucell', catalog=experiment_id)
        assert set(rows['cell_id']) == expected_cells and rows['score'].between(0, 1).all()


def test_ant_ucell_score_of_a_cell_does_not_depend_on_the_other_cells(tmp_path):
    """UCell ranks genes within a cell: scoring a subset gives the same values as scoring everyone."""
    anndata = pytest.importorskip('anndata')
    tool = load_tool('ant-ucell')
    store, entry, cell_names, expression = build_dataset(tmp_path)
    adata = anndata.AnnData(X=expression.astype('float32'), obs=pd.DataFrame(index=cell_names),
                            var=pd.DataFrame(index=[f'GENE{i}' for i in range(N_GENES)]))
    sets = {'mod': [f'GENE{g}' for g in range(6)]}
    everyone = tool._run_ucell(adata, sets)
    subset = tool._run_ucell(adata[:20], sets)
    np.testing.assert_allclose(subset['mod'].to_numpy(), everyone['mod'].to_numpy()[:20])


def test_ant_ucell_removes_stale_rows_when_no_cell_matches(tmp_path):
    anndata = pytest.importorskip('anndata')
    tool = load_tool('ant-ucell')
    store, entry, cell_names, expression = build_dataset(tmp_path, labels=['astrocyte'] * 40)
    geneset_root = build_genesetstore(tmp_path)
    registry = scan(geneset_root)
    upsert_scores(entry.duckdb_path, 'ucell', 'Syn/Micro', sidecar_rows('Syn/Micro', 'ucell'))
    cells = celltype_scope.cell_type_groups(entry, store)
    adata = anndata.AnnData(X=expression.astype('float32'), obs=pd.DataFrame(index=cell_names),
                            var=pd.DataFrame(index=[f'GENE{i}' for i in range(N_GENES)]))
    members = [g for g in registry.list_genesets() if g.experiment.experiment_id == 'Syn/Micro']
    result = tool._ucell_one_experiment(adata, registry.get_experiment('Syn/Micro'), members, entry,
                                        min_n=3, verbose=False, dry_run=False, cells=cells)
    assert result['status'] == 'error' and 'cell type scope' in result['error']
    assert not scores_path(entry.duckdb_path).exists()


# ---------------------------------------------------------------------------
# ant-score (scanpy, obs columns)
# ---------------------------------------------------------------------------

def test_ant_score_scores_a_restricted_geneset_on_a_copy_of_the_matching_cells(tmp_path):
    anndata = pytest.importorskip('anndata')
    pytest.importorskip('scanpy')
    tool = load_tool('ant-score')
    store, entry, cell_names, expression = build_dataset(tmp_path)
    registry = scan(build_genesetstore(tmp_path))
    adata = anndata.AnnData(X=np.log1p(expression).astype('float32'), obs=pd.DataFrame(index=cell_names),
                            var=pd.DataFrame(index=[f'GENE{i}' for i in range(N_GENES)]))
    cells = celltype_scope.cell_type_groups(entry, store)
    groups = pd.Series(cells['group'].to_numpy(), index=adata.obs_names)
    args = SimpleNamespace(ctrl_size=5, n_bins=5, seed=0, layer=None, use_raw=False, verbose=False)
    experiment = registry.get_experiment('Syn/Micro')
    members = [g for g in registry.list_genesets() if g.experiment is experiment]
    record = tool._score_experiment(adata, experiment, members, tmp_path, args, cell_groups=groups)
    column = 'Syn::Micro::mod'
    assert column in record['columns']
    scored = adata.obs[column]
    assert scored.iloc[:20].notna().all() and scored.iloc[20:].isna().all()          # NULL outside the scope
    reference = adata[:20].copy()
    tool.sc.tl.score_genes(reference, gene_list=[f'GENE{g}' for g in range(6)], ctrl_size=5, n_bins=5,
                           score_name='ref', random_state=0)
    np.testing.assert_allclose(scored.iloc[:20].to_numpy(), reference.obs['ref'].to_numpy())
    assert record['per_geneset'][0]['summary']['n_scored'] == 20


# ---------------------------------------------------------------------------
# ant-score-fleet
# ---------------------------------------------------------------------------

def test_fleet_cpu_split_and_plan(tmp_path):
    fleet = load_tool('ant-score-fleet')
    assert fleet.Fleet._split_cpus('4-11', 2) == ['4-7', '8-11']
    assert fleet.Fleet._split_cpus('4-11', 1) == ['4-11']
    assert fleet.Fleet._split_cpus('0-4', 2) == ['0-1', '2-4']

    store, entry, _, _ = build_dataset(tmp_path)
    (entry.md_path.parent / 'Syn.h5ad.mus').write_text(
        'organism=human\nexperimental_system=post_mortem_tissue\n')
    registry = scan(build_genesetstore(tmp_path))
    record = fleet.plan_dataset(entry, registry, store, fleet._load_gene_tool())
    assert record['skip_reason'] is None, record
    assert sorted(record['experiments']) == ['Syn/Everywhere', 'Syn/Micro', 'Syn/Panel']
    assert record['detail']['Syn/Micro']['cells'] == 20 and record['detail']['Syn/Everywhere']['cells'] == 40
    assert record['groups'] == {'microglia': 20, 'astrocyte': 10, 'unmapped': 10}


# ---------------------------------------------------------------------------
# ant-facet-test-modules defaults, fleet catalogs filter, ingest check
# ---------------------------------------------------------------------------

def test_module_facet_tests_default_to_scanpy_and_ucell():
    assert load_tool('ant-facet-test-modules').DEFAULT_SOURCES == ('geneset', 'ucell')


def test_applicable_experiments_counts_modules_and_cells(tmp_path):
    store, entry, _, _ = build_dataset(tmp_path)
    registry = scan(build_genesetstore(tmp_path))
    cells = celltype_scope.cell_type_groups(entry, store)
    applicable = celltype_scope.applicable_experiments(registry, cells['group'], ['human'])
    assert applicable == {'Syn/Micro': {'modules': 1, 'of': 1, 'cells': 20},
                          'Syn/Everywhere': {'modules': 1, 'of': 1, 'cells': 40},
                          'Syn/Panel': {'modules': 1, 'of': 1, 'cells': 40}}
    assert celltype_scope.applicable_experiments(registry, cells['group'], ['mouse']) == {}
    only_astro = celltype_scope.applicable_experiments(registry, pd.Series(['astrocyte'] * 5), ['human'])
    assert set(only_astro) == {'Syn/Everywhere', 'Syn/Panel'}


def test_fleet_catalogs_filter_limits_the_plan(tmp_path, monkeypatch):
    fleet = load_tool('ant-score-fleet')
    store, entry, _, _ = build_dataset(tmp_path)
    (entry.md_path.parent / 'Syn.h5ad.mus').write_text('organism=human\nexperimental_system=post_mortem_tissue\n')
    geneset_root = build_genesetstore(tmp_path)
    arguments = SimpleNamespace(h5adstore_root=str(store), genesetstore_root=str(geneset_root), only='SY1/SYN',
                                catalogs='Syn/Micro')
    plan = fleet.build_plan(arguments)
    assert [record['experiments'] for record in plan] == [['Syn/Micro']] and plan[0]['stale'] == []
    arguments.catalogs = 'Other/Catalog'
    assert fleet.build_plan(arguments)[0]['skip_reason'] == 'none of the requested catalogs applies'


def make_check_context(entry, store, **overrides):
    values = dict(md={}, annotation={'organism': 'human', 'experimental_system': 'post_mortem_tissue'}, api={},
                  db_id=entry.db_id, duckdb=entry.duckdb_path, md_path=entry.md_path, h5ad_path=entry.h5ad_path,
                  group_dir=entry.md_path.parent, group_id=entry.group_id, file_id=entry.file_id,
                  basename=entry.stem, store_root=store)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_scores_check_reports_missing_sets_and_passes_when_complete(tmp_path, monkeypatch):
    from ingest_check import PASS, SKIP, WARN
    from ingest_check import checks_scores
    store, entry, _, _ = build_dataset(tmp_path)
    registry = scan(build_genesetstore(tmp_path))
    monkeypatch.setattr(celltype_scope, 'default_registry', lambda: registry)
    ctx = make_check_context(entry, store)
    outcome = checks_scores.scores_modules_complete(ctx)
    assert outcome.status == WARN and 'no scores sidecar' in outcome.summary
    for source in ('geneset', 'ucell'):
        for catalog in ('Syn/Micro', 'Syn/Everywhere'):
            upsert_scores(entry.duckdb_path, source, catalog, sidecar_rows(catalog, source))
    outcome = checks_scores.scores_modules_complete(ctx)
    assert outcome.status == WARN and set(outcome.details['missing']) == {'geneset::Syn/Panel', 'ucell::Syn/Panel'}
    for source in ('geneset', 'ucell'):
        upsert_scores(entry.duckdb_path, source, 'Syn/Panel', sidecar_rows('Syn/Panel', source))
    assert checks_scores.scores_modules_complete(ctx).status == PASS
    secondary = make_check_context(entry, store, md={'is_secondary': True})
    assert checks_scores.scores_modules_complete(secondary).status == SKIP
    mouse = make_check_context(entry, store, annotation={'organism': 'mouse'})
    assert checks_scores.scores_modules_complete(mouse).status == SKIP


def test_missing_scores_lists_source_catalog_pairs():
    from ingest_check import checks_scores
    applicable = {'A/B': {'modules': 1, 'of': 1, 'cells': 3}, 'C/D': {'modules': 1, 'of': 1, 'cells': 3}}
    present = {('geneset', 'A/B'), ('ucell', 'A/B'), ('geneset', 'C/D')}
    assert checks_scores.missing_scores(applicable, present) == ['ucell::C/D']
    assert checks_scores.missing_scores(applicable, present, sources=('geneset',)) == []


def test_ant_ucell_ranks_the_cells_of_a_scope_once_for_all_catalogs(tmp_path, monkeypatch):
    """Ranking dominates the cost: catalogs sharing a scope are scored in one pass, with the same values."""
    anndata = pytest.importorskip('anndata')
    tool = load_tool('ant-ucell')
    store, entry, cell_names, expression = build_dataset(tmp_path)
    registry = scan(build_genesetstore(tmp_path))
    cells = celltype_scope.cell_type_groups(entry, store)
    adata = anndata.AnnData(X=expression.astype('float32'), obs=pd.DataFrame(index=cell_names),
                            var=pd.DataFrame(index=[f'GENE{i}' for i in range(N_GENES)]))
    pairs = [(registry.get_experiment(name), [g for g in registry.list_genesets() if g.experiment.experiment_id == name])
             for name in ('Syn/Micro', 'Syn/Everywhere', 'Syn/Panel')]
    calls = []
    original = tool._run_ucell
    monkeypatch.setattr(tool, '_run_ucell', lambda data, sets: calls.append((data.n_obs, len(sets))) or original(data, sets))
    results = tool._ucell_experiments(adata, pairs, entry, 3, False, False, cells)
    assert [r['status'] for r in results] == ['ok', 'ok', 'ok']
    assert sorted(calls) == [(20, 1), (40, 2)]            # microglia once; all cells once for Everywhere + Panel
    joint = read_scores(entry.duckdb_path, source='ucell')
    for name in ('Syn/Micro', 'Syn/Everywhere', 'Syn/Panel'):
        alone = original(adata[celltype_scope.module_scope(
            registry.genesets[f'{name}/mod']).cell_mask(cells['group']).to_numpy()],
            {'mod': [f'GENE{g}' for g in range({'Syn/Micro': 0, 'Syn/Everywhere': 10, 'Syn/Panel': 20}[name],
                                                {'Syn/Micro': 6, 'Syn/Everywhere': 16, 'Syn/Panel': 26}[name])]})
        stored = joint[joint['catalog'] == name].sort_values('cell_id')['score'].to_numpy()
        np.testing.assert_allclose(stored, alone['mod'].to_numpy())
