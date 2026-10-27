"""lib/catalog_search.py: the keyword search of GET /search and the browse lists of the front page.

Pure functions over dataset records shaped like GET /databases; no server, no files.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import catalog_search as cs  # noqa: E402

DATABASES = [
    {'id': 'A1/MG', 'group_id': 'A1', 'file_id': 'MG', 'title': 'Sorted microglia, AD cohort',
     'collection': {'title': 'Smith 2024', 'group': 'Smith2024'}, 'organism': ['human'],
     'celltype': ['microglial cell'], 'disease': ['Alzheimer disease', 'normal'], 'tissue': ['prefrontal cortex'],
     'n_cells': 1000, 'designation': 'primary',
     'obs': {'categorical': ['ant_apoe4_carrier', 'ant_sex', 'ant_donor_uid'], 'numerical': ['ant_age']}},
    {'id': 'A1/RE', 'group_id': 'A1', 'file_id': 'RE', 'title': 'Reanalysis of sorted microglia',
     'collection': {'title': 'Smith 2024'}, 'organism': ['human'], 'celltype': ['microglial cell'],
     'disease': ['Alzheimer disease'], 'n_cells': 800, 'designation': 'reanalysis'},
    {'id': 'B2/WT', 'group_id': 'B2', 'file_id': 'WT', 'title': 'Whole tissue',
     'collection': {'title': 'Jones 2025'}, 'organism': ['human'],
     'celltype': ['astrocyte', 'microglial cell', 'neuron'],
     'disease': ['Alzheimer disease', 'leukoencephalopathy, diffuse hereditary, with spheroids 1'],
     'n_cells': 5000, 'description': 'Adult donors; TREM2 variant carriers included.'},
    {'id': 'C3/MM', 'group_id': 'C3', 'file_id': 'MM', 'title': 'Mouse microglia',
     'collection': {'title': 'Lee 2023'}, 'organism': ['mouse'], 'celltype': ['microglial cell'], 'n_cells': 300},
]


def _gene_index():
    return cs.GeneIndex([('A1/MG', 'TREM2'), ('B2/WT', 'TREM2'), ('Z9/HIDDEN', 'TREM2'), ('C3/MM', 'Trem2'),
                         ('A1/MG', 'APOE'), ('A1/MG', 'TREM1'), ('A1/MG', 'CAT')])


def test_query_words_drop_filler_possessives_and_duplicates():
    assert cs.query_words("TREM2 expression in microglia across Alzheimer's datasets") == ['trem2', 'microglia', 'alzheimer']
    assert cs.query_words('Which datasets have APOE4 carriers?') == ['apoe4', 'carriers']
    assert cs.query_words('HLA-DRA  hla-dra') == ['hla-dra']
    assert cs.query_words('in the of') == []


def test_short_words_match_whole_words_and_abbreviations_expand():
    assert not cs.word_matches('ad', 'adult donors')
    assert cs.word_matches('ad', 'alzheimer disease')          # AD -> alzheimer
    assert cs.word_matches('microglia', 'microglial cell')
    assert cs.word_matches('carriers', 'apoe e4 carrier')       # plural s optional
    assert cs.word_matches('apoe4', 'apoe e4 carrier')
    assert not cs.word_matches('ms', 'mass spectrometry')


def test_disease_values_are_never_split_on_commas():
    assert cs.disease_values(DATABASES[2]) == ['Alzheimer disease',
                                               'leukoencephalopathy, diffuse hereditary, with spheroids 1']
    assert cs.disease_label('normal') == 'control donors'
    assert cs.disease_label('essential_tremor') == 'essential tremor'


def test_gene_words_are_genes_and_do_not_filter_datasets():
    result = cs.search("TREM2 expression in microglia across Alzheimer's datasets", DATABASES,
                       gene_index=_gene_index(), labels={})
    assert result['gene_words'] == ['trem2']
    # counted over the caller's visible datasets only: the hidden Z9/HIDDEN is not counted
    assert result['genes'] == [{'gene': 'TREM2', 'n_datasets': 3}]
    ids = [row['db_id'] for row in result['datasets']]
    assert set(ids) == {'A1/MG', 'A1/RE', 'B2/WT'} and result['n_datasets'] == 3
    assert ids.index('A1/MG') < ids.index('A1/RE')               # primary before reanalysis
    assert result['datasets_basis'] == 'words'


def test_a_cell_type_word_must_match_the_cell_type_field():
    neurons = {'id': 'D4/EX', 'group_id': 'D4', 'title': 'Excitatory neurons', 'celltype': ['excitatory neuron'],
               'disease': ['Alzheimer disease'], 'description': 'Companion of a microglia study.'}
    result = cs.search('microglia alzheimer', DATABASES + [neurons], labels={})
    assert 'D4/EX' not in [row['db_id'] for row in result['datasets']]
    # a word that names no curated value anywhere still matches the free text
    assert [row['db_id'] for row in cs.search('companion', DATABASES + [neurons], labels={})['datasets']] == ['D4/EX']


def test_gene_only_query_lists_datasets_that_mention_it():
    result = cs.search('TREM2', DATABASES, gene_index=_gene_index(), labels={})
    assert result['datasets_basis'] == 'mentions'
    assert [row['db_id'] for row in result['datasets']] == ['B2/WT']


def test_apoe4_carriers_finds_the_annotation_column():
    result = cs.search('Which datasets have APOE4 carriers?', DATABASES, gene_index=_gene_index(),
                       labels={'apoe4_carrier': 'APOE e4 carrier'})
    assert [row['db_id'] for row in result['datasets']] == ['A1/MG']
    assert 'cell annotations' in result['datasets'][0]['matched_in']
    assert result['genes'] == []


def test_unknown_single_word_suggests_genes_by_prefix_and_skipped_groups_are_none():
    result = cs.search('trem', DATABASES, gene_index=_gene_index(), labels={})
    assert result['suggestions'] == ['TREM1', 'TREM2']
    assert result['genesets'] is None and result['papers'] is None
    assert cs.search('trem', DATABASES, labels={})['genes'] is None


def test_genesets_and_papers_groups():
    genesets = [{'study': 'S', 'experiment': 'E', 'name': 'Microglia_DAM', 'description': 'disease associated microglia',
                 'n_genes': 30},
                {'study': 'S', 'experiment': 'E', 'name': 'Astro_A1', 'n_genes': 12}]
    seen = []
    result = cs.search('DAM microglia', DATABASES, gene_index=_gene_index(), genesets=genesets,
                       papers=lambda words: seen.append(words) or [{'citekey': 'smith2024'}], labels={})
    assert [row['name'] for row in result['genesets']] == ['Microglia_DAM']
    assert seen == ['dam microglia'] and result['n_papers'] == 1
    by_gene = cs.search('TREM2', DATABASES, gene_index=_gene_index(), genesets=genesets,
                        genesets_with_gene=lambda gene: [{'study': 'S', 'experiment': 'E', 'name': f'has_{gene}'}],
                        labels={})
    assert [row['name'] for row in by_gene['genesets']] == ['has_TREM2']


def test_author_names_find_datasets_and_collections():
    named = {'id': 'S9/V6', 'group_id': 'S9', 'file_id': 'V6', 'title': 'Village grafts',
             'collection': {'title': 'Xenograft study', 'group': 'Xeno2026',
                            'authors': ['Daan Moechars'], 'first_author': 'Annerieke Sierksma',
                            'last_author': 'Bart De Strooper'}, 'organism': ['human'], 'n_cells': 10}
    own = {'id': 'S9/QC', 'group_id': 'S9', 'file_id': 'QC', 'title': 'Grafts QC', 'first_author': 'Ann Other',
           'collection': {'title': 'Xenograft study', 'group': 'Xeno2026'}}
    result = cs.search('Annerieke', DATABASES + [named, own], labels={})
    assert [row['db_id'] for row in result['datasets']] == ['S9/V6']
    assert result['datasets'][0]['matched_in'] == ['authors']
    assert [row['group_id'] for row in result['collections']] == ['S9']
    assert result['collections'][0]['matched_in'] == ['collection']
    both = cs.search('sierksma strooper', DATABASES + [named, own], labels={})
    assert both['n_datasets'] == 1
    assert [row['db_id'] for row in cs.search('Other', [own], labels={})['datasets']] == ['S9/QC']
    assert cs.author_names({'authors': ['A B', 'C D'], 'first_author': 'A B', 'last_author': 'E F'}) == \
        ['A B', 'C D', 'E F']


PAPERS = [
    {'citekey': 'smith2024', 'title': 'Microglia in Alzheimer disease', 'authors': ['Smith, Anna'],
     'journal': 'Nature', 'doi': '10.1/abc', 'keywords': ['lipid'], 'summary': 'A study.', 'year': 2024},
    {'citekey': 'jones2020', 'title': 'Astrocytes', 'authors': ['Jones, Bob'], 'journal': 'Cell',
     'doi': '10.1/def', 'celltypes': ['microglial cell'], 'summary': None, 'year': 2020},
]


def _fulltext_hit(citekey, score, n=1):
    return {'citekey': citekey, 'summary': {'citekey': citekey, 'title': f'Paper {citekey}'}, 'score': score,
            'n_passages': n, 'passages': [{'page_start': 3, 'page_end': 3, 'section': 'results',
                                           'snippet': '<mark>microglia</mark> here'}]}


def test_papers_merge_record_and_full_text_hits():
    seen = []
    result = cs.search('microglia', DATABASES, labels={},
                       papers=lambda words: PAPERS,
                       paper_fulltext=lambda groups: seen.append(groups) or [
                           _fulltext_hit('weak2019', 1.0, 5), _fulltext_hit('jones2020', 2.0),
                           _fulltext_hit('strong2018', 3.0, 1)])
    assert seen == [[('microglia',)]]
    keys = [row['citekey'] for row in result['papers']]
    # record and full text, then record only, then full text only by best passage score
    assert keys == ['jones2020', 'smith2024', 'strong2018', 'weak2019']
    assert result['n_papers'] == 4
    rows = {row['citekey']: row for row in result['papers']}
    assert rows['jones2020']['matched_in'] == ['keywords', 'fulltext']
    assert rows['smith2024']['matched_in'] == ['title'] and rows['smith2024']['passages'] == []
    assert rows['strong2018']['matched_in'] == ['fulltext'] and rows['strong2018']['passages'][0]['page_start'] == 3
    assert rows['weak2019']['n_passages'] == 5 and rows['smith2024']['n_passages'] == 0


def test_paper_matched_fields_cover_the_record():
    row = {'citekey': 'sierksma2020novel', 'title': 'Novel risk genes', 'authors': ['Sierksma, Annerieke'],
           'journal': 'EMBO Molecular Medicine', 'doi': '10.15252/emmm.201910606', 'keywords': ['amyloid'],
           'summary': 'Risk genes and amyloid.'}
    assert cs.paper_matched_fields(row, ['annerieke']) == ['authors']
    assert cs.paper_matched_fields(row, ['sierksma']) == ['citekey', 'authors']
    assert cs.paper_matched_fields(row, ['embo']) == ['journal']
    assert cs.paper_matched_fields(row, ['10.15252']) == ['doi']
    assert cs.paper_matched_fields(row, ['amyloid']) == ['keywords', 'summary']


def test_kinds_limit_the_work():
    calls = []
    result = cs.search('microglia', DATABASES, labels={}, gene_index=_gene_index(),
                       papers=lambda words: calls.append('papers') or [],
                       paper_fulltext=lambda groups: calls.append('fulltext') or [],
                       genesets=[], help_hidden=[], kinds=['datasets'])
    assert calls == [] and result['kinds'] == ['datasets']
    assert result['n_datasets'] > 0
    for group in ('papers', 'genesets', 'help', 'genes', 'collections', 'artefacts', 'facets'):
        assert result[group] is None, group
    only_papers = cs.search('microglia', DATABASES, labels={}, papers=lambda words: PAPERS, kinds=['papers'])
    assert only_papers['datasets'] is None and only_papers['n_papers'] == 2
    assert cs.search('microglia', DATABASES, labels={}, kinds=[])['kinds'] == list(cs.SEARCH_KINDS)
    with pytest.raises(ValueError):
        cs.search('microglia', DATABASES, kinds=['planets'])


def test_paper_query_expands_abbreviations():
    assert cs.paper_query(['ad', 'carriers', 'trem2']) == 'alzheimer carrier trem2'


def test_browse_counts_disease_and_celltype():
    diseases = cs.browse_counts(DATABASES, 'disease')
    labels = [entry['label'] for entry in diseases['entries']]
    assert labels[0] == 'Alzheimer disease' and labels[-1] == 'control donors'
    alz = diseases['entries'][0]
    assert (alz['n_datasets'], alz['n_collections']) == (3, 2)
    assert diseases['n_with_value'] == 3 and diseases['n_without_value'] == 1
    celltypes = cs.browse_counts(DATABASES, 'celltype')
    first = celltypes['entries'][0]
    assert (first['label'], first['n_datasets']) == ('microglia', 4)
    rows = cs.browse_datasets(DATABASES, 'disease', 'control donors')
    assert [row['db_id'] for row in rows] == ['A1/MG']
    with pytest.raises(ValueError):
        cs.browse_counts(DATABASES, 'tissue')


def test_catalogue_summary_counts_cells_of_primary_data_once():
    summary = cs.catalogue_summary(DATABASES)
    assert summary['n_datasets'] == 4 and summary['n_collections'] == 3
    assert summary['n_cells'] == 7100 and summary['n_cells_primary'] == 6300
    assert summary['n_human'] == 3 and summary['n_microglia'] == 4


def test_annotation_labels_come_from_the_vocabulary():
    labels = cs.vocabulary_labels()
    assert labels.get('apoe4_carrier') == 'APOE e4 carrier' and labels.get('sex') == 'Sex'
    assert cs.annotation_label('ant_braak', labels) == 'Braak stage'
    assert cs.annotation_label('ant_made_up_thing', labels) == 'Made up thing'


def test_catalogue_stats_and_recently_updated():
    dbs = [dict(db, updated=f'2026-10-0{n}') for n, db in enumerate(DATABASES, start=1)]
    stats = cs.catalogue_stats(dbs, gene_index=_gene_index(), n_genesets=4, n_papers=None, n_artefacts=0)
    assert stats['n_genes'] == 4            # TREM2 (with Trem2), APOE, TREM1, CAT; Z9/HIDDEN is not visible
    assert stats['n_mouse'] == 1 and stats['n_genesets'] == 4 and stats['n_papers'] is None
    assert [row['db_id'] for row in stats['recently_updated']] == ['C3/MM', 'B2/WT', 'A1/RE', 'A1/MG']
    assert cs.catalogue_stats(DATABASES)['n_genes'] is None
