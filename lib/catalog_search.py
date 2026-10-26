"""Keyword search over the catalogue and browse lists by disease and cell type.

One module for the unified search (``GET /search`` in ant-serve) and the
"Browse by disease / cell type" pages of the web UI, so both read the
catalogue the same way.

Inputs are the dataset records of ``GET /databases`` (already filtered to
what the caller may see: this module never widens access), the gene names of
the precomputed gene statistics index, the gene set summaries of
``GET /genesets`` and the paper summaries of the library. Nothing here reads
DuckDB or h5ad files.

Matching rules (plain, documented on the help page):

* The query is lower-cased, accents and possessives are dropped
  ("Alzheimer's" -> "alzheimer") and common filler words are removed
  ("expression", "in", "which", "datasets", ...).
* A word that is the exact name of a gene in the gene index (case ignored) is
  a gene word. Gene words list the gene; they do not filter datasets, because
  nearly every dataset measures nearly every gene.
* A word that names a disease, cell type, organism or tissue recorded for
  some dataset of the catalogue must match that field ("microglia" finds
  datasets recorded as microglia, not a neuron dataset whose description
  mentions microglia).
* Every other word must occur in a dataset's text (title, collection, paper
  title, description, organism, tissue, cell type, disease, condition,
  technology and the labels of its standard cell annotation columns). A word
  matches inside a longer word ("microglia" finds "microglial cell"); a
  trailing plural s is optional; a few abbreviations are expanded (AD, PD,
  MS, ALS, FTD, APOE4). Words of one or two letters must match a whole word.
* With gene words only, datasets whose text mentions the gene are listed.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

# Words that carry no search meaning in a question typed by a biologist.
STOPWORDS: frozenset = frozenset({
    'a', 'an', 'the', 'in', 'of', 'on', 'for', 'and', 'or', 'with', 'within', 'without', 'across', 'among',
    'between', 'by', 'to', 'from', 'at', 'as', 'is', 'are', 'be', 'do', 'does', 'did', 'have', 'has', 'had',
    'which', 'what', 'where', 'who', 'how', 'when', 'any', 'all', 'some', 'there', 'that', 'this', 'these',
    'those', 'me', 'my', 'show', 'find', 'list', 'give', 'get', 'see', 'about', 'vs', 'versus', 'than',
    'expression', 'expressed', 'expressing', 'level', 'levels', 'dataset', 'datasets', 'data', 'study',
    'studies', 'cell', 'cells', 'gene', 'genes',
})

# Abbreviations a biologist types, expanded to the words the catalogue uses. A word matches when any of its
# alternatives matches.
SYNONYMS: Dict[str, Tuple[str, ...]] = {
    'ad': ('alzheimer',),
    'alzheimers': ('alzheimer',),
    'pd': ('parkinson',),
    'parkinsons': ('parkinson',),
    'ms': ('multiple sclerosis',),
    'als': ('amyotrophic lateral sclerosis',),
    'ftd': ('frontotemporal',),
    'dlb': ('lewy bod',),
    'psp': ('progressive supranuclear',),
    'hd': ('huntington',),
    'apoe4': ('apoe4', 'apoe e4'),
    'e4': ('apoe4', 'apoe e4'),
    'microglial': ('microglia',),
    'astrocytes': ('astrocyte',),
    'snrna-seq': ('snrna', 'single nucleus', 'nucleus'),
    'scrna-seq': ('scrna', 'single cell'),
}

# Plain names for cell type ontology labels (shared with the web UI's landing list).
CELLTYPE_PLAIN: Dict[str, str] = {
    'microglial cell': 'microglia', 'microglia': 'microglia',
    'astrocyte': 'astrocytes', 'oligodendrocyte': 'oligodendrocytes',
    'oligodendrocyte precursor cell': 'OPCs', 'neuron': 'neurons',
    'excitatory neuron': 'excitatory neurons', 'inhibitory neuron': 'inhibitory neurons',
    'myeloid cell': 'myeloid cells', 'macrophage': 'macrophages',
    'endothelial cell': 'endothelial cells', 'pericyte': 'pericytes', 'pericyte cell': 'pericytes',
    'vascular associated smooth muscle cell': 'smooth muscle cells',
    'vascular leptomeningeal cell': 'vascular leptomeningeal cells',
    'glutamatergic neuron': 'glutamatergic neurons', 'gabaergic neuron': 'GABAergic neurons',
    'dopaminergic neuron': 'dopaminergic neurons', 'monocyte': 'monocytes',
    'mixed': 'several cell types',
}
# Disease values that describe control donors rather than a condition.
CONTROL_DISEASE_VALUES: frozenset = frozenset({'normal', 'control', 'healthy', 'none', 'not applicable'})
_SKIP_VALUES: frozenset = frozenset({'', 'na', 'n/a', 'null', 'unknown', 'other'})

# Standard (ant_*) cell annotation columns that describe the cells' donors or conditions; their labels
# make "APOE e4 carrier" or "Braak stage" findable. Structural columns (donor ids, author clusters) are
# left out of the search text.
_ANNOTATION_SKIP_TAGS: frozenset = frozenset({'donor', 'donor_uid'})


def fold(text: Any) -> str:
    """Lower-case, accent-free, whitespace-collapsed text."""
    decomposed = unicodedata.normalize('NFKD', str(text if text is not None else ''))
    stripped = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    return ' '.join(stripped.lower().split())


def as_list(value: Any) -> List[str]:
    """A metadata value as a list of non-empty strings (lists kept, scalars wrapped)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def query_words(query: str) -> List[str]:
    """The search words of a query: folded, possessives and punctuation dropped, filler words removed.

    Hyphens and dots inside a word are kept so gene names such as HLA-DRA survive. Duplicates are removed,
    the order is kept.
    """
    text = fold(query).replace('’', "'")
    text = re.sub(r"'s\b", '', text)
    words: List[str] = []
    for raw in re.split(r"[^\w.\-+]+", text):
        word = raw.strip('.-+_')
        if not word or word in STOPWORDS or word in words:
            continue
        words.append(word)
    return words


