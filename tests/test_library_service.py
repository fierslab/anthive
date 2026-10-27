"""Library service, nested library folders, library S3 sync, ant-stack
library wiring and the MCP literature surface.

Uses fixtures only (fake embedder, fake S3 client, no network). The REST
handlers in bin/api/ant-serve are thin wrappers over LibraryService and are
exercised end to end against a scratch stack (see the D2 report).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'lib'))

pytest.importorskip('yaml')

import library as L  # noqa: E402
from library_service import LibraryService, highlight_snippet  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────

def write_record(folder: Path, citekey: str, **fields) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    data = {'entrytype': 'article', 'citekey': citekey, 'title': f'Title of {citekey}',
            'author': ['Smith, Anna', 'Jones, Bob'], 'date': '2024', **fields}
    path = folder / f'{citekey}.md'
    L.write_sidecar(path, data, '## Notes\n\nSecret notes for ' + citekey)
    return path


def make_store(tmp_path: Path) -> Path:
    store = tmp_path / 'store'
    coll = store / 'Study2024'
    coll.mkdir(parents=True)
    (coll / 'index.md').write_text('---\ngroup: Study2024\ngroup_id: S24\npapers:\n  src2024: source\n---\n')
    (coll / 'cells.md').write_text('---\nid: CEL\npapers:\n  meth2023: method\n---\n')
    (coll / 'other.md').write_text('---\nid: OTH\n---\n')
    return store


@pytest.fixture
def library(tmp_path):
    papers = tmp_path / 'library' / 'papers'
    write_record(papers / 'datasets', 'src2024', anthive_role='source', anthive_cohorts=['ROSMAP'],
                 anthive_celltypes=['microglial cell'], doi='10.1/src', file='src2024.pdf',
                 anthive_summary='Source paper summary.', related=['rel2022'], relatedtype='samecohort',
                 anthive_cites=['10.1/rel'])
    (papers / 'datasets' / 'src2024.pdf').write_bytes(b'%PDF-1.4 fake')
    write_record(papers / 'methods', 'meth2023', anthive_role='method', author=['Doe, Jane'])
    write_record(papers / 'reading' / '2022', 'rel2022', anthive_role='background', doi='10.1/rel',
                 anthive_cohorts=['ROSMAP'], anthive_celltypes=['microglial cell'])
    write_record(papers / 'lab', 'secret2025', anthive_role='background', anthive_access='restricted',
                 author=['De Strooper, Bart'])
    write_record(papers / 'reading', 'nollm2021', anthive_role='background', anthive_llm='never',
                 related=['secret2025'])
    write_record(papers / 'reading', 'summ2020', anthive_role='background', anthive_llm='summary_only',
                 abstract='An abstract.')
    (papers / 'README.md').write_text('# not a record\n')
    (papers / 'reading' / 'notes.md').write_text('---\ntitle: just notes\n---\nno entrytype\n')
    (papers / '_staging').mkdir()
    write_record(papers / '_staging', 'staged2026')
    return tmp_path / 'library'


class FakeEmbedder:
    def __init__(self, model='fake/model-1', dim=32):
        self.model, self.dim = model, dim
        self.query_prefix = self.doc_prefix = ''

    def _vec(self, text):
        vector = np.zeros(self.dim, np.float32)
        for word in re.findall(r'\w+', text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    def embed_documents(self, texts):
        return np.array([self._vec(t) for t in texts], np.float32)

    def embed_query(self, text):
        return self._vec(text)


def build_toy_index(tmp_path: Path, library_dir: Path, model='fake/model-1') -> Path:
    pytest.importorskip('pyarrow')
    pytest.importorskip('scipy')
    import library_index as li
    rows = []
    texts = {
        'src2024': [(1, 'abstract', 'Microglia respond to amyloid plaques in ROSMAP donors.'),
                    (5, 'methods', 'Nuclei were isolated from frozen prefrontal cortex and sequenced.'),
                    (7, 'results', 'TREM2 expression rises in plaque associated microglia.')],
        'nollm2021': [(2, 'results', 'Microglia in the never paper also respond to amyloid.')],
        'summ2020': [(3, 'results', 'Summary only paper on microglia and amyloid.')],
        'secret2025': [(1, 'results', 'Restricted microglia amyloid result.')],
    }
    emb = FakeEmbedder(model)
    for citekey, chunks in texts.items():
        for i, (page, section, text) in enumerate(chunks):
            rows.append({'chunk_id': f'{citekey}:{i}', 'citekey': citekey, 'collection': None, 'doi': None,
                         'title': None, 'year': 2024, 'source_path': f'{citekey}.pdf',
                         'source_sha256': citekey, 'page_start': page, 'page_end': page,
                         'section': section, 'heading': section, 'text': text,
                         'n_tokens': li.count_tokens(text), 'cited_dois': [],
                         'embedding': emb.embed_documents([text])[0].tolist()})
    out = tmp_path / 'index' / 'library_chunks.parquet'
    li.write_index(rows, out, emb)
    return out


@pytest.fixture
def service(tmp_path, library):
    store = make_store(tmp_path)
    svc = LibraryService(library, tmp_path / 'index', store,
                         embedder_factory=lambda model: FakeEmbedder(model))
    svc.set_datasets({'S24/CEL': store / 'Study2024' / 'cells.md',
                      'S24/OTH': store / 'Study2024' / 'other.md'})
    svc.load()
    return svc


# ── nested folders in lib/library.py ─────────────────────────────────────────

def test_records_load_recursively_and_skip_non_records(library):
    papers, issues = L.load_library(library)
    assert set(papers) == {'src2024', 'meth2023', 'rel2022', 'secret2025', 'nollm2021', 'summ2020'}
    assert 'staged2026' not in papers            # _staging folder skipped
    assert not issues.errors
    assert papers['rel2022'].path.parent.name == '2022'


def test_duplicate_citekey_across_folders_reports_both_paths(library):
    write_record(library / 'papers' / 'lab', 'rel2022')
    _, issues = L.load_library(library)
    message = next(e for e in issues.errors if 'duplicate citekey rel2022' in e)
    assert 'reading/2022/rel2022.md' in message and 'lab/rel2022.md' in message


def test_file_resolves_relative_to_record_folder(library):
    papers, issues = L.load_library(library)
    assert not [w for w in issues.warnings if 'src2024' in w]
    (library / 'papers' / 'datasets' / 'src2024.pdf').unlink()
    _, issues = L.load_library(library)
    assert any('src2024' in w for w in issues.warnings)


def test_folder_precedence_and_lab_authors(tmp_path):
    lib = tmp_path / 'lib'
    assert L.folder_for(lib, 'source') == lib / 'papers' / 'datasets'
    assert L.folder_for(lib, 'reanalysis') == lib / 'papers' / 'datasets'
    assert L.folder_for(lib, 'method') == lib / 'papers' / 'methods'
    assert L.folder_for(lib, 'background') == lib / 'papers' / 'reading'
    assert L.folder_for(lib, 'source', authors=['De Strooper, Bart']) == lib / 'papers' / 'lab'
    assert L.folder_for(lib, 'source', authors=['Doe, J'], lab_authors=['Doe']) == lib / 'papers' / 'lab'
    assert L.folder_for(lib, 'source', authors=['Smith, A'], lab_authors=['Doe']) == lib / 'papers' / 'datasets'
    assert L.folder_for(lib, 'source', 'papers/reading/2024') == lib / 'papers' / 'reading' / '2024'
    assert L.folder_for(lib, 'source', 'lab') == lib / 'papers' / 'lab'
    # proposals win over everything else; only a report whose type mentions a proposal
    lab = ['De Strooper, Bart']
    assert L.folder_for(lib, 'background', authors=lab, entrytype='report',
                        doc_type='Project Proposal') == lib / 'papers' / 'proposals'
    assert L.folder_for(lib, 'source', entrytype='report', doc_type='grant proposal') == lib / 'papers' / 'proposals'
    assert L.folder_for(lib, 'background', entrytype='report', doc_type='technical report') == lib / 'papers' / 'reading'
    assert L.folder_for(lib, 'background', entrytype='article', doc_type='proposal') == lib / 'papers' / 'reading'
    assert L.folder_for(lib, 'background', 'reading', entrytype='report',
                        doc_type='project proposal') == lib / 'papers' / 'reading'
    with pytest.raises(ValueError):
        L.folder_for(lib, 'source', '../outside')
    with pytest.raises(ValueError):
        L.folder_for(lib, 'source', '_staging')


def test_is_lab_paper_accent_and_case_insensitive():
    assert L.is_lab_paper(['de strooper, B'])
    assert L.is_lab_paper(['Smith, A', 'De Strooper, Bart'])
    assert not L.is_lab_paper(['Strooper, X'], ['De Strooper'])


# ── references, access, records ──────────────────────────────────────────────

def test_papers_references_resolve_to_datasets(service):
    state = service.state
    assert state.papers_by_db['S24/CEL'] == [('src2024', 'source'), ('meth2023', 'method')]
    assert state.papers_by_db['S24/OTH'] == [('src2024', 'source')]
    assert state.unresolved == []


def test_legacy_access_fields_do_not_hide_papers(service):
    """2.41: access follows the site login; anthive_access/anthive_llm left in
    a record are ignored, so every paper is listed, fetched and exported."""
    listed = {p['citekey'] for p in service.list()}
    assert {'secret2025', 'nollm2021', 'summ2020'} <= listed
    for citekey in ('secret2025', 'nollm2021', 'summ2020'):
        assert service.get(citekey) is not None
    summary = service.summary(service.get('secret2025'))
    assert 'access' not in summary and 'llm' not in summary


def test_full_record_always_has_notes_abstract_and_related(service):
    record = service.record(service.get('summ2020'))
    assert record['notes'] and record['fields']['abstract'] == 'An abstract.'
    assert 'anthive_llm' not in record['fields']
    record = service.record(service.get('nollm2021'))
    assert record['fields']['related'] == ['secret2025']
    bib = service.bibtex(None)
    assert 'secret2025' in bib and bib.count('\n@') + 1 == 6
    assert 'anthive_access' not in bib and 'anthive_llm' not in bib


def test_list_filters(service):
    by_cohort = {p['citekey'] for p in service.list(cohort='rosmap')}
    assert by_cohort == {'src2024', 'rel2022'}
    assert [p['citekey'] for p in service.list(role='method')] == ['meth2023']
    assert [p['citekey'] for p in service.list(author='doe')] == ['meth2023']
    assert {p['citekey'] for p in service.list(q='source paper summary')} == {'src2024'}


def test_related_kinds_and_evidence(service):
    related = service.related('src2024')
    rel = next(r for r in related if r['citekey'] == 'rel2022')
    kinds = {x['kind'] for x in rel['relations']}
    assert {'explicit', 'cohort', 'celltype', 'cites'} <= kinds
    only = service.related('src2024', kinds=['cites'])
    assert [r['citekey'] for r in only] == ['rel2022']
    assert all(x['kind'] == 'cites' for x in only[0]['relations'])
    back = service.related('rel2022', kinds=['cited_by'])
    assert [r['citekey'] for r in back] == ['src2024']


def test_pdf_path_stays_inside_record_folder(service):
    paper = service.get('src2024')
    assert service.pdf_path(paper).name == 'src2024.pdf'
    assert service.pdf_s3_relpath(paper) == 'datasets/src2024.pdf'
    paper.fields['file'] = '../../etc/passwd.pdf'
    assert service.pdf_path(paper) is None and service.pdf_s3_relpath(paper) is None
    paper.fields['file'] = 'notes.md'
    assert service.pdf_path(paper) is None


# ── search, pages, context ───────────────────────────────────────────────────

def test_search_modes(service, tmp_path, library):
    build_toy_index(tmp_path, library)
    service.load_index()
    result = service.search('microglia amyloid', k=10)
    assert result['mode'] == 'hybrid'
    keys = {r['citekey'] for r in result['results']}
    # legacy anthive_access/anthive_llm values no longer hold passages back
    assert {'src2024', 'nollm2021', 'summ2020', 'secret2025'} <= keys
    methods = service.search('nuclei isolated', sections=['methods'])
    assert methods['results'] and all(r['section'] == 'methods' for r in methods['results'])
    assert '<mark>' in methods['results'][0]['snippet']


def test_fulltext_needs_every_word_in_one_passage_and_groups_per_paper(service, tmp_path, library):
    """GET /search papers (API 2.48): the keyword index, no query model, all words in one passage."""
    build_toy_index(tmp_path, library)
    service.embedder_factory = None
    service.load_index()
    hits = service.fulltext([('microglia',), ('amyloid',)], k=50)
    keys = [hit['citekey'] for hit in hits]
    assert set(keys) == {'src2024', 'nollm2021', 'summ2020', 'secret2025'}
    src = next(hit for hit in hits if hit['citekey'] == 'src2024')
    assert src['n_passages'] == 1 and src['passages'][0]['page_start'] == 1
    assert set(src['passages'][0]) == {'page_start', 'page_end', 'section', 'snippet'}
    assert '<mark>Microglia</mark>' in src['passages'][0]['snippet']
    # "plaque" and "nuclei" never share a passage: no hit
    assert service.fulltext([('plaque',), ('nuclei',)]) == []
    # an alternative of a word counts (synonym / singular); prefix match of longer tokens
    assert [hit['citekey'] for hit in service.fulltext([('trem2',), ('xyz', 'plaque')])] == ['src2024']
    assert {hit['citekey'] for hit in service.fulltext([('microgli',)])} >= {'src2024'}
    # at most per_paper passages; papers without a record are never returned
    assert all(len(hit['passages']) <= 1 for hit in service.fulltext([('microglia',)], per_paper=1))


def test_snippet_shows_the_words_together_and_reference_lists_rank_low():
    import library_index as li
    from library_service import _densest_window
    text = 'microglia ' + 'filler words here ' * 60 + 'lipid droplets accumulate in aged microglia here.'
    start = _densest_window(text, [('lipid',), ('microglia',)])
    assert text[start:start + 60].startswith('lipid droplets')
    snippet = highlight_snippet(text, 'lipid microglia', focus=start)
    assert '<mark>lipid</mark> droplets' in snippet and snippet.startswith('...')
    references = ('12. Chen, D. et al. CTLA-4 blockade. Nat Med 19, 784-790 (2013). 13. Lee, X. et al. '
                  'Microglia mediated. Immunity 56, 2086-2104 (2023). 14. Wu, Y. et al. Glia. Cell 7, 1-9 (2020).')
    assert li.looks_like_citations(references)
    assert not li.looks_like_citations('Microglia near plaques took up lipid droplets (Fig. 2a) in aged mice, '
                                       'as reported before (Lee et al. 2020), and lost homeostatic genes.')


def test_fulltext_leaves_out_overlapping_passages(service):
    class Index:
        rows = [{'citekey': 'src2024', 'page_start': 6, 'page_end': 7, 'section': 'results',
                 'text': 'Aging affects microglia, characterized by lipid droplets, also after plaques appear.'},
                {'citekey': 'src2024', 'page_start': 7, 'page_end': 8, 'section': 'results',
                 'text': 'Aging affects microglia, characterized by lipid droplets, also after plaques appear. More.'},
                {'citekey': 'src2024', 'page_start': 9, 'page_end': 9, 'section': 'discussion',
                 'text': 'In discussion, microglia with lipid droplets are new.'}]

        def match_all(self, word_groups, k=50, citekeys=None):
            return {'results': [(0, 3.0), (1, 2.9), (2, 1.0)], 'per_citekey': {'src2024': 3}}
    service.index = Index()
    hits = service.fulltext([('microglia',), ('lipid',)])
    assert [p['page_start'] for p in hits[0]['passages']] == [6, 9] and hits[0]['n_passages'] == 3
    assert '_texts' not in hits[0]


def test_fulltext_without_index_is_empty(service):
    assert service.fulltext([('microglia',)]) == []


def test_model_mismatch_falls_back_to_keyword(service, tmp_path, library):
    build_toy_index(tmp_path, library, model='other/model')
    service.embedder_factory = lambda model: FakeEmbedder('fake/model-1')
    service.load_index()
    result = service.search('microglia')
    assert result['mode'] == 'keyword'
    assert any('does not match' in w or 'unavailable' in w for w in result['warnings'])


def test_missing_index_reports_warning(service):
    service.load_index()
    result = service.search('microglia')
    assert result['results'] == [] and result['warnings']
    assert service.status()['index']['loaded'] is False


def test_page_text(service, tmp_path, library):
    build_toy_index(tmp_path, library)
    service.load_index()
    paper = service.get('src2024')
    assert 'Nuclei were isolated' in service.page_text(paper, 5)
    assert service.page_text(paper, 99) is None and service.n_pages(paper) == 7
    assert not hasattr(service, 'full_text_allowed')


def test_context_pack_and_budget(service, tmp_path, library):
    build_toy_index(tmp_path, library)
    service.load_index()
    info = {'title': 'Cells', 'organism': 'human', 'n_cells': 100, 'n_genes': 50,
            'obs': {'categorical': ['ant_donor', 'cluster'], 'numerical': ['ant_age']},
            'caveats': [{'level': 'high', 'message': 'ambient RNA'}], 'description': 'A dataset.'}
    full = service.context('S24/CEL', info, module_scores=['A::B::C'], level='full', budget_tokens=5000)
    names = [s['name'] for s in full['sections']]
    assert 'source:src2024' in names and 'caveats' in names and 'module_scores' in names
    assert any(n.startswith('passage:src2024:methods') for n in names)
    assert 'ambient RNA' in full['text'] and 'ant_donor' in full['text']
    assert full['papers'][0] == {'citekey': 'src2024', 'role': 'source', 'citation': 'Smith et al. 2024'}
    small = service.context('S24/CEL', info, level='full', budget_tokens=200)
    assert small['truncated'] and small['tokens'] <= 200 and small['dropped_sections']
    assert [s['name'] for s in small['sections']][0] == 'dataset'


def test_list_and_record_return_plain_text_not_html_entities(tmp_path):
    papers = tmp_path / 'library' / 'papers'
    write_record(papers / 'lab', 'amp2025', journaltitle="Alzheimer's &amp; Dementia",
                 title='Plasma &amp;amp;lt; CSF', anthive_role='background')
    svc = LibraryService(tmp_path / 'library', None, None)
    svc.load()
    row = next(r for r in svc.list() if r['citekey'] == 'amp2025')
    assert row['journal'] == "Alzheimer's & Dementia"
    assert row['title'] == 'Plasma < CSF'
    assert svc.list(q='alzheimer & dementia')[0]['citekey'] == 'amp2025'


def test_highlight_snippet_escapes_ampersands():
    snippet = highlight_snippet('Microglia & astrocytes, P < 0.05', 'microglia')
    assert snippet == '<mark>Microglia</mark> &amp; astrocytes, P &lt; 0.05'


def test_highlight_snippet_skips_stopwords():
    snippet = highlight_snippet('Nuclei were isolated from the cortex with care.', 'isolated from cortex')
    assert '<mark>isolated</mark>' in snippet and '<mark>cortex</mark>' in snippet
    assert '<mark>from</mark>' not in snippet


def test_geneset_references(tmp_path, library):
    gs = tmp_path / 'genesets'
    (gs / 'Study' / 'exp1').mkdir(parents=True)
    (gs / 'Study' / 'index.md').write_text('---\npapers:\n  src2024: source\n---\n')
    (gs / 'Study' / 'exp1' / 'index.md').write_text('---\npapers:\n  rel2022: related\n---\n')
    svc = LibraryService(library, None, None, gs)
    svc.load()
    assert svc.state.geneset_links['src2024'] == [('Study', 'source')]
    assert svc.state.geneset_links['rel2022'] == [('Study/exp1', 'related')]


# ── library S3 sync (fake client) ────────────────────────────────────────────

class FakeS3:
    def __init__(self):
        self.objects = {}            # key -> (bytes, metadata)

    def get_paginator(self, _name):
        fake = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803 - boto3 names
                yield {'Contents': [{'Key': k, 'Size': len(v[0])} for k, v in sorted(fake.objects.items())
                                    if k.startswith(Prefix)]}
        return Paginator()

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)
        return {'Metadata': self.objects[Key][1]}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):  # noqa: N803
        self.objects[key] = (Path(filename).read_bytes(), dict((ExtraArgs or {}).get('Metadata') or {}))

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[key][0])


def test_library_sync_mirrors_folders_and_reports_moves(tmp_path, library):
    import library_sync as ls
    index_dir = tmp_path / 'index'
    index_dir.mkdir()
    (index_dir / 'library_chunks.parquet').write_bytes(b'PAR1')
    (index_dir / 'library_chunks.json').write_text('{}')
    (index_dir / 'other.parquet').write_bytes(b'x')
    s3 = FakeS3()
    areas = ls.library_areas(library, index_dir)
    logs = []
    reports = [ls.push_area(s3, 'b', 'pre', a, say=logs.append) for a in areas]
    assert 'pre/_library/papers/datasets/src2024.pdf' in s3.objects
    assert 'pre/_library_index/library_chunks.parquet' in s3.objects
    assert 'pre/_library_index/library_chunks.json' in s3.objects
    assert 'pre/_library_index/other.parquet' not in s3.objects
    assert not any(k.endswith('.md') for k in s3.objects)
    assert reports[0].transferred == ['datasets/src2024.pdf']
    # unchanged: nothing uploaded again
    again = ls.push_area(s3, 'b', 'pre', areas[0], say=logs.append)
    assert again.transferred == [] and again.up_to_date == 1
    # a paper moves to another folder: new key uploaded, old key reported, not deleted
    src = library / 'papers' / 'datasets' / 'src2024.pdf'
    (library / 'papers' / 'lab').mkdir(exist_ok=True)
    src.rename(library / 'papers' / 'lab' / 'src2024.pdf')
    moved = ls.push_area(s3, 'b', 'pre', areas[0], say=logs.append)
    assert moved.transferred == ['lab/src2024.pdf']
    assert moved.remote_only == ['datasets/src2024.pdf']
    assert 'pre/_library/papers/datasets/src2024.pdf' in s3.objects
    assert any('S3 ONLY' in line for line in logs)


def test_library_sync_pull(tmp_path, library):
    import library_sync as ls
    s3 = FakeS3()
    s3.objects['_library/papers/reading/2024/new.pdf'] = (b'%PDF new', {'original-mtime': str(int(time.time()) - 100)})
    s3.objects['_library_index/library_chunks.parquet'] = (b'PAR1', {})
    target_index = tmp_path / 'fresh_index'
    reports = [ls.pull_area(s3, 'b', '', a, create_root=(a.key_prefix == ls.INDEX_S3_PREFIX), say=lambda m: None)
               for a in ls.library_areas(library, target_index)]
    assert (library / 'papers' / 'reading' / '2024' / 'new.pdf').read_bytes() == b'%PDF new'
    assert (target_index / 'library_chunks.parquet').is_file()
    assert reports[0].local_only == ['datasets/src2024.pdf']
    # no library checkout: PDFs are skipped, not dumped somewhere
    reports = [ls.pull_area(s3, 'b', '', a, create_root=False, say=lambda m: None)
               for a in ls.library_areas(tmp_path / 'absent', None)]
    assert reports[0].skipped_reason


def test_ant_sync_store_ignores_library_prefixes():
    """The h5adstore sync must never pick up the _library prefixes."""
    tree = ast.parse((REPO / 'bin' / 'ops' / 'ant-sync').read_text())
    prefixes = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(getattr(t, 'id', '') == 'SYNC_PATH_PREFIXES' for t in node.targets))
    values = [elt.value for elt in prefixes.elts]
    assert not any(v.startswith('_library') for v in values)


# ── config, ant-stack, MCP surface ───────────────────────────────────────────

def test_config_helpers(monkeypatch, tmp_path):
    import config
    monkeypatch.setenv('ANTHIVE_LIBRARY_INDEX', str(tmp_path / 'idx'))
    assert config.get_library_index_dir({}) == tmp_path / 'idx'
    monkeypatch.delenv('ANTHIVE_MODEL_CACHE', raising=False)
    monkeypatch.setenv('FASTEMBED_CACHE_PATH', '/opt/fastembed')
    assert config.get_model_cache_dir({'model_cache': ''}) == Path('/opt/fastembed')
    monkeypatch.setenv('ANTHIVE_MODEL_CACHE', str(tmp_path / 'models'))
    assert config.get_model_cache_dir({}) == tmp_path / 'models'
    assert config.get_library_lab_authors({'library_lab_authors': ['X']}) == ['X']
    assert config.get_library_lab_authors({}) == ['De Strooper']


def _ant_stack_namespace():
    path = REPO / 'bin' / 'ops' / 'ant-stack'
    source = path.read_text()
    namespace = {'__name__': 'ant_stack_test', '__file__': str(path)}
    exec(compile(source.split("\nif __name__ == '__main__':")[0], str(path), 'exec'), namespace)
    return namespace


def test_ant_stack_renders_library_mounts(tmp_path):
    pytest.importorskip('bcrypt')
    ns = _ant_stack_namespace()
    lib, idx = tmp_path / 'library', tmp_path / 'library_index'
    (lib / 'papers').mkdir(parents=True)
    idx.mkdir()
    site = {'_h5adstore': tmp_path / 'store', '_genesetstore': None, '_starcatstore': None,
            '_library': lib, '_library_index': idx, '_state': tmp_path / 'state', '_scratch': None,
            'data': {'readonly': False}, 'site': {'name': 'x'}}
    compose = ns['render_compose_site'].__wrapped__(site) if hasattr(ns['render_compose_site'], '__wrapped__') else None
    if compose is None:
        ns['_listen_and_ports'] = lambda s: ('', [], '')
        ns['stack_dir'] = lambda s: tmp_path / 'stack'
        compose = ns['render_compose_site'](site)
    api = compose['services']['api']['volumes']
    ingest = compose['services']['ingest']['volumes']
    api_lib = [v for v in api if v.get('source') == str(lib)]
    ingest_lib = [v for v in ingest if v.get('source') == str(lib)]
    assert api_lib and api_lib[0].get('read_only') is True and api_lib[0]['target'] == str(lib)
    assert ingest_lib and not ingest_lib[0].get('read_only')
    assert any(v.get('source') == str(idx) for v in api)


def test_ant_stack_check_warns_on_index_model_mismatch(tmp_path, capsys):
    pytest.importorskip('bcrypt')
    ns = _ant_stack_namespace()
    lib, idx = tmp_path / 'library', tmp_path / 'library_index'
    (lib / 'papers').mkdir(parents=True)
    idx.mkdir()
    (idx / 'library_chunks.parquet').write_bytes(b'PAR1')
    (idx / 'library_chunks.json').write_text(json.dumps({'embedding_model': 'other/model'}))
    ns['_check_library']({'_library': lib, '_library_index': idx})
    err = capsys.readouterr()
    assert 'other/model' in (err.out + err.err)
    (idx / 'library_chunks.parquet').unlink()
    ns['_check_library']({'_library': lib, '_library_index': idx})
    err = capsys.readouterr()
    assert 'missing' in (err.out + err.err)


def test_mcp_literature_surface_declared():
    tree = ast.parse((REPO / 'bin' / 'api' / 'ant-mcp').read_text())
    tools, prompts, resources = set(), set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            if dec.func.attr == 'tool':
                tools.add(node.name)
            elif dec.func.attr == 'prompt':
                prompts.add(next(k.value.value for k in dec.keywords if k.arg == 'name'))
            elif dec.func.attr == 'resource':
                resources.add(dec.args[0].value)
    assert {'search_literature', 'get_paper', 'related_papers', 'get_dataset_context'} <= tools
    assert prompts == {'investigate_dataset', 'literature_in_our_data'}
    assert resources == {'anthive://guide', 'anthive://dataset/{group_id}/{file_id}/context',
                         'anthive://help', 'anthive://help/{topic}'}


def test_serve_registers_library_routes():
    source = (REPO / 'bin' / 'api' / 'ant-serve').read_text()
    for route in ('"/papers"', '"/papers/{citekey}"', '"/papers/{citekey}.bib"', '"/papers/{citekey}/related"',
                  '"/papers/{citekey}/file"', '"/papers/{citekey}/pages/{page}"', '"/library.bib"',
                  '"/library/status"', '"/search/literature"', '"/databases/{db_id:path}/papers"',
                  '"/databases/{db_id:path}/context"'):
        assert f'@app.get({route}' in source, route
    # the .bib route must be registered before the bare /papers/{citekey}
    assert source.index('"/papers/{citekey}.bib"') < source.index('@app.get("/papers/{citekey}", ')


def _wait_reload(svc, timeout=5.0):
    import time
    end = time.time() + timeout
    while svc._reloading and time.time() < end:
        time.sleep(0.02)


def test_refresh_picks_up_an_index_replaced_on_disk(tmp_path, service, library):
    import os
    out = build_toy_index(tmp_path, library)
    service.load_index()
    assert service.index is not None and service.index.search('TREM2', k=3)['results']
    assert service.refresh_if_changed(min_interval=0) is False            # nothing changed
    out.unlink()                                                          # a sync swaps the file
    build_toy_index(tmp_path, library)
    os.utime(out, ns=(1, 1))                                              # different signature
    before = service.index
    assert service.refresh_if_changed(min_interval=0) is True
    _wait_reload(service)
    assert service.index is not None and service.index is not before


def test_refresh_picks_up_new_records_from_git(tmp_path, service, library):
    import subprocess
    def git(*args):
        subprocess.run(['git', '-C', str(library), *args], check=True, capture_output=True)
    git('init', '-q', '-b', 'main')
    git('-c', 'user.email=t@t', '-c', 'user.name=t', 'add', '.')
    git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'one')
    service.load()
    assert 'new2026' not in service.state.papers
    assert service.refresh_if_changed(min_interval=0) is False
    write_record(library / 'papers' / 'reading', 'new2026', anthive_role='background')
    git('-c', 'user.email=t@t', '-c', 'user.name=t', 'add', '.')
    git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'two')   # what a git pull does
    assert service.refresh_if_changed(min_interval=0) is True
    _wait_reload(service)
    assert 'new2026' in service.state.papers


def test_refresh_is_throttled(service):
    service._last_change_check = 0.0
    service.refresh_if_changed(min_interval=3600)
    assert service.refresh_if_changed(min_interval=3600) is False
