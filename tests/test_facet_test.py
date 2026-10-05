"""Tests for ``bin/annot/ant-facet-test`` (donor-level facet testing, v1).

Covers, on a tiny synthetic dataset built like ant-convert output
(``__cells``, ``__genes``, ``__obscat``, ``__obsnum`` + a counts by_gene
sidecar + ``.md`` / ``.h5ad.mus``):

* a planted sex effect is recovered with the right sign and size, and null
  genes stay mostly non-significant (needs R with limma + edgeR; skipped
  otherwise),
* n_lt5 units are reported descriptively with p / q suppressed,
* xenograft, mouse, secondary and host-sex datasets are excluded,
* donors with several samples collapse into one pseudobulk sample per
  (donor, cell type) and the summed counts match,
* the current .md mapping (not the stored view) defines the facets,
* level, batch-confounding and normalisation rules.

Run with:
    uv run --with pytest --with duckdb --with pandas --with numpy \
           --with scipy --with pyarrow --with pyyaml --no-project \
           env PYTHONPATH=lib python -m pytest tests/test_facet_test.py -q
Set ANT_FACET_RSCRIPT to an Rscript that has limma and edgeR (default:
envs/facet-test-r of the checkout).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))


def _load_tool():
    path = REPO_ROOT / 'bin' / 'annot' / 'ant-facet-test'
    loader = importlib.machinery.SourceFileLoader('ant_facet_test', str(path))
    spec = importlib.util.spec_from_loader('ant_facet_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


aft = _load_tool()


def _rscript_with_limma() -> str | None:
    candidate = os.environ.get('ANT_FACET_RSCRIPT', aft.DEFAULT_RSCRIPT)
    if not candidate or not Path(candidate).exists():
        return None
    result = subprocess.run([candidate, '-e', 'library(limma); library(edgeR)'],
                            capture_output=True, text=True)
    return candidate if result.returncode == 0 else None


RSCRIPT = _rscript_with_limma()
needs_r = pytest.mark.skipif(RSCRIPT is None, reason='no Rscript with limma + edgeR')

N_GENES = 300
PLANTED_GENE = 'GENE0'
PLANTED_LOG2_FC = 2.0


def make_dataset(tmp_path: Path, n_f: int = 10, n_m: int = 10, organism: str = 'human',
                 system: str = 'post_mortem_tissue', extra_api: str = '',
                 is_secondary: bool = False, samples_per_donor: int = 1,
                 disease_pattern: str | None = None, seed: int = 7, extra_md: str = '',
                 extra_rename: str = ''):
    """Write a synthetic dataset; returns (store_root, H5adEntry).

    Two cell types (T1 with 30 cells per donor, T2 with 4 cells per donor so
    it falls under the 10 cell minimum). GENE0 is up in males by 2 log2 units
    in T1. The author sex column is ``Sex`` with values F / M and the .md
    renames it to ant_sex (the current mapping), so the physical table never
    carries an ant_ name.
    """
    rng = np.random.default_rng(seed)
    store = tmp_path / 'store'
    group_dir = store / 'Syn2026'
    group_dir.mkdir(parents=True)
    (group_dir / 'index.md').write_text('---\ngroup: Syn2026\ngroup_id: SY1\n---\n')
    stem = 'Syn_ds'
    donors = [f'D{i:02d}' for i in range(n_f + n_m)]
    sexes = ['F'] * n_f + ['M'] * n_m
    disease = disease_pattern and disease_pattern.split(',')
    rows = []
    cell_id = 0
    base_mean = rng.uniform(2.0, 12.0, size=N_GENES)
    count_triples = []
    for donor_index, (donor, sex) in enumerate(zip(donors, sexes)):
        donor_factor = np.exp(rng.normal(0, 0.15))
        for cell_type, n_cells in (('T1', 30), ('T2', 4)):
            for cell_index in range(n_cells):
                sample = f'{donor}_s{cell_index % samples_per_donor}'
                rows.append((cell_id, f'{sample}_c{cell_id}', donor, sample, sex, cell_type,
                             disease[donor_index] if disease else 'control'))
                mean_vector = base_mean * donor_factor
                if sex == 'M' and cell_type == 'T1':
                    mean_vector = mean_vector.copy()
                    mean_vector[0] = mean_vector[0] * 2 ** PLANTED_LOG2_FC
                counts = rng.poisson(mean_vector)
                for gene_id in np.nonzero(counts)[0]:
                    count_triples.append((cell_id, int(gene_id), float(counts[gene_id])))
                cell_id += 1
    cells = pd.DataFrame(rows, columns=['cell_id', 'cell_name', 'donor_id', 'sample_id', 'Sex',
                                        'celltype', 'dx'])
    database_path = group_dir / f'{stem}.duckdb'
    conn = duckdb.connect(str(database_path))
    conn.execute('CREATE TABLE __cells AS SELECT cell_id, cell_name FROM cells')
    genes = pd.DataFrame({'gene_id': range(N_GENES),
                          'gene_name': [f'GENE{i}' for i in range(N_GENES)]})
    conn.execute('CREATE TABLE __genes AS SELECT * FROM genes')
    donor_rank = cells['donor_id'].map({d: i for i, d in enumerate(donors)})
    cells['bank'] = np.where(donor_rank % 2 == 0, 'BA', 'BB')      # donor level, crossed with sex
    cells['age'] = 50.0 + 3.0 * donor_rank                          # D00 50 ... D19 107
    obscat = cells[['cell_id', 'donor_id', 'sample_id', 'Sex', 'celltype', 'dx', 'bank']]
    conn.execute('CREATE TABLE __obscat AS SELECT * FROM obscat')
    obsnum = cells[['cell_id', 'age']]
    conn.execute('CREATE TABLE __obsnum AS SELECT * FROM obsnum')
    conn.close()
    triples = pd.DataFrame(count_triples, columns=['cell_id', 'gene_id', 'value'])
    triples['cell_id'] = triples['cell_id'].astype('int32')
    triples['gene_id'] = triples['gene_id'].astype('int32')
    triples['value'] = triples['value'].astype('float32')
    triples.sort_values(['gene_id', 'cell_id']).to_parquet(
        group_dir / f'{stem}.layer.counts.by_gene.parquet', index=False)
    md_text = (
        '---\nid: SYN\ntitle: Syn\nversion: 0.1.0\nsha256: deadbeef\n'
        f'is_secondary: {"true" if is_secondary else "false"}\n'
        'layer_stats:\n  counts:\n    min_nonzero: 1.0\n'
        'api:\n  obs_rename:\n    donor_id: ant_donor\n    Sex: ant_sex\n'
        '    celltype: ant_author_cell_type\n    dx: ant_disease\n' + extra_rename + extra_api +
        '  obs_value_rename:\n    Sex:\n      F: f\n      M: m\n' + extra_md + '---\n')
    (group_dir / f'{stem}.md').write_text(md_text)
    (group_dir / f'{stem}.h5ad.mus').write_text(
        f'organism={organism}\nexperimental_system={system}\n')
    entry = aft.H5adEntry(db_id='SY1/SYN', md_path=group_dir / f'{stem}.md',
                          h5ad_path=group_dir / f'{stem}.h5ad',
                          duckdb_path=database_path, group_id='SY1', file_id='SYN',
                          study='Syn2026', stem=stem)
    return store, entry


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def test_facet_notes_from_md_normalises_names_and_whitespace():
    assert aft.facet_notes_from_md({}) == {}
    assert aft.facet_notes_from_md({'facet_notes': 'nonsense'}) == {}
    notes = aft.facet_notes_from_md({'facet_notes': {'ant_disease': 'a  proxy\n note', 'sex': '', 'x': 5}})
    assert notes == {'disease': 'a proxy note'}


def test_normalisers():
    assert aft.normalize_sex('F') == 'f' and aft.normalize_sex('male') == 'm'
    assert aft.normalize_sex('unknown') is None and aft.normalize_sex(None) is None
    assert aft.normalize_disease('AD') == 'AD' and aft.normalize_disease('control') == 'control'
    for dropped in ('AD_DLBD', 'AD_vascular', 'MCI', 'NCI', 'PD', 'other', None):
        assert aft.normalize_disease(dropped) is None
    assert aft.normalize_apoe4_carrier('E4+') == 'e4_carrier'
    assert aft.normalize_apoe4_carrier('E4-') == 'noncarrier'
    assert aft.derive_apoe4_carrier_from_genotype('E3/E4') == 'e4_carrier'
    assert aft.derive_apoe4_carrier_from_genotype('E4/E4') == 'e4_carrier'
    assert aft.derive_apoe4_carrier_from_genotype('E2/E3') == 'noncarrier'
    assert aft.derive_apoe4_carrier_from_genotype('33') == 'noncarrier'
    assert aft.derive_apoe4_carrier_from_genotype('unknown') is None


@pytest.mark.parametrize('column', ['ant_apoe4_carrier', 'ant_apoe_carrier'])
def test_donor_table_finds_the_apoe_facet_under_the_new_and_the_old_column(column):
    cells = pd.DataFrame({'ant_donor': ['D1', 'D1', 'D2'], column: ['E4+', 'E4+', 'E4-']})
    donors, conflicts = aft.build_donor_table(cells)
    assert dict(zip(donors['donor'], donors['apoe4_carrier'])) == {'D1': 'e4_carrier', 'D2': 'noncarrier'}
    assert conflicts == {}


def test_donor_table_prefers_the_new_column_and_derives_from_the_genotype_otherwise():
    both = pd.DataFrame({'ant_donor': ['D1'], 'ant_apoe4_carrier': ['E4-'], 'ant_apoe_carrier': ['E4+']})
    assert aft.build_donor_table(both)[0]['apoe4_carrier'].tolist() == ['noncarrier']
    genotype = pd.DataFrame({'ant_donor': ['D1'], 'ant_apoe': ['E3/E4']})
    assert aft.build_donor_table(genotype)[0]['apoe4_carrier'].tolist() == ['e4_carrier']


def _donor_table(levels: list[str], batches: list[str] | None = None, facet: str = 'disease'):
    frame = pd.DataFrame({'donor': [f'D{i}' for i in range(len(levels))], facet: levels})
    frame['sex'] = ['f', 'm'] * (len(levels) // 2) + ['f'] * (len(levels) % 2)
    frame['age'] = np.nan
    frame['pmi'] = np.nan
    frame['braak'] = np.nan
    frame['batch'] = batches if batches is not None else None
    return frame


def test_plan_job_drops_level_with_fewer_than_three_donors():
    table = _donor_table(['AD'] * 2 + ['control'] * 8)
    plan = aft.plan_job(table, 'disease', table['donor'])
    assert plan['status'] != 'ok' and 'level' in plan['status']


def test_plan_job_n_lt5_flag():
    table = _donor_table(['AD'] * 4 + ['control'] * 10)
    plan = aft.plan_job(table, 'disease', table['donor'])
    assert plan['status'] == 'ok' and plan['n_lt5'] is True
    table = _donor_table(['AD'] * 5 + ['control'] * 10)
    assert aft.plan_job(table, 'disease', table['donor'])['n_lt5'] is False


def test_plan_job_batch_confounded_is_flagged_and_dropped():
    levels = ['AD'] * 6 + ['control'] * 6
    batches = ['b1'] * 6 + ['b2'] * 6          # batch == disease
    table = _donor_table(levels, batches)
    plan = aft.plan_job(table, 'disease', table['donor'])
    assert plan['batch_confounded'] is True and plan['batch_used'] is False
    mixed = ['b1', 'b2'] * 6                   # batch crossed with disease
    plan = aft.plan_job(_donor_table(levels, mixed), 'disease', table['donor'])
    assert plan['batch_confounded'] is False and plan['batch_used'] is True


def test_plan_job_age_pmi_only_with_15_donors():
    small = _donor_table(['AD'] * 7 + ['control'] * 7)
    small['age'] = np.arange(14.0)
    assert 'age' not in aft.plan_job(small, 'disease', small['donor'])['covariates']
    large = _donor_table(['AD'] * 8 + ['control'] * 8)
    large['age'] = np.arange(16.0) % 7
    plan = aft.plan_job(large, 'disease', large['donor'])
    assert 'age' in plan['covariates'] and 'sex' in plan['covariates']
    sex_plan = aft.plan_job(_donor_table(['f', 'm'] * 6, facet='sex'), 'sex', [f'D{i}' for i in range(12)])
    assert 'sex' not in sex_plan['covariates']


def test_braak_plan_uses_numeric_value():
    table = _donor_table(['control'] * 12)
    table['braak'] = [0, 1, 2, 3, 4, 5, 6, 2, 3, 4, 5, 6]
    plan = aft.plan_job(table, 'braak', table['donor'])
    assert plan['status'] == 'ok' and plan['n_lt5'] is False
    assert plan['design']['facet_value'].max() == 6


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def _eligibility(tmp_path, **kwargs):
    store, entry = make_dataset(tmp_path, **kwargs)
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    annotation = aft.mus.resolve_annotation(entry.h5ad_path, root=store)
    return aft.dataset_eligibility(entry, md_yaml, annotation, aft.get_api_config(md_yaml))


def test_eligible_baseline(tmp_path):
    assert _eligibility(tmp_path) == (True, 'ok')


def test_xenograft_excluded(tmp_path):
    eligible, reason = _eligibility(tmp_path, system='xenograft')
    assert not eligible and 'xenograft' in reason


def test_in_vitro_and_bulk_excluded(tmp_path):
    assert not _eligibility(tmp_path / 'a', system='in_vitro_2d')[0]
    assert not _eligibility(tmp_path / 'b', system='bulk_tissue')[0]


def test_mouse_and_secondary_excluded(tmp_path):
    assert not _eligibility(tmp_path / 'a', organism='mouse')[0]
    assert not _eligibility(tmp_path / 'b', is_secondary=True)[0]


def test_host_sex_facet_excluded(tmp_path):
    eligible, reason = _eligibility(
        tmp_path, extra_api='    sample_id: ant_host_sex\n')
    assert not eligible and 'host' in reason


# ---------------------------------------------------------------------------
# Current mapping, pseudobulk collapse
# ---------------------------------------------------------------------------

def test_views_use_current_md_mapping(tmp_path):
    store, entry = make_dataset(tmp_path)
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    conn = aft.open_dataset(entry, aft.get_api_config(md_yaml), threads=1, memory_limit_gb=1)
    columns = aft.view_columns(conn, 'obscat')
    assert {'ant_donor', 'ant_sex', 'ant_author_cell_type', 'ant_disease'} <= set(columns)
    cells = aft.cell_table(conn, ['ant_donor', 'ant_sex'])
    assert set(cells['ant_sex'].dropna()) == {'f', 'm'}
    conn.close()


def test_multi_sample_donors_collapse_to_one_pseudobulk_sample(tmp_path):
    store, entry = make_dataset(tmp_path, samples_per_donor=3)
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    conn = aft.open_dataset(entry, aft.get_api_config(md_yaml), threads=1, memory_limit_gb=1)
    cells = aft.cell_table(conn, ['ant_donor', 'ant_author_cell_type'])
    t1 = cells[cells['ant_author_cell_type'] == 'T1'].copy()
    t1['donor'] = t1['ant_donor'].astype(str)
    donors = sorted(t1['donor'].unique())
    t1['sample_idx'] = t1['donor'].map({d: i for i, d in enumerate(donors)})
    layer, parquet = aft.counts_layer_for(entry, md_yaml)
    pseudobulk = aft.pseudobulk(conn, parquet, t1[['cell_id', 'sample_idx']])
    assert pseudobulk['sample_idx'].nunique() == len(donors) == 20
    raw = pd.read_parquet(parquet)
    expected = raw[raw['cell_id'].isin(t1['cell_id'])].merge(
        t1[['cell_id', 'sample_idx']], on='cell_id').groupby(['sample_idx', 'gene_id'])['value'].sum()
    got = pseudobulk.set_index(['sample_idx', 'gene_id'])['count']
    assert np.allclose(got.sort_index().to_numpy(), expected.sort_index().to_numpy())
    conn.close()


# ---------------------------------------------------------------------------
# End to end (needs R)
# ---------------------------------------------------------------------------

@needs_r
def test_planted_sex_effect_recovered(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, samples_per_donor=2)
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1,
                                   memory_limit_gb=1)
    assert manifest['status'] == 'done'
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    t1 = table[(table['cell_type'] == 'T1') & (table['facet'] == 'sex')]
    planted = t1[t1['gene'] == PLANTED_GENE].iloc[0]
    assert planted['logFC'] == pytest.approx(PLANTED_LOG2_FC, abs=0.5)
    assert planted['q_value'] < 0.01
    assert planted['ci_low'] < planted['logFC'] < planted['ci_high']
    assert planted['n_donors_ref'] == 10 and planted['n_donors_test'] == 10
    assert planted['contrast'] == 'm_vs_f'
    null_genes = t1[t1['gene'] != PLANTED_GENE]
    assert (null_genes['q_value'] < 0.05).mean() < 0.05
    # T2 has only 4 cells per donor: below the 10 cell minimum, never tested
    assert 'T2' not in set(table['cell_type'])
    assert manifest['pseudobulk']['n_dropped_lt_min_cells'] >= 20
    # manifest carries the reproducibility fields
    for key in ('dataset_sha256', 'seed', 'r_versions', 'parameters', 'anthive4_git'):
        assert key in manifest
    assert json.loads((out_root / 'Syn2026' / 'Syn_ds.facet_run_manifest.json').read_text())['seed']


EXCLUSION_MD = ('facet_donor_exclusions:\n  reason: shared persons\n  obs:\n'
                '    sample_id: [D00_s0, D01_s0, D12_s0]\n')


@needs_r
def test_donor_exclusion_rule_counted_run_and_all_donors_run(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, extra_md=EXCLUSION_MD)
    out_root = tmp_path / 'out'
    counted = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert counted['status'] == 'done'
    exclusion = counted['donor_exclusion']
    assert exclusion['counted'] is True and exclusion['variant'] == 'counted'
    assert (exclusion['n_donors_before'], exclusion['n_donors_excluded'], exclusion['n_donors_after']) == (20, 3, 17)
    assert counted['n_donors'] == 17
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    planted = table[(table['cell_type'] == 'T1') & (table['gene'] == PLANTED_GENE)].iloc[0]
    assert planted['n_donors'] == 17 and planted['n_donors_ref'] == 8 and planted['n_donors_test'] == 9
    # the manifest holds counts, never donor labels
    # (the rule itself is echoed in the manifest; here its values are sample labels, in practice a bank code)
    text = json.dumps({'units': counted['units'], 'keys': counted['donor_keys'],
                       'counts': {k: v for k, v in exclusion.items() if k not in ('rule', 'n_excluded_by_criterion')}})
    assert 'D00' not in text and 'D12' not in text
    digests = [u['donor_key_digests'] for u in counted['units'] if u['status'] == 'ok'][0]
    assert len(digests) == 17
    # full run: every donor, supplementary folder, not counted; the counted files are untouched
    before = (out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet').read_bytes()
    full = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1,
                               all_donors=True)
    assert full['status'] == 'done' and full['donor_exclusion']['counted'] is False
    assert full['n_donors'] == 20 and full['donor_exclusion']['n_donors_excluded'] == 3
    supplementary = out_root / 'Syn2026' / 'supplementary_all_donors'
    assert (supplementary / 'Syn_ds.facet_tests.parquet').exists()
    assert (out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet').read_bytes() == before
    all_table = pd.read_parquet(supplementary / 'Syn_ds.facet_tests.parquet')
    assert all_table[all_table['gene'] == PLANTED_GENE].iloc[0]['n_donors'] == 20


@needs_r
def test_age_floor_applies_to_the_declared_facets_only(tmp_path):
    pattern = ','.join(['AD', 'control'] * 10)
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, disease_pattern=pattern,
                                extra_rename='    age: ant_age\n', extra_md='facet_min_age:\n  disease: 80\n')
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['sex', 'disease'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'done'
    report = manifest['facet_min_age']['disease']
    assert report['floor_years'] == 80.0 and report['n_donors_below_floor'] == 10 and report['n_donors'] == 20
    by_facet = {u['facet']: u for u in manifest['units'] if u['status'] == 'ok'}
    assert by_facet['sex']['n_donors_analysed'] == 20                  # sex keeps every donor
    assert by_facet['disease']['n_donors_analysed'] == 10              # D10..D19 are 80 or older


@needs_r
def test_declared_batch_column_replaces_the_name_guess(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, extra_md='facet_batch_column: bank\n')
    manifest = aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['batch_column'] == 'bank'
    unit = [u for u in manifest['units'] if u['status'] == 'ok'][0]
    assert unit['batch_used'] is True and unit['batch_confounded'] is False


def test_declared_batch_column_must_exist(tmp_path):
    store, entry = make_dataset(tmp_path, extra_md='facet_batch_column: nope\n')
    with pytest.raises(ValueError, match='not an obs column'):
        aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT or 'Rscript', threads=1,
                            memory_limit_gb=1)


@needs_r
def test_all_donors_run_needs_a_rule_and_plain_datasets_are_unchanged(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=8, n_m=8)
    out_root = tmp_path / 'out'
    skipped = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1,
                                  all_donors=True)
    assert skipped['status'] == 'skipped' and 'no facet_donor_exclusions' in skipped['reason']
    plain = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert plain['status'] == 'done' and 'donor_exclusion' not in plain and plain['n_donors'] == 16


def test_malformed_exclusion_rule_is_an_error_not_a_silent_skip(tmp_path):
    store, entry = make_dataset(tmp_path, extra_md='facet_donor_exclusions: nonsense\n')
    with pytest.raises(ValueError, match='must be a mapping'):
        aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT or 'Rscript', threads=1,
                            memory_limit_gb=1)


def test_unknown_exclusion_column_fails_loudly(tmp_path):
    store, entry = make_dataset(tmp_path, extra_md='facet_donor_exclusions:\n  obs:\n    no_such_col: [x]\n')
    with pytest.raises(ValueError, match='not an obs column'):
        aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT or 'Rscript', threads=1,
                            memory_limit_gb=1)


@needs_r
def test_n_lt5_units_suppress_p_and_q(tmp_path):
    # 3 AD vs 12 control donors: tested (>= 3 per level) but descriptive only
    pattern = ','.join(['AD'] * 3 + ['control'] * 12)
    store, entry = make_dataset(tmp_path, n_f=8, n_m=7, disease_pattern=pattern)
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['disease'], RSCRIPT, threads=1,
                                   memory_limit_gb=1)
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    disease = table[table['facet'] == 'disease']
    assert len(disease) > 0
    assert disease['n_lt5'].all() and disease['descriptive_only'].all()
    for column in ('p_value', 'q_value', 'ci_low', 'ci_high', 't'):
        assert disease[column].isna().all()
    assert disease['logFC'].notna().all()
    assert disease['provisional'].all()
    unit = [u for u in manifest['units'] if u['facet'] == 'disease'][0]
    assert unit['n_lt5'] is True and 'n_sig_q05' not in unit


def test_choose_strata_rules():
    cells = pd.DataFrame({
        'ant_cell_type': ['astrocyte'] * 6,
        'ant_author_cell_type': ['A1', 'A1', 'A2', 'A2', 'A1', None],
        'ant_author_cluster': ['c1', 'c2', 'c1', 'c2', 'c1', 'c2'],
    })
    membership, _ = aft.choose_strata(cells, {})
    assert set(membership['source']) == {'constant_all_cells', 'ant_author_cell_type'}
    assert (membership[membership['source'] == 'constant_all_cells']['label'] == 'astrocyte').all()
    assert len(membership[membership['source'] == 'ant_author_cell_type']) == 5   # NULL label dropped
    varying = cells.assign(ant_cell_type=['a', 'a', 'b', 'b', 'a', 'b'])
    membership, _ = aft.choose_strata(varying, {})
    assert set(membership['source']) == {'ant_cell_type'}
    absent = cells.drop(columns=['ant_cell_type', 'ant_author_cell_type'])
    membership, _ = aft.choose_strata(absent, {})
    assert set(membership['source']) == {'ant_author_cluster'}
    # no ant_cell_type and no ant_author_cell_type: an author column such as M23/IMM's
    # cell_type_high_resolution must be used (it used to fall through to a single 'all' stratum)
    high_res = pd.DataFrame({
        'cell_type_high_resolution': ['Mic P2RY12', 'Mic P2RY12', 'T cells', 'CAMs', 'Mic TPT1', None],
        'ant_author_cluster': ['c1', 'c2', 'c1', 'c2', 'c1', 'c2'],
    })
    membership, note = aft.choose_strata(high_res, {'cell_type_high_resolution': 'VARCHAR'})
    # the two Mic labels are one group: a derived whole-microglia pool stratum is added
    assert set(membership['source']) == {'cell_type_high_resolution', 'group_pool'}
    own = membership[membership['source'] == 'cell_type_high_resolution']
    assert set(own['label']) == {'Mic P2RY12', 'T cells', 'CAMs', 'Mic TPT1'}     # NULL label dropped
    pooled = membership[membership['source'] == 'group_pool']
    assert set(pooled['label']) == {'microglia'} and sorted(pooled['pos']) == [0, 1, 4]
    assert 'cell_type_high_resolution' in note and 'group pool' in note
    for name in ('cell_type_high_resolution', 'author_cell_type_low_resolution', 'cell_type_fine', 'celltype_major'):
        assert aft.CELLTYPE_CANDIDATE_RE.match(name), name
    for name in ('cell_type_ontology_term_id', 'seurat_idents', 'sample'):
        assert not aft.CELLTYPE_CANDIDATE_RE.match(name), name
    nothing = pd.DataFrame({'x': [1, 2, 3]})
    membership, _ = aft.choose_strata(nothing, {})
    assert set(membership['source']) == {'none'} and set(membership['label']) == {'all'}
