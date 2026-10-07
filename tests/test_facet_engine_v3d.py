"""Facet engine round 3d (2026-10-07): generic layer exclusion, tissue groups, retired facets, replicate column ``cell_name``.

* ``lib/facet_scopes.py``: ``facet_exclusion_reason`` (spatial, proteomics, ``facet_exclude``).
* ``lib/facet_lint.py``: ``lint_facet_exclude``, ``lint_replicate`` accepting ``cell_name``, the tissue controlled values.
* ``lib/tissue_groups.py`` and ``lib/tissue_groups.yaml``: coarse tissue groups with UBERON ids.
* ``lib/obscol_tags_vocab.yaml``: tissue facet, retired region, author cell type and singleton facets.
* ``bin/annot/ant-facet-test``: ``dataset_eligibility`` honours the exclusion, ``n_tissues`` counts ``tissue_uberon``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import facet_lint  # noqa: E402
import facet_results  # noqa: E402
import facet_scopes  # noqa: E402
import obscol_tags  # noqa: E402
import celltype_groups  # noqa: E402
import tissue_groups  # noqa: E402
from test_facet_test import aft, make_dataset  # noqa: E402


# ── facet_exclusion_reason ───────────────────────────────────────────────────

@pytest.mark.parametrize('technology', ['10x_visium', 'Slide-tags', 'MERFISH', 'Stereo-seq', 'NovaST', 'CosMx', '10x_xenium',
                                        'Slide-seq V2'])
def test_spatial_technologies_are_excluded(technology):
    reason = facet_scopes.facet_exclusion_reason({'technology': technology})
    assert reason and 'spatial' in reason


def test_proteomics_and_spatial_system_are_excluded():
    assert 'proteomics' in facet_scopes.facet_exclusion_reason({'technology': 'mass-spectrometry'})
    assert 'spatial' in facet_scopes.facet_exclusion_reason({'experimental_system': 'spatial'})


@pytest.mark.parametrize('annotation', [{}, {'technology': '10x_chromium_3p_v3'}, {'technology': ['snRNA-seq', '10x_multiome']},
                                        {'technology': 'bulk_RNA-seq'}])
def test_single_cell_technologies_are_not_excluded(annotation):
    assert facet_scopes.facet_exclusion_reason(annotation) is None


def test_facet_exclude_flag_carries_its_reason():
    assert facet_scopes.facet_exclusion_reason({}, {'facet_exclude': True, 'facet_exclude_reason': 'metacell set'}) == 'metacell set'
    assert facet_scopes.facet_exclusion_reason({}, {'facet_exclude': True}) == 'facet_exclude is set'
    assert facet_scopes.facet_exclusion_reason({}, {'facet_exclude': False, 'facet_exclude_reason': 'x'}) is None


def test_eligibility_reports_the_exclusion(tmp_path):
    store, entry = make_dataset(tmp_path)
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    annotation = aft.mus.resolve_annotation(entry.h5ad_path, root=store)
    api = aft.get_api_config(md_yaml)
    assert aft.dataset_eligibility(entry, md_yaml, annotation, api) == (True, 'ok')
    eligible, reason = aft.dataset_eligibility(entry, {**md_yaml, 'facet_exclude': True, 'facet_exclude_reason': 'metacell set'},
                                               annotation, api)
    assert not eligible and reason == 'excluded from the facet layer: metacell set'
    eligible, reason = aft.dataset_eligibility(entry, md_yaml, {**annotation, 'technology': 'MERFISH'}, api)
    assert not eligible and 'spatial technology (merfish)' in reason


# ── lint_facet_exclude / lint_replicate ──────────────────────────────────────

def test_lint_facet_exclude_rules():
    assert facet_lint.lint_facet_exclude({}) == []
    assert facet_lint.lint_facet_exclude({'facet_exclude': True, 'facet_exclude_reason': 'metacell set'}) == []
    assert [f.rule for f in facet_lint.lint_facet_exclude({'facet_exclude': True})] == ['facet_exclude_invalid']
    assert [f.rule for f in facet_lint.lint_facet_exclude({'facet_exclude': True, 'facet_exclude_reason': '  '})] == ['facet_exclude_invalid']
    assert [f.rule for f in facet_lint.lint_facet_exclude({'facet_exclude': 'yes'})] == ['facet_exclude_invalid']
    unused = facet_lint.lint_facet_exclude({'facet_exclude_reason': 'x'})
    assert [f.rule for f in unused] == ['facet_exclude_reason_unused'] and unused[0].severity == facet_lint.WARNING


def test_lint_replicate_accepts_cell_name():
    metadata = {'replicate_unit': 'sample', 'replicate_column': 'cell_name'}
    assert facet_lint.lint_replicate(metadata, {}, stored_columns=['Replicate']) == []
    missing = facet_lint.lint_replicate({'replicate_unit': 'sample', 'replicate_column': 'nope'}, {}, stored_columns=['Replicate'])
    assert [f.rule for f in missing] == ['replicate_column_missing']


# ── tissue groups ────────────────────────────────────────────────────────────

def test_every_label_is_in_one_group_and_ids_are_uberon():
    table = tissue_groups._table()[0]
    assert len(tissue_groups.coarse_terms()) == len(set(tissue_groups.coarse_terms()))
    for key, entry in table.items():
        assert entry.group in tissue_groups.coarse_terms()
        assert entry.uberon_id is None or re.fullmatch(r'UBERON:\d+', entry.uberon_id), key
        assert tissue_groups.normalise_label(entry.label) == key


def test_every_group_term_is_also_a_specific_label():
    for group in tissue_groups.coarse_terms():
        if group in ('basal ganglia', 'brainstem'):
            continue
        assert tissue_groups.coarse_tissue(group) is not None, group


@pytest.mark.parametrize('label, group', [
    ('dorsolateral prefrontal cortex', 'prefrontal cortex'), ('superior frontal gyrus', 'prefrontal cortex'),
    ('Brodmann (1909) area 10', 'prefrontal cortex'), ('frontal pole', 'prefrontal cortex'),
    ('anterior cingulate cortex', 'cingulate cortex'), ('middle temporal gyrus', 'temporal cortex'),
    ('Cerebral_cortex', 'cerebral cortex'), ('Substantia_nigra', 'midbrain'), ('  Hippocampal  Formation ', 'hippocampus'),
    ('white matter of temporal lobe', 'white matter'), ('cervical spinal cord white matter', 'spinal cord'),
    ('caudate nucleus', 'basal ganglia'), ('occipitotemporal cortex', 'occipital cortex'),
])
def test_coarse_tissue(label, group):
    assert tissue_groups.coarse_tissue(label) == group


@pytest.mark.parametrize('label', [None, '', '   ', 'Tumor', 'MPM', 'layer 5'])
def test_unknown_labels_have_no_group(label):
    assert tissue_groups.lookup(label) is None
    assert tissue_groups.coarse_tissue(label) is None and tissue_groups.uberon_id(label) is None


def test_uberon_id_lookup_and_missing_id():
    assert tissue_groups.uberon_id('dorsolateral prefrontal cortex') == 'UBERON:0009834'
    assert tissue_groups.uberon_id('occipitotemporal cortex') is None


def test_tissue_facet_values_are_the_group_terms():
    registry = obscol_tags.facet_registry()
    assert registry['tissue']['controlled_values'] == tissue_groups.coarse_terms()
    assert registry['tissue']['facet_scopes'] == ['human_postmortem']


def test_tissue_value_outside_the_groups_is_flagged():
    api = {'obs_constant': {'ant_tissue': {'value': 'dorsolateral prefrontal cortex'}}}
    findings = facet_lint.lint_api(api)
    assert any(f.rule == 'controlled_value' for f in findings)
    ok = {'obs_constant': {'ant_tissue': {'value': 'prefrontal cortex'}}}
    assert not any(f.rule == 'controlled_value' for f in facet_lint.lint_api(ok))


def test_tissue_in_a_xenograft_is_a_scope_mismatch():
    api = {'obs_constant': {'ant_tissue': {'value': 'cerebral cortex'}}}
    findings = facet_lint.lint_facet_scope(api, 'xenograft_human_in_mouse')
    assert [f.rule for f in findings] == ['facet_scope_mismatch']
    assert facet_lint.lint_facet_scope(api, 'human_postmortem') == []


# ── retired facets ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['region', 'lesion', 'ms_type', 'cause_of_death', 'ethnicity', 'source_institution',
                                  'technology', 'group', 'author_cell_type'])
def test_retired_facets_are_deprecated(name):
    assert obscol_tags.facet_registry()[name]['status'] == 'deprecated'


CLUSTER_TAGS = ['mg_author_cluster', 'astro_author_cluster', 'oligo_author_cluster', 'opc_author_cluster',
                'exn_author_cluster', 'inn_author_cluster', 'endo_author_cluster']


def test_generic_author_cluster_is_retired_for_per_cell_type_facets():
    registry = obscol_tags.facet_registry()
    assert registry['author_cluster']['status'] == 'deprecated'
    assert registry['author_cluster']['replaced_by'] == CLUSTER_TAGS
    assert registry['region']['replaced_by'] == ['tissue']
    for tag in CLUSTER_TAGS:
        assert registry[tag]['status'] == 'registered'
        assert registry[tag]['dataset_scoped'] is True and registry[tag]['scope'] == 'dataset'
    assert registry['sex']['dataset_scoped'] is False


def test_cluster_columns_match_the_registry_and_are_never_cross_dataset():
    assert set(aft.AUTHOR_CLUSTER_COLUMNS) == {'ant_' + tag for tag in CLUSTER_TAGS} | {'ant_author_cluster'}
    assert 'ant_mg_author_cluster' in aft.AUTHOR_CLUSTER_COLUMNS


def test_author_cluster_column_prefers_the_per_cell_type_column():
    frame = pd.DataFrame({'ant_donor': ['a'], 'ant_author_cluster': ['x'], 'ant_mg_author_cluster': ['y']})
    assert aft.author_cluster_column(frame) == 'ant_mg_author_cluster'
    assert aft.author_cluster_column(frame.drop(columns='ant_mg_author_cluster')) == 'ant_author_cluster'
    assert aft.author_cluster_column(frame[['ant_donor']]) is None


def test_cluster_strata_and_source_priority_accept_the_per_group_columns():
    assert facet_results.source_priority('ant_mg_author_cluster') == facet_results.source_priority('ant_author_cluster') == 4
    assert 'ant_mg_author_cluster' in facet_results._stratum_source_text('ant_mg_author_cluster')


def test_a_cluster_column_is_the_only_stratifier_when_cell_types_are_absent():
    cells = pd.DataFrame({'ant_donor': ['a', 'a', 'b', 'b'],
                          'ant_mg_author_cluster': ['d1/cluster_1', 'd1/cluster_2', 'd1/cluster_1', 'd1/cluster_2']})
    membership, _ = aft.choose_strata(cells, {})
    assert set(membership['source']) >= {'ant_mg_author_cluster'}


# ── n_tissues counts the specific column ─────────────────────────────────────

def test_n_tissues_counts_tissue_uberon_within_one_coarse_group():
    cells = pd.DataFrame({'ant_donor': ['d1', 'd1', 'd2', 'd2'], 'ant_tissue': ['prefrontal cortex'] * 4,
                          'tissue_uberon': ['dorsolateral prefrontal cortex', 'superior frontal gyrus',
                                            'dorsolateral prefrontal cortex', 'dorsolateral prefrontal cortex']})
    table, _ = aft.build_donor_table(cells)
    assert dict(zip(table['donor'], table['n_tissues'])) == {'d1': 2, 'd2': 1}


# ── cell type groups of the Cell Ontology subtype labels ─────────────────────

@pytest.mark.parametrize('label, group', [
    ('L2/3 intratelencephalic projecting glutamatergic neuron', 'excitatory neuron'),
    ('L6 intratelencephalic projecting Car3 glutamatergic neuron', 'excitatory neuron'),
    ('VIP GABAergic interneuron', 'inhibitory neuron'), ('Lamp5 Lhx6 neuron', 'inhibitory neuron'),
    ('pvalb chandelier GABAergic interneuron', 'inhibitory neuron'), ('sst chodl GABAergic interneuron', 'inhibitory neuron'),
    ('central nervous system macrophage', 'perivascular macrophage'), ('monocyte', 'myeloid (mixed)'),
    ('plasmablast', 'immune (mixed)'), ('T cell', 'T cell'), ('erythrocyte', 'unmapped'),
])
def test_cell_ontology_subtype_labels_belong_to_a_group(label, group):
    assert celltype_groups.group_of(label) == group