def word_alternatives(word: str) -> Tuple[str, ...]:
    """The strings that count as a match for ``word`` (itself, synonyms, the singular)."""
    alternatives = [word]
    alternatives += list(SYNONYMS.get(word, ()))
    if len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
        alternatives.append(word[:-1])
    return tuple(dict.fromkeys(alternatives))


def paper_query(words: Sequence[str]) -> str:
    """The words for the library's plain substring search: abbreviations expanded, plural s dropped."""
    out = []
    for word in words:
        alternatives = SYNONYMS.get(word)
        if alternatives:
            out.append(alternatives[0].split()[0])
        elif len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
            out.append(word[:-1])
        else:
            out.append(word)
    return ' '.join(out)


def word_matches(word: str, text: str) -> bool:
    """True when ``word`` (or one of its alternatives) occurs in the folded ``text``.

    Words of one or two letters only match a whole word, so 'ad' does not find 'adult'.
    """
    for alternative in word_alternatives(word):
        if len(alternative) <= 2:
            if re.search(r'(?<![a-z0-9])' + re.escape(alternative) + r'(?![a-z0-9])', text):
                return True
        elif alternative in text:
            return True
    return False


# ── dataset fields ────────────────────────────────────────────────────────────

def disease_values(db: Mapping[str, Any]) -> List[str]:
    """The disease values of a dataset as recorded (a list item may contain commas: never split on them)."""
    values: List[str] = []
    for item in as_list(db.get('disease')):
        for part in re.split(r'\s*[|;]\s*', item):
            part = part.strip()
            if part and part.lower() not in _SKIP_VALUES and part not in values:
                values.append(part)
    return values


def disease_label(value: str) -> str:
    """Readable disease label: 'essential_tremor' -> 'essential tremor', control words -> 'control donors'."""
    text = str(value).strip().replace('_', ' ')
    if text.lower() in CONTROL_DISEASE_VALUES:
        return 'control donors'
    return text


def celltype_values(db: Mapping[str, Any]) -> List[str]:
    """The dataset-level cell type values (``celltype``, else ``cell_type``)."""
    raw = as_list(db.get('celltype')) or as_list(db.get('cell_type'))
    values: List[str] = []
    for item in raw:
        for part in str(item).split(','):
            part = part.strip()
            if part and part.lower() not in _SKIP_VALUES and part not in values:
                values.append(part)
    return values


def celltype_label(value: str) -> str:
    """Plain cell type name ('microglial cell' -> 'microglia')."""
    return CELLTYPE_PLAIN.get(str(value).strip().lower(), str(value).strip())


