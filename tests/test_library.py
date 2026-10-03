"""Tests for lib/library.py: sidecar round trip, BibTeX export, validation,
citekeys and h5adstore reference resolution."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import library as L  # noqa: E402

bibtexparser = pytest.importorskip('bibtexparser')


def make_paper(tmp_path, key='lee2026plasticity', **extra):
    fields = {
        'entrytype': 'article', 'citekey': key,
        'author': ['Lee, Donghoon', 'O\'Neil, Ann & Co', 'Roussos, Panos'],
        'title': 'Plasticity of human microglia: 100% {GPNMB} & TREM2_high',
        'journaltitle': 'Nature Genetics', 'date': '2026-08-11', 'volume': '58',
        'pages': '2152-2163', 'doi': '10.1038/s41588-026-02716-6',
        'keywords': ['microglia', 'aging'], 'anthive_role': 'source',
        'anthive_celltypes': ['microglial cell'], 'anthive_draft': ['anthive_celltypes'],
    }
    fields.update(extra)
    papers = tmp_path / 'papers'
    papers.mkdir(parents=True, exist_ok=True)
    path = papers / f"{key}.md"
    L.write_sidecar(path, fields, '## Notes\n\nFree text, with: colons and "quotes".')
    return path


def test_sidecar_round_trip(tmp_path):
    path = make_paper(tmp_path)
    paper = L.parse_sidecar(path)
    assert paper.citekey == 'lee2026plasticity'
    assert paper.fields['author'][0] == 'Lee, Donghoon'
    assert paper.fields['keywords'] == ['microglia', 'aging']
    assert 'Free text, with: colons' in paper.body
    # writing it again is stable
    L.write_sidecar(path, paper.fields, paper.body)
    assert L.parse_sidecar(path).fields == paper.fields


def test_bibtex_export_parses_and_escapes(tmp_path):
    paper = L.parse_sidecar(make_paper(tmp_path))
    text = L.to_bibtex(paper)
    assert text.startswith('@article{lee2026plasticity,')
    assert 'author = {Lee, Donghoon and O\'Neil, Ann \\& Co and Roussos, Panos}' in text
    assert '100\\%' in text and 'TREM2\\_high' in text
    assert 'doi = {10.1038/s41588-026-02716-6}' in text  # verbatim, not escaped
    db = bibtexparser.loads(text)
    entry = db.entries[0]
    assert entry['ID'] == 'lee2026plasticity'
    assert entry['ENTRYTYPE'] == 'article'
    assert entry['keywords'] == 'microglia, aging'
    assert entry['anthive_role'] == 'source'
    plain = L.to_bibtex(paper, include_anthive=False)
    assert 'anthive_' not in plain


def test_bibtex_round_trip_is_lossless(tmp_path):
    from bibtexparser.bparser import BibTexParser
    path = make_paper(tmp_path, entrytype='online', note='Preprint, bioRxiv',
                      anthive_orcids={'Donghoon Lee': '0000-0001-0000-0001'},
                      anthive_cites=['10.1/a', '10.1/b'], related=['other2020x'], relatedtype='samecohort',
                      institution='KU Leuven', type='protocol')
    paper = L.parse_sidecar(path)
    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    entry = bibtexparser.loads(L.to_bibtex(paper), parser=parser).entries[0]
    back = L.fields_from_bibtex_entry(entry)
    back['citekey'] = entry['ID']
    assert back == paper.fields


def test_classic_export(tmp_path):
    paper = L.parse_sidecar(make_paper(tmp_path, entrytype='online'))
    entry = bibtexparser.loads(L.to_bibtex(paper, classic=True)).entries[0]
    assert entry['ENTRYTYPE'] == 'misc'
    assert entry['journal'] == 'Nature Genetics'
    assert entry['year'] == '2026' and entry['month'] == 'aug'
    back = L.fields_from_bibtex_entry(entry)
    assert back['journaltitle'] == 'Nature Genetics' and back['date'] == '2026-08'


def test_integer_date_exports_as_year(tmp_path):
    paper = L.parse_sidecar(make_paper(tmp_path, date=2019))
    assert 'year = {2019}' in L.to_bibtex(paper)


def test_unbalanced_braces_are_neutralised(tmp_path):
    paper = L.parse_sidecar(make_paper(tmp_path, title='Broken } title { here {'))
    entry = bibtexparser.loads(L.to_bibtex(paper)).entries[0]
    assert 'Broken' in entry['title']


def test_validation_errors(tmp_path):
    path = make_paper(tmp_path, key='lee2026plasticity', entrytype='journalpaper', anthive_role='favourite',
                      anthive_access='secret', doi='not-a-doi', keywords='microglia')
    issues = L.validate_paper(L.parse_sidecar(path))
    text = ' | '.join(issues.errors)
    assert 'entrytype' in text and 'anthive_role' in text
    assert 'doi' in text and 'keywords must be a list' in text
    assert 'anthive_access' not in text


def test_legacy_access_and_llm_fields_are_ignored_with_a_warning(tmp_path):
    path = make_paper(tmp_path, anthive_access='restricted', anthive_llm='never')
    paper = L.parse_sidecar(path)
    issues = L.validate_paper(paper)
    assert not issues.errors
    warnings = ' | '.join(issues.warnings)
    assert 'anthive_access ignored: access follows the site login' in warnings
    assert 'anthive_llm ignored' in warnings
    bib = L.to_bibtex(paper)
    assert 'anthive_access' not in bib and 'anthive_llm' not in bib
    assert not hasattr(L, 'ACCESS') and not hasattr(L, 'LLM_POLICY')


def test_document_type_constants():
    assert L.DEFAULT_ROLE == 'background'
    assert set(L.ROLE_HELP) == L.ROLES
    assert 'not linked to any dataset' in L.ROLE_HELP['background']
    assert set(L.ENTRYTYPE_HELP) == {'article', 'online', 'book', 'inbook', 'incollection', 'inproceedings',
                                     'thesis', 'report', 'unpublished', 'misc'}
    assert set(L.ENTRYTYPE_HELP) <= L.ENTRY_TYPES
    assert 'project proposal' in L.DOCUMENT_TYPES['report'] and 'grant proposal' in L.DOCUMENT_TYPES['report']
    assert L.DOCUMENT_TYPES['thesis'] == ['phd thesis', 'master thesis']
    assert 'type' in L.FIELD_ORDER


def test_validation_file_name_and_citekey(tmp_path):
    path = make_paper(tmp_path, key='lee2026plasticity')
    renamed = path.with_name('other.md')
    path.rename(renamed)
    issues = L.validate_paper(L.parse_sidecar(renamed))
    assert any('<citekey>.md' in e for e in issues.errors)
    bad = make_paper(tmp_path, key='Lee2026Plasticity')
    issues = L.validate_paper(L.parse_sidecar(bad))
    assert any('lowercase' in e for e in issues.errors)


def test_vocabulary_warning(tmp_path):
    path = make_paper(tmp_path, anthive_celltypes=['microglial cell', 'unicorn cell'])
    issues = L.validate_paper(L.parse_sidecar(path), L.load_vocabulary())
    assert any('unicorn cell' in w for w in issues.warnings)
    assert not any('microglial cell' in w for w in issues.warnings)


def test_citekey_generation_and_collisions():
    key = L.make_citekey('Lee', 2026, 'The plasticity of human microglia')
    assert key == 'lee2026plasticity'
    assert L.make_citekey('Smajić', '2021-12-17', 'Single-cell sequencing of midbrain') == 'smajic2021singlecell'
    assert L.make_citekey('Huuki-Myers', 2024, 'A data-driven map') == 'huukimyers2024datadriven'
    taken = {'lee2026plasticity', 'lee2026plasticitya'}
    assert L.make_citekey('Lee', 2026, 'Plasticity again', taken) == 'lee2026plasticityb'
    assert L.make_citekey('', None, '') == 'anonnd'


def test_first_author_last():
    assert L.first_author_last({'author': ['De Strooper, Bart']}) == 'De Strooper'
    assert L.first_author_last({'author': 'Jin, Mengmeng and Ma, Ziyuan'}) == 'Jin'


def test_load_library_detects_duplicates_and_dangling_related(tmp_path):
    make_paper(tmp_path, key='lee2026plasticity')
    make_paper(tmp_path, key='lee2026plasticitya', related=['missing2020paper'], relatedtype='samecohort')
    papers, issues = L.load_library(tmp_path)
    assert set(papers) == {'lee2026plasticity', 'lee2026plasticitya'}
    assert any(e.startswith('DOI ') and ' in both ' in e for e in issues.errors)  # same DOI twice
    assert any('missing2020paper' in e for e in issues.errors)


def test_references_and_unresolved(tmp_path):
    lib = tmp_path / 'lib'
    make_paper(lib, key='lee2026plasticity')
    papers, _ = L.load_library(lib)
    store = tmp_path / 'store'
    (store / 'Lee2026').mkdir(parents=True)
    (store / 'Lee2026' / 'index.md').write_text(
        '---\ngroup: Lee2026\ngroup_id: L26\npapers:\n  lee2026plasticity: source\n  nobody2020missing: related\n---\n')
    (store / 'Lee2026' / 'ds.md').write_text('---\nid: FMG\npapers:\n  lee2026plasticity: favourite\n---\n')
    (store / '_staging').mkdir()
    (store / '_staging' / 'index.md').write_text('---\npapers:\n  ghost2020x: source\n---\n')
    refs = L.find_references(store)
    assert {(r.citekey, r.scope) for r in refs} == {
        ('lee2026plasticity', 'collection'), ('nobody2020missing', 'collection'), ('lee2026plasticity', 'dataset')}
    problems = L.unresolved_references(papers, refs)
    assert any('nobody2020missing' in p for p in problems)
    assert any('favourite' in p for p in problems)
    assert not any('ghost2020x' in p for p in problems)


def test_normalize_doi():
    assert L.normalize_doi('https://doi.org/10.1038/S41588-026-02716-6') == '10.1038/s41588-026-02716-6'
    assert L.normalize_doi('doi: 10.1101/2024.03.12.584646.') == '10.1101/2024.03.12.584646'


def test_ontology_terms():
    terms = L.ontology_terms(['microglial cell', 'unicorn cell'])
    assert terms[0] == {'label': 'microglial cell', 'id': 'CL:0000129'}
    assert terms[1]['id'] is None


def test_library_dir_config(monkeypatch, tmp_path):
    import config
    monkeypatch.setenv('ANTHIVE_LIBRARY', str(tmp_path / 'mylib'))
    assert config.get_library_dir({}) == tmp_path / 'mylib'


def test_clean_text_undoes_html_entities_and_bibtex_escapes():
    assert L.clean_text("Alzheimer's &amp; Dementia") == "Alzheimer's & Dementia"
    # CrossRef abstracts escaped two or three times
    assert L.clean_text('(P &amp;amp;lt; .001)') == '(P < .001)'
    assert L.clean_text('&amp;gt;41 000 nuclei') == '>41 000 nuclei'
    assert L.clean_text('Cell Death \\& Differentiation, 100\\%') == 'Cell Death & Differentiation, 100%'
    assert L.clean_text(['O&#39;Neil, Ann', 'Smith &amp; Co']) == ["O'Neil, Ann", 'Smith & Co']
    # plain text, bare ampersands and non-strings are untouched
    assert L.clean_text('R&D at A & B') == 'R&D at A & B'
    assert L.clean_text(2024) == 2024


def test_parse_sidecar_normalises_escaped_record_text(tmp_path):
    path = make_paper(tmp_path, journaltitle="Alzheimer's &amp; Dementia",
                      title='APOE \\& TREM2 in &lt;5% of microglia',
                      abstract='significant (P &amp;amp;lt; .001)',
                      author=['O&#39;Neil, Ann'],
                      url='https://example.org/?a=1&amp;b=2')
    fields = L.parse_sidecar(path).fields
    assert fields['journaltitle'] == "Alzheimer's & Dementia"
    assert fields['title'] == 'APOE & TREM2 in <5% of microglia'
    assert fields['abstract'] == 'significant (P < .001)'
    assert fields['author'] == ["O'Neil, Ann"]
    # verbatim identifier fields keep their exact characters
    assert fields['url'] == 'https://example.org/?a=1&amp;b=2'
    # and the BibTeX export escapes the plain text exactly once
    bib = L.to_bibtex(L.parse_sidecar(path))
    assert "Alzheimer's \\& Dementia" in bib and '&amp;' not in bib.split('url')[0]