def annotation_columns(db: Mapping[str, Any]) -> List[str]:
    """Names of the standard ``ant_*`` cell annotation columns of a dataset."""
    obs = db.get('obs') or {}
    names: List[str] = []
    if isinstance(obs, Mapping):
        for kind in ('categorical', 'numerical'):
            for name in obs.get(kind) or []:
                name = str(name)
                if name.startswith('ant_') and name not in names:
                    names.append(name)
    return names


def annotation_label(column: str, labels: Optional[Mapping[str, str]] = None) -> str:
    """Human label of an ``ant_*`` column from the tag vocabulary ('ant_apoe4_carrier' -> 'APOE e4 carrier')."""
    tag = column[4:] if column.startswith('ant_') else column
    if labels and labels.get(tag):
        return str(labels[tag])
    return tag.replace('_', ' ').capitalize()


def vocabulary_labels() -> Dict[str, str]:
    """``{tag: label}`` from ``lib/obscol_tags_vocab.yaml`` (empty when it cannot be read)."""
    try:
        import obscol_tags  # noqa: PLC0415
        tags = (obscol_tags.load_vocab() or {}).get('tags') or {}
    except Exception:  # pragma: no cover - vocabulary missing or yaml absent
        return {}
    return {str(tag): str(entry.get('label')) for tag, entry in tags.items()
            if isinstance(entry, Mapping) and entry.get('label')}


def dataset_fields(db: Mapping[str, Any], labels: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The folded text of a dataset per field, as searched. Keys name the field in plain words."""
    collection = db.get('collection') or {}
    annotations = [column for column in annotation_columns(db)
                   if column[4:] not in _ANNOTATION_SKIP_TAGS]
    fields = {
        'title': ' '.join([str(db.get('title') or ''), str(db.get('basename') or ''), str(db.get('id') or ''),
                           str(db.get('file_id') or '')]),
        'collection': ' '.join([str(collection.get('title') or ''), str(collection.get('group') or ''),
                                str(db.get('group_id') or ''), str(collection.get('summary') or ''),
                                ' '.join(as_list(collection.get('authors')))]),
        'disease': ' '.join(disease_values(db) + [disease_label(v) for v in disease_values(db)]
                            + as_list(db.get('condition'))),
        'cell type': ' '.join(celltype_values(db) + [celltype_label(v) for v in celltype_values(db)]),
        'organism': ' '.join(as_list(db.get('organism'))),
        'tissue': ' '.join(as_list(db.get('tissue')) + as_list(db.get('region'))),
        'technology': ' '.join(as_list(db.get('technology')) + as_list(db.get('suspension_type'))
                               + as_list(db.get('experimental_system'))).replace('_', ' '),
        'cell annotations': ' '.join([annotation_label(c, labels) for c in annotations] + annotations),
        'description': ' '.join([str(db.get('description') or ''), str(db.get('markdown') or '')[:1500]]),
    }
    return {key: fold(value) for key, value in fields.items()}


# ── search ────────────────────────────────────────────────────────────────────

class GeneIndex:
    """Gene names known to the catalogue, looked up without case.

    Built from (dataset, gene) pairs so a gene can report how many of the caller's visible datasets hold it.
    """

    def __init__(self, pairs: Iterable[Tuple[str, str]]) -> None:
        self._datasets: Dict[str, Set[str]] = defaultdict(set)
        self._names: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for dataset, gene in pairs:
            key = str(gene).lower()
            self._datasets[key].add(str(dataset))
            self._names[key][str(gene)] += 1

    def __len__(self) -> int:
        return len(self._datasets)

    def canonical(self, word: str) -> Optional[str]:
        """The gene name as stored (the upper-case spelling when there is one), None if unknown."""
        names = self._names.get(word.lower())
        if not names:
            return None
        upper = [name for name in names if name == name.upper()]
        return upper[0] if upper else max(names.items(), key=lambda item: item[1])[0]

    def datasets(self, word: str) -> Set[str]:
        return set(self._datasets.get(word.lower(), set()))

    def prefixed(self, prefix: str, limit: int = 8) -> List[str]:
        """Up to ``limit`` gene names starting with ``prefix`` (case ignored), shortest first."""
        prefix = prefix.lower()
        keys = sorted((key for key in self._datasets if key.startswith(prefix) and key != prefix),
                      key=lambda key: (len(key), key))
        return [self.canonical(key) or key for key in keys[:limit]]


# Curated fields: a word that names one of their values somewhere in the catalogue must match that field.
SCOPED_FIELDS: Tuple[str, ...] = ('disease', 'cell type', 'organism', 'tissue')


def _gene_like(word: str) -> bool:
    return bool(re.fullmatch(r'[a-z0-9][a-z0-9.\-]{1,19}', word)) and any(ch.isalpha() for ch in word)


def search_datasets(databases: Sequence[Mapping[str, Any]], words: Sequence[str],
                    labels: Optional[Mapping[str, str]] = None, limit: int = 50,
                    scoped: bool = True) -> Tuple[List[Dict[str, Any]], int]:
    """Datasets whose text holds every word. Returns (rows, total matches).

    A word that names a value of a curated field somewhere in the catalogue (a disease, cell type, organism
    or tissue) must match that field: "microglia" then finds datasets recorded as microglia, not a neuron
    dataset whose description mentions microglia. Any other word may match anywhere in the dataset's text.
    ``scoped=False`` lets every word match anywhere (used for the mentions of a gene).
    Rows are ordered by how many words hit the core fields (title, disease, cell type, tissue, organism,
    cell annotations), then primary data before reanalyses, then by cell count.
    """
    if not words:
        return [], 0
    core = ('title', 'disease', 'cell type', 'tissue', 'organism', 'cell annotations', 'collection')
    all_fields = [(db, dataset_fields(db, labels)) for db in databases]
    scopes: Dict[str, Tuple[str, ...]] = {}
    for word in words:
        scopes[word] = tuple(name for name in SCOPED_FIELDS
                             if scoped and any(word_matches(word, fields[name]) for _, fields in all_fields))
    hits: List[Tuple[Tuple, Dict[str, Any]]] = []
    for db, fields in all_fields:
        everything = ' '.join(fields.values())
        if not all(word_matches(word, ' '.join(fields[name] for name in scopes[word]) if scopes[word] else everything)
                   for word in words):
            continue
        matched_in: List[str] = []
        core_hits = 0
        for word in words:
            where = [name for name, text in fields.items() if word_matches(word, text)]
            if any(name in core for name in where):
                core_hits += 1
            for name in where[:2]:
                if name not in matched_in:
                    matched_in.append(name)
        row = dataset_row(db)
        row['matched_in'] = matched_in
        designation_rank = 0 if (db.get('designation') or 'primary') == 'primary' else 1
        hits.append(((-core_hits, designation_rank, -(db.get('n_cells') or 0), row['title'].lower()), row))
    hits.sort(key=lambda item: item[0])
    return [row for _, row in hits[:limit]], len(hits)


def dataset_row(db: Mapping[str, Any]) -> Dict[str, Any]:
    """The few fields a result list shows for a dataset."""
    collection = db.get('collection') or {}
    db_id = str(db.get('id') or '')
    group_id = str(db.get('group_id') or db_id.split('/')[0])
    file_id = str(db.get('file_id') or (db_id.split('/', 1)[1] if '/' in db_id else db_id))
    return {
        'db_id': db_id,
        'group_id': group_id,
        'file_id': file_id,
        'title': str(db.get('title') or file_id),
        'collection': str((collection.get('title') or '').strip() or collection.get('group') or group_id),
        'organism': as_list(db.get('organism')),
        'tissue': as_list(db.get('tissue')),
        'cell_types': [celltype_label(v) for v in celltype_values(db)],
        'diseases': [disease_label(v) for v in disease_values(db)],
        'n_cells': db.get('n_cells'),
        'designation': db.get('designation'),
    }


def _geneset_text(summary: Mapping[str, Any]) -> str:
    return fold(' '.join(str(summary.get(key) or '') for key in
                         ('name', 'display_name', 'study', 'experiment', 'description', 'method', 'organism'))
                + ' ' + ' '.join(as_list(summary.get('celltypes'))))


def search_genesets(genesets: Sequence[Mapping[str, Any]], words: Sequence[str],
                    limit: int = 20) -> Tuple[List[Dict[str, Any]], int]:
    """Gene sets whose name, study, experiment, description or cell types hold every word."""
    if not words:
        return [], 0
    rows = []
    for summary in genesets:
        text = _geneset_text(summary)
        if all(word_matches(word, text) for word in words):
            rows.append(_geneset_row(summary))
    rows.sort(key=lambda row: (row['study'].lower(), row['experiment'].lower(), row['name'].lower()))
    return rows[:limit], len(rows)


def _geneset_row(summary: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        'study': str(summary.get('study') or ''),
        'experiment': str(summary.get('experiment') or ''),
        'name': str(summary.get('name') or ''),
        'display_name': str(summary.get('display_name') or summary.get('name') or ''),
        'description': str(summary.get('description') or '')[:300],
        'n_genes': summary.get('n_genes'),
        'organism': summary.get('organism'),
    }


def _title_hits(words: Sequence[str], title: str) -> int:
    folded = fold(title)
    return sum(1 for word in words if word_matches(word, folded))


def search_collections(databases: Sequence[Mapping[str, Any]], words: Sequence[str],
                       matched_db_ids: Set[str],
                       analysis_collections: Optional[Sequence[Mapping[str, Any]]] = None,
                       limit: int = 50) -> Tuple[List[Dict[str, Any]], int]:
    """Collections whose own text (title, folder name, code, summary, description, authors, year, DOI,
    PubMed id, accession) holds every word, or that hold a matching dataset.

    Collections come from the visible datasets (a collection is visible when it has a visible dataset, as
    ``GET /groups/{id}``) plus the visible analysis collections the caller passes.
    """
    if not words:
        return [], 0
    groups: Dict[str, Dict[str, Any]] = {}
    for db in databases:
        group_id = str(db.get('group_id') or str(db.get('id', '')).split('/')[0])
        entry = groups.setdefault(group_id, {'group_id': group_id, 'meta': db.get('collection') or {},
                                             'db_ids': [], 'kind': 'datasets'})
        entry['db_ids'].append(str(db.get('id')))
    for collection in analysis_collections or []:
        group_id = str(collection.get('group_id') or '')
        if group_id and group_id not in groups:
            groups[group_id] = {'group_id': group_id, 'meta': dict(collection), 'db_ids': [], 'kind': 'analysis'}
    hits = []
    for group_id, entry in groups.items():
        meta = entry['meta']
        title = str((meta.get('title') or '').strip() or meta.get('group') or group_id)
        text = fold(' '.join([title, str(meta.get('group') or ''), group_id, str(meta.get('summary') or ''),
                              str(meta.get('description') or '')[:2000], ' '.join(as_list(meta.get('authors'))),
                              str(meta.get('year') or ''), str(meta.get('doi') or ''), str(meta.get('pmid') or ''),
                              str(meta.get('accession') or '')]))
        n_matching = len(matched_db_ids.intersection(entry['db_ids']))
        own = all(word_matches(word, text) for word in words)
        if not own and not n_matching:
            continue
        summary = str(meta.get('summary') or '').strip()
        hits.append(((-int(own) * 1000 - _title_hits(words, title) * 100 - n_matching, title.lower()), {
            'group_id': group_id, 'title': title, 'kind': entry['kind'],
            'summary': '' if summary.lower() in ('', 'na') else summary[:300],
            'year': meta.get('year'), 'n_datasets': len(entry['db_ids']), 'n_matching': n_matching,
            'matched_in': ['collection'] if own else ['its datasets'],
        }))
    hits.sort(key=lambda item: item[0])
    return [row for _, row in hits[:limit]], len(hits)


def search_artefacts(artefacts: Sequence[Mapping[str, Any]], words: Sequence[str],
                     limit: int = 50) -> Tuple[List[Dict[str, Any]], int]:
    """Artefacts (already filtered to what the caller may see) whose id, title, description, kind, scope or
    dataset hold every word. Title hits first."""
    if not words:
        return [], 0
    hits = []
    for item in artefacts:
        title = str(item.get('title') or item.get('artefact_id') or '')
        kind = str(item.get('kind') or '')
        text = fold(' '.join([str(item.get('artefact_id') or ''), title, str(item.get('description') or ''),
                              kind, kind.replace('_', ' '), str(item.get('scope') or ''), str(item.get('db_id') or ''),
                              str(item.get('group_id') or ''), str(item.get('ref') or '')]))
        if not all(word_matches(word, text) for word in words):
            continue
        hits.append(((-_title_hits(words, title), title.lower()), {
            'ref': item.get('ref'), 'artefact_id': item.get('artefact_id'), 'title': title, 'kind': kind,
            'group_id': item.get('group_id'), 'scope': item.get('scope'), 'db_id': item.get('db_id'),
            'description': str(item.get('description') or '')[:300], 'updated': item.get('updated'),
            'withdrawn': bool(item.get('withdrawn')),
        }))
    hits.sort(key=lambda item: item[0])
    return [row for _, row in hits[:limit]], len(hits)


def search_facets(facets: Sequence[Mapping[str, Any]], words: Sequence[str],
                  limit: int = 50) -> Tuple[List[Dict[str, Any]], int]:
    """Facets (``{column, tag, label, summary, definition, n_datasets}``) whose name, label, summary or
    definition hold every word. Label hits first."""
    if not words:
        return [], 0
    hits = []
    for facet in facets:
        label = str(facet.get('label') or facet.get('tag') or '')
        text = fold(' '.join(str(facet.get(key) or '') for key in ('column', 'tag', 'label', 'summary', 'definition'))
                    + ' ' + str(facet.get('tag') or '').replace('_', ' '))
        if not all(word_matches(word, text) for word in words):
            continue
        hits.append(((-_title_hits(words, label), label.lower()), {
            'column': facet.get('column'), 'label': label, 'summary': str(facet.get('summary') or '')[:300],
            'n_datasets': facet.get('n_datasets')}))
    hits.sort(key=lambda item: item[0])
    return [row for _, row in hits[:limit]], len(hits)


def help_entries(hidden: Sequence[str] = ()) -> List[Dict[str, str]]:
    """The help topics (with their subsections) and glossary terms as searchable entries, without the topics,
    terms and passages of parts switched off (``hidden``). ``anchor`` is the id on the help page."""
    import help_content  # noqa: PLC0415
    hidden = set(hidden or ())
    topics = {topic.id: topic for topic in help_content.topics()}
    parent_of = {child: topic.id for topic in topics.values() for child in topic.children}
    entries = []
    for topic in topics.values():
        parent = topics.get(parent_of.get(topic.id, ''))
        if (topic.ui and topic.ui in hidden) or (parent is not None and parent.ui and parent.ui in hidden):
            continue
        body = help_content.html_to_text(help_content.strip_ui(topic.body_html, hidden))
        entries.append({'kind': 'topic', 'anchor': topic.id, 'title': topic.title, 'summary': topic.summary,
                        'text': ' '.join([topic.title, ' '.join(topic.aliases), topic.summary, body]),
                        'parent': parent.title if parent is not None else ''})
    for term in help_content.glossary():
        if term.ui and term.ui in hidden:
            continue
        definition = help_content.html_to_text(help_content.strip_ui(term.definition_html, hidden))
        entries.append({'kind': 'term', 'anchor': f'glossary-{term.slug}', 'title': term.term,
                        'summary': definition[:240], 'text': f'{term.term} {definition}', 'parent': 'Glossary'})
    return entries


def search_help(entries: Sequence[Mapping[str, str]], words: Sequence[str],
                limit: int = 50) -> Tuple[List[Dict[str, Any]], int]:
    """Help topics and glossary terms whose title, aliases, summary or text hold every word; title hits first,
    then topics before glossary terms."""
    if not words:
        return [], 0
    hits = []
    for entry in entries:
        if not all(word_matches(word, fold(entry['text'])) for word in words):
            continue
        hits.append(((-_title_hits(words, entry['title']), entry['kind'] != 'topic', entry['title'].lower()),
                     {key: entry[key] for key in ('kind', 'anchor', 'title', 'summary', 'parent')}))
    hits.sort(key=lambda item: item[0])
    return [row for _, row in hits[:limit]], len(hits)


def search(query: str, databases: Sequence[Mapping[str, Any]], *,
           gene_index: Optional[GeneIndex] = None,
           genesets: Optional[Sequence[Mapping[str, Any]]] = None,
           genesets_with_gene: Optional[Callable[[str], Sequence[Mapping[str, Any]]]] = None,
           papers: Optional[Callable[[str], Sequence[Mapping[str, Any]]]] = None,
           analysis_collections: Optional[Sequence[Mapping[str, Any]]] = None,
           artefacts: Optional[Sequence[Mapping[str, Any]]] = None,
           facets: Optional[Sequence[Mapping[str, Any]]] = None,
           help_hidden: Optional[Sequence[str]] = None,
           labels: Optional[Mapping[str, str]] = None,
           limit: int = 50) -> Dict[str, Any]:
    """Unified keyword search, grouped by kind.

    Args:
        query: what the visitor typed.
        databases: the dataset records the caller may see (``GET /databases``).
        gene_index: gene names of the catalogue; None skips the gene group.
        genesets: gene set summaries; None skips the gene set group.
        genesets_with_gene: gene sets that contain a gene (for a query of gene words only).
        papers: callable taking the joined words and returning paper summaries; None skips papers.
        analysis_collections: visible analysis collections (no datasets) to search besides the collections
            of the visible datasets.
        artefacts: artefacts the caller may see (``ArtefactService.searchable``); None skips the group.
        facets: facets to search (``{column, tag, label, summary, definition, n_datasets}``); None skips.
        help_hidden: parts switched off on the site, for the help group; None skips the help group.
        labels: ``{tag: label}`` of the ant_* annotation columns.
        limit: most rows per group (totals are reported separately).

    Returns:
        ``{query, words, gene_words, datasets, n_datasets, datasets_basis, collections, n_collections, genes,
        suggestions, genesets, n_genesets, papers, n_papers, artefacts, n_artefacts, facets, n_facets, help,
        n_help}``. A group that was skipped is None; an empty match is an empty list.
    """
    words = query_words(query)
    labels = labels if labels is not None else vocabulary_labels()
    visible_ids = {str(db.get('id')) for db in databases}
    gene_words: List[str] = []
    genes: Optional[List[Dict[str, Any]]] = None
    suggestions: List[str] = []
    if gene_index is not None:
        genes = []
        for word in words:
            name = gene_index.canonical(word) if _gene_like(word) else None
            if name:
                gene_words.append(word)
                genes.append({'gene': name, 'n_datasets': len(gene_index.datasets(word) & visible_ids)})
        if not genes and len(words) == 1 and _gene_like(words[0]) and len(words[0]) >= 3:
            suggestions = gene_index.prefixed(words[0])
    other_words = [word for word in words if word not in gene_words]

    if other_words:
        all_rows, n_datasets = search_datasets(databases, other_words, labels, limit=10_000)
        basis = 'words'
    elif gene_words:
        # Gene words only: the datasets that mention one of the genes in their description or annotations.
        found: Dict[str, Dict[str, Any]] = {}
        for word in gene_words:
            rows, _ = search_datasets(databases, [word], labels, limit=10_000, scoped=False)
            for row in rows:
                found.setdefault(row['db_id'], row)
        all_rows, n_datasets = list(found.values()), len(found)
        basis = 'mentions'
    else:
        all_rows, n_datasets, basis = [], 0, 'none'
    dataset_rows = all_rows[:limit]
    text_words = other_words or gene_words
    collection_rows, n_collections = search_collections(
        databases, text_words, {row['db_id'] for row in all_rows}, analysis_collections, limit)

    geneset_rows: Optional[List[Dict[str, Any]]] = None
    n_genesets = 0
    if genesets is not None:
        if other_words:
            geneset_rows, n_genesets = search_genesets(genesets, other_words, limit=limit)
        elif gene_words and genesets_with_gene is not None:
            seen: Dict[str, Dict[str, Any]] = {}
            for word in gene_words:
                for summary in genesets_with_gene(gene_index.canonical(word) or word):
                    row = _geneset_row(summary)
                    seen.setdefault(f"{row['study']}/{row['experiment']}/{row['name']}", row)
            geneset_rows, n_genesets = list(seen.values())[:limit], len(seen)
        else:
            geneset_rows = []

    paper_rows: Optional[List[Dict[str, Any]]] = None
    if papers is not None:
        paper_rows = list(papers(paper_query(words))) if words else []
    artefact_rows, n_artefacts = (search_artefacts(artefacts, words, limit) if artefacts is not None
                                  else (None, 0))
    facet_rows, n_facets = search_facets(facets, words, limit) if facets is not None else (None, 0)
    help_rows, n_help = (search_help(help_entries(help_hidden), words, limit) if help_hidden is not None
                         else (None, 0))
    return {
        'query': query,
        'words': words,
        'gene_words': gene_words,
        'datasets': dataset_rows,
        'n_datasets': n_datasets,
        'datasets_basis': basis,
        'collections': collection_rows,
        'n_collections': n_collections,
        'genes': genes,
        'suggestions': suggestions,
        'genesets': geneset_rows,
        'n_genesets': n_genesets,
        'papers': paper_rows[:limit] if paper_rows is not None else None,
        'n_papers': len(paper_rows) if paper_rows is not None else 0,
        'artefacts': artefact_rows,
        'n_artefacts': n_artefacts,
        'facets': facet_rows,
        'n_facets': n_facets,
        'help': help_rows,
        'n_help': n_help,
    }


# ── browse by disease / cell type ─────────────────────────────────────────────

def browse_counts(databases: Sequence[Mapping[str, Any]], kind: str) -> Dict[str, Any]:
    """Dataset counts per disease or cell type value.

    Args:
        databases: the dataset records the caller may see.
        kind: 'disease' or 'celltype'.

    Returns:
        ``{entries: [{key, label, recorded_as, n_datasets, n_collections, n_cells}], n_datasets, n_with_value,
        n_without_value}``. ``key`` is the folded label (what the filtered list takes); entries are sorted by
        dataset count, then label; control donors come last for diseases.
    """
    if kind not in ('disease', 'celltype'):
        raise ValueError(f'unknown browse kind: {kind}')
    values_of = disease_values if kind == 'disease' else celltype_values
    label_of = disease_label if kind == 'disease' else celltype_label
    entries: Dict[str, Dict[str, Any]] = {}
    with_value = 0
    for db in databases:
        values = values_of(db)
        if values:
            with_value += 1
        seen_keys: Set[str] = set()
        for value in values:
            label = label_of(value)
            key = fold(label)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            entry = entries.setdefault(key, {'key': key, 'label': label, 'recorded_as': [], 'db_ids': [],
                                             'groups': set(), 'n_cells': 0})
            if value not in entry['recorded_as']:
                entry['recorded_as'].append(value)
            entry['db_ids'].append(str(db.get('id')))
            entry['groups'].add(str(db.get('group_id') or str(db.get('id')).split('/')[0]))
            entry['n_cells'] += int(db.get('n_cells') or 0)
    rows = []
    for entry in entries.values():
        rows.append({'key': entry['key'], 'label': entry['label'], 'recorded_as': entry['recorded_as'],
                     'n_datasets': len(entry['db_ids']), 'n_collections': len(entry['groups']),
                     'n_cells': entry['n_cells']})
    rows.sort(key=lambda row: (row['key'] in ('control donors',), -row['n_datasets'], row['label'].lower()))
    return {'entries': rows, 'n_datasets': len(databases), 'n_with_value': with_value,
            'n_without_value': len(databases) - with_value}


def browse_datasets(databases: Sequence[Mapping[str, Any]], kind: str, key: str) -> List[Dict[str, Any]]:
    """The datasets carrying the disease or cell type ``key`` (a folded label from :func:`browse_counts`)."""
    values_of = disease_values if kind == 'disease' else celltype_values
    label_of = disease_label if kind == 'disease' else celltype_label
    wanted = fold(key)
    rows = []
    for db in databases:
        if any(fold(label_of(value)) == wanted for value in values_of(db)):
            rows.append(dataset_row(db))
    rows.sort(key=lambda row: (row['collection'].lower(), row['title'].lower()))
    return rows


def catalogue_summary(databases: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Counts for the front page: datasets, collections, cells, human share, microglia share, organisms."""
    groups = {str(db.get('group_id') or str(db.get('id')).split('/')[0]) for db in databases}
    n_human = sum(1 for db in databases if 'human' in [v.lower() for v in as_list(db.get('organism'))])
    n_microglia = sum(1 for db in databases if 'microglia' in [celltype_label(v) for v in celltype_values(db)])
    organisms: Dict[str, int] = defaultdict(int)
    for db in databases:
        for value in as_list(db.get('organism')):
            organisms[value.lower()] += 1
    primary = [db for db in databases if (db.get('designation') or 'primary') == 'primary']
    return {
        'n_datasets': len(databases),
        'n_collections': len(groups),
        'n_cells': sum(int(db.get('n_cells') or 0) for db in databases),
        # reanalyses and QC companions reuse cells another dataset serves: not counted twice
        'n_cells_primary': sum(int(db.get('n_cells') or 0) for db in primary),
        'n_primary': len(primary),
        'n_single_nucleus': sum(1 for db in databases
                                if 'nucleus' in [v.lower() for v in as_list(db.get('suspension_type'))]),
        'n_single_cell': sum(1 for db in databases
                             if 'cell' in [v.lower() for v in as_list(db.get('suspension_type'))]),
        'n_human': n_human,
        'n_microglia': n_microglia,
        'organisms': dict(sorted(organisms.items(), key=lambda item: -item[1])),
    }
