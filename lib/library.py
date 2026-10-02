"""Paper library: BibLaTeX-compatible markdown sidecars.

Every paper is one ``<citekey>.md`` file in the library repo (optionally with
``<citekey>.pdf`` next to it). The YAML front matter uses BibLaTeX field
names plus ``anthive_*`` extensions; the markdown body holds free notes.
Datasets, collections and gene sets point at papers with
``papers: {<citekey>: <role>}`` in their own front matter. See
``doc/LIBRARY.md`` for the full specification.

This module parses, validates and writes sidecars, generates citekeys,
exports BibTeX, finds references to papers in an h5adstore, and fetches
bibliographic metadata from CrossRef and OpenAlex.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

# ── field model ─────────────────────────────────────────────────────────

ENTRY_TYPES = {
    'article', 'online', 'preprint', 'book', 'inbook', 'incollection',
    'inproceedings', 'thesis', 'phdthesis', 'mastersthesis', 'report',
    'techreport', 'misc', 'unpublished', 'dataset', 'software',
}

# BibLaTeX fields we recognise, in the order they are written.
BIBLATEX_FIELDS = [
    'entrytype', 'citekey', 'author', 'editor', 'title', 'subtitle',
    'journaltitle', 'journal', 'booktitle', 'publisher', 'institution',
    'date', 'year', 'month', 'volume', 'number', 'issue', 'pages',
    'eid', 'doi', 'pmid', 'pmcid', 'isbn', 'issn', 'eprint', 'eprinttype',
    'eprintclass', 'url', 'urldate', 'language', 'abstract', 'keywords',
    'file', 'related', 'relatedtype', 'note',
]

ANTHIVE_FIELDS = [
    'anthive_role', 'anthive_celltypes', 'anthive_tissues',
    'anthive_diseases', 'anthive_organisms', 'anthive_methods',
    'anthive_cohorts', 'anthive_summary', 'anthive_orcids', 'anthive_cites',
    'anthive_openalex', 'anthive_access', 'anthive_llm', 'anthive_draft',
]

FIELD_ORDER = BIBLATEX_FIELDS + ANTHIVE_FIELDS
LIST_FIELDS = {
    'author', 'editor', 'keywords', 'related', 'anthive_celltypes',
    'anthive_tissues', 'anthive_diseases', 'anthive_organisms',
    'anthive_methods', 'anthive_cohorts', 'anthive_cites', 'anthive_draft',
}

ROLES = {'source', 'reanalysis', 'method', 'background', 'related'}
ACCESS = {'public', 'internal', 'restricted'}
LLM_POLICY = {'allowed', 'summary_only', 'never'}

# BibLaTeX relatedtype values plus the ones anthive uses.
RELATED_TYPES = {
    'samecohort', 'reanalysis', 'reanalysisof', 'extends', 'version',
    'comment', 'reply', 'origpubin', 'translationof', 'reprintof',
    'companion', 'samedataset', 'samelab', 'related',
}

# Ontology ids for the mus vocabulary labels used in anthive_celltypes /
# anthive_tissues / anthive_diseases. Labels without an entry stay labels.
ONTOLOGY_IDS = {
    # cell types (CL)
    'microglial cell': 'CL:0000129', 'macrophage': 'CL:0000235',
    'dendritic cell': 'CL:0000451', 'monocyte': 'CL:0000576',
    'astrocyte': 'CL:0000127', 'oligodendrocyte': 'CL:0000128',
    'oligodendrocyte precursor cell': 'CL:0002453', 'neuron': 'CL:0000540',
    'excitatory neuron': 'CL:0008030', 'inhibitory neuron': 'CL:0008029',
    'endothelial cell': 'CL:0000115', 'pericyte': 'CL:0000669',
    'fibroblast': 'CL:0000057', 'ependymal cell': 'CL:0000065',
    'myeloid cell': 'CL:0000763', 'glial cell': 'CL:0000125',
    'vascular leptomeningeal cell': 'CL:4023051',
    # tissues (UBERON)
    'brain': 'UBERON:0000955', 'brainstem': 'UBERON:0002298',
    'cortex': 'UBERON:0000956', 'frontal cortex': 'UBERON:0001870',
    'prefrontal cortex': 'UBERON:0000451',
    'dorsolateral prefrontal cortex': 'UBERON:0009834',
    'motor cortex': 'UBERON:0001384', 'occipital cortex': 'UBERON:0002021',
    'temporal cortex': 'UBERON:0016538', 'parietal cortex': 'UBERON:0016530',
    'middle temporal gyrus': 'UBERON:0002771',
    'inferior temporal gyrus': 'UBERON:0002751',
    'entorhinal cortex': 'UBERON:0002728', 'angular gyrus': 'UBERON:0002686',
    'hippocampus': 'UBERON:0002421', 'striatum': 'UBERON:0002435',
    'caudate nucleus': 'UBERON:0001873', 'thalamus': 'UBERON:0001897',
    'hypothalamus': 'UBERON:0001898', 'cerebellum': 'UBERON:0002037',
    'midbrain': 'UBERON:0001891', 'substantia nigra': 'UBERON:0002038',
    'olfactory bulb': 'UBERON:0002264', 'corpus callosum': 'UBERON:0002336',
    'white matter': 'UBERON:0002316', 'gray matter': 'UBERON:0002020',
    'subventricular zone': 'UBERON:0004922', 'spinal cord': 'UBERON:0002240',
    'blood': 'UBERON:0000178', 'bone marrow': 'UBERON:0002371',
    'retina': 'UBERON:0000966', 'csf': 'UBERON:0001359',
    # diseases (MONDO; normal is PATO)
    'normal': 'PATO:0000461', 'Alzheimer disease': 'MONDO:0004975',
    'Parkinson disease': 'MONDO:0005180',
    'amyotrophic lateral sclerosis': 'MONDO:0004976',
    'multiple sclerosis': 'MONDO:0005301', 'Huntington disease': 'MONDO:0007739',
    'frontotemporal dementia': 'MONDO:0017276', 'Down syndrome': 'MONDO:0008608',
    'autism spectrum disorder': 'MONDO:0005258', 'schizophrenia': 'MONDO:0005090',
    'glioblastoma': 'MONDO:0018177', 'COVID-19': 'MONDO:0100096',
    'epilepsy': 'MONDO:0005027', 'temporal lobe epilepsy': 'MONDO:0005115',
    'dementia with Lewy bodies': 'MONDO:0007488',
    'vascular dementia': 'MONDO:0004648', 'bipolar disorder': 'MONDO:0004985',
}

CITEKEY_RE = re.compile(r'^[a-z0-9][a-z0-9_.:-]*$')
DOI_RE = re.compile(r'\b(10\.\d{4,9}/[^\s"<>]+)', re.I)
FRONT_MATTER_RE = re.compile(r'^---\n(.*?)\n---\n?(.*)$', re.S)

# Words skipped when picking the title word of a Better BibTeX citekey.
_TITLE_STOPWORDS = {
    'a', 'an', 'the', 'of', 'in', 'on', 'at', 'to', 'for', 'and', 'or', 'by',
    'with', 'from', 'into', 'over', 'under', 'between', 'during', 'is', 'are',
    'as', 'its', 'via', 'using', 'toward', 'towards', 'single',
}


@dataclass
class Paper:
    """One library entry: front matter fields plus the free-text body."""
    citekey: str
    fields: Dict[str, Any]
    body: str = ''
    path: Optional[Path] = None

    @property
    def doi(self) -> Optional[str]:
        value = self.fields.get('doi')
        return normalize_doi(value) if value else None


@dataclass
class Reference:
    """A ``papers:`` entry in an h5adstore (or gene-set) metadata file."""
    path: Path
    citekey: str
    role: str
    scope: str  # 'collection' (index.md) or 'dataset'


@dataclass
class LibraryIssues:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ── parsing and writing ─────────────────────────────────────────────────

def parse_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        raise ValueError('no YAML front matter (file must start with ---)')
    data = yaml.safe_load(match.group(1)) or {}
    if not isinstance(data, dict):
        raise ValueError('front matter is not a mapping')
    return data, match.group(2)


def parse_sidecar(path: Path) -> Paper:
    data, body = parse_front_matter(Path(path).read_text(encoding='utf-8'))
    citekey = str(data.get('citekey') or Path(path).stem)
    return Paper(citekey=citekey, fields=data, body=body.lstrip('\n'), path=Path(path))


def ordered_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in FIELD_ORDER:
        if key in fields and fields[key] not in (None, '', [], {}):
            out[key] = fields[key]
    for key in sorted(fields):
        if key not in out and fields[key] not in (None, '', [], {}):
            out[key] = fields[key]
    return out


class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, value: str):
    style = '>' if len(value) > 100 and '\n' not in value else ('|' if '\n' in value else None)
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style=style)


_Dumper.add_representer(str, _str_representer)


def render_sidecar(fields: Dict[str, Any], body: str = '') -> str:
    front = yaml.dump(ordered_fields(fields), Dumper=_Dumper, sort_keys=False,
                      allow_unicode=True, width=78, default_flow_style=False)
    text = f"---\n{front}---\n"
    if body.strip():
        text += "\n" + body.strip() + "\n"
    return text


def write_sidecar(path: Path, fields: Dict[str, Any], body: str = '') -> None:
    Path(path).write_text(render_sidecar(fields, body), encoding='utf-8')


# ── citekeys and DOIs ───────────────────────────────────────────────────

def normalize_doi(value: str) -> str:
    value = str(value).strip()
    value = re.sub(r'^(https?://(dx\.)?doi\.org/|doi:\s*)', '', value, flags=re.I)
    return value.rstrip('.,;)').lower()


def _ascii_word(text: str) -> str:
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', text.lower())


def make_citekey(first_author_last: str, year: Any, title: str,
                 existing: Iterable[str] = ()) -> str:
    """Better BibTeX style: ``<lastname><year><firsttitleword>``, lowercase
    ascii; a, b, c ... appended on collision with ``existing``."""
    last = _ascii_word(first_author_last) or 'anon'
    year_text = str(year or '')[:4] if year else 'nd'
    word = ''
    for token in re.findall(r"[A-Za-z0-9À-ɏ'-]+", title or ''):
        candidate = _ascii_word(token)
        if candidate and candidate not in _TITLE_STOPWORDS:
            word = candidate
            break
    base = f"{last}{year_text}{word}"
    taken = set(existing)
    if base not in taken:
        return base
    for suffix in 'abcdefghijklmnopqrstuvwxyz':
        if base + suffix not in taken:
            return base + suffix
    raise ValueError(f'too many citekey collisions for {base}')


def first_author_last(fields: Dict[str, Any]) -> str:
    authors = fields.get('author') or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(' and ')]
    if not authors:
        return ''
    first = str(authors[0])
    return first.split(',')[0].strip() if ',' in first else first.split()[-1]


# ── validation ──────────────────────────────────────────────────────────

def load_vocabulary(vocab_path: Optional[Path] = None) -> Dict[str, set]:
    path = vocab_path or Path(__file__).with_name('_mus_vocab.yaml')
    try:
        vocab = (yaml.safe_load(path.read_text()) or {}).get('vocabulary', {})
    except OSError:
        return {}
    return {key: {str(v) for v in (values or [])} for key, values in vocab.items()
            if isinstance(values, list)}


def validate_paper(paper: Paper, vocab: Optional[Dict[str, set]] = None) -> LibraryIssues:
    issues = LibraryIssues()
    f = paper.fields
    where = paper.path.name if paper.path else paper.citekey
    if not CITEKEY_RE.match(paper.citekey):
        issues.errors.append(f"{where}: citekey {paper.citekey!r} must be lowercase ascii")
    if paper.path and paper.path.stem != paper.citekey:
        issues.errors.append(f"{where}: file name must be <citekey>.md ({paper.citekey}.md)")
    entrytype = f.get('entrytype')
    if entrytype not in ENTRY_TYPES:
        issues.errors.append(f"{where}: entrytype {entrytype!r} is not a known BibLaTeX type")
    if not f.get('title'):
        issues.errors.append(f"{where}: title is required")
    if not (f.get('date') or f.get('year')):
        issues.errors.append(f"{where}: date or year is required")
    if not f.get('author') and not f.get('editor'):
        issues.warnings.append(f"{where}: no author or editor")
    for key in LIST_FIELDS:
        if key in f and f[key] is not None and not isinstance(f[key], list):
            issues.errors.append(f"{where}: {key} must be a list")
    for author in f.get('author') or []:
        if isinstance(author, str) and ',' not in author and ' ' in author.strip() \
                and not author.strip().startswith('{'):
            issues.warnings.append(f"{where}: author {author!r} is not in 'Last, First' form")
    if f.get('doi') and not DOI_RE.match(str(f['doi'])):
        issues.errors.append(f"{where}: doi {f['doi']!r} does not look like a DOI")
    role = f.get('anthive_role')
    if role is not None and role not in ROLES:
        issues.errors.append(f"{where}: anthive_role {role!r} not in {sorted(ROLES)}")
    if f.get('anthive_access') not in (None, *ACCESS):
        issues.errors.append(f"{where}: anthive_access must be one of {sorted(ACCESS)}")
    if f.get('anthive_llm') not in (None, *LLM_POLICY):
        issues.errors.append(f"{where}: anthive_llm must be one of {sorted(LLM_POLICY)}")
    if f.get('relatedtype') and f['relatedtype'] not in RELATED_TYPES:
        issues.warnings.append(f"{where}: relatedtype {f['relatedtype']!r} is not a known type")
    if f.get('related') and not f.get('relatedtype'):
        issues.warnings.append(f"{where}: related without relatedtype")
    for key in (f.get('anthive_draft') if isinstance(f.get('anthive_draft'), list) else []):
        if key not in FIELD_ORDER:
            issues.warnings.append(f"{where}: anthive_draft names unknown field {key!r}")
    if vocab:
        for key, vocab_key in (('anthive_celltypes', 'cell_type'), ('anthive_tissues', 'tissue'),
                               ('anthive_diseases', 'disease'), ('anthive_organisms', 'organism')):
            allowed = vocab.get(vocab_key)
            values = f.get(key) if isinstance(f.get(key), list) else []
            for label in values:
                if allowed and label not in allowed:
                    issues.warnings.append(f"{where}: {key} label {label!r} is not in the mus vocabulary")
    for key in f:
        if key not in FIELD_ORDER and not key.startswith('anthive_'):
            issues.warnings.append(f"{where}: unknown field {key!r} (exported to BibTeX as is)")
    return issues


# ── library loading ─────────────────────────────────────────────────────

def library_papers_dir(root: Path) -> Path:
    """Papers live in ``<library>/papers``; a flat library root also works."""
    root = Path(root)
    return root / 'papers' if (root / 'papers').is_dir() else root


def load_library(root: Path) -> Tuple[Dict[str, Paper], LibraryIssues]:
    issues = LibraryIssues()
    papers: Dict[str, Paper] = {}
    dois: Dict[str, str] = {}
    for path in sorted(library_papers_dir(root).glob('*.md')):
        if path.name.upper() in ('README.MD', 'REVIEW.MD', 'INDEX.MD'):
            continue
        try:
            paper = parse_sidecar(path)
        except (ValueError, yaml.YAMLError) as exc:
            issues.errors.append(f"{path.name}: {exc}")
            continue
        if paper.citekey in papers:
            issues.errors.append(f"{path.name}: duplicate citekey {paper.citekey}")
            continue
        if paper.doi:
            if paper.doi in dois:
                issues.errors.append(f"{path.name}: DOI {paper.doi} already used by {dois[paper.doi]}")
            dois[paper.doi] = paper.citekey
        papers[paper.citekey] = paper
    for paper in papers.values():
        for target in paper.fields.get('related') or []:
            if target not in papers:
                issues.errors.append(f"{paper.citekey}: related citekey {target!r} is not in the library")
        pdf = paper.fields.get('file')
        if pdf and paper.path and not (paper.path.parent / pdf).exists():
            issues.warnings.append(f"{paper.citekey}: file {pdf!r} not found next to the sidecar")
    return papers, issues


def find_references(store: Path) -> List[Reference]:
    """Every ``papers:`` entry in dataset .md and collection index.md files."""
    refs: List[Reference] = []
    store = Path(store)
    for path in sorted(store.rglob('*.md')):
        rel_parts = path.relative_to(store).parts
        if any(part.startswith(('_', '.')) for part in rel_parts) or 'static' in rel_parts:
            continue
        try:
            data, _ = parse_front_matter(path.read_text(encoding='utf-8'))
        except (ValueError, yaml.YAMLError, UnicodeDecodeError):
            continue
        papers = data.get('papers')
        if not papers:
            continue
        scope = 'collection' if path.name == 'index.md' else 'dataset'
        if isinstance(papers, dict):
            items = papers.items()
        elif isinstance(papers, list):
            items = [(key, 'related') for key in papers]
        else:
            continue
        for citekey, role in items:
            refs.append(Reference(path=path, citekey=str(citekey), role=str(role), scope=scope))
    return refs


def unresolved_references(papers: Dict[str, Paper], refs: List[Reference]) -> List[str]:
    problems = []
    for ref in refs:
        if ref.citekey not in papers:
            problems.append(f"{ref.path}: papers -> {ref.citekey!r} is not in the library")
        if ref.role not in ROLES:
            problems.append(f"{ref.path}: papers -> {ref.citekey}: role {ref.role!r} not in {sorted(ROLES)}")
    return problems


# ── BibTeX export ───────────────────────────────────────────────────────

_VERBATIM_FIELDS = {'doi', 'url', 'file', 'eprint', 'pmid', 'pmcid', 'anthive_openalex'}
_BIBTEX_SPECIALS = {'\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$',
                    '#': r'\#', '_': r'\_', '~': r'\textasciitilde{}',
                    '^': r'\textasciicircum{}'}


def _escape_bibtex(value: str) -> str:
    out = ''.join(_BIBTEX_SPECIALS.get(char, char) for char in value)
    # unbalanced braces would break the entry; drop them
    depth = 0
    balanced = []
    for char in out:
        if char == '{':
            depth += 1
        elif char == '}':
            if depth == 0:
                continue
            depth -= 1
        balanced.append(char)
    return ''.join(balanced) + '}' * depth


def _bib_value(key: str, value: Any) -> str:
    if isinstance(value, dict):
        value = [f"{k}: {v}" for k, v in value.items()]
    if isinstance(value, list):
        if key in ('author', 'editor'):
            text = ' and '.join(str(v) for v in value)
        elif key in ('keywords', 'related', 'anthive_cites'):
            text = ', '.join(str(v) for v in value)
        else:
            text = '; '.join(str(v) for v in value)
    else:
        text = str(value)
    text = ' '.join(text.split())
    if key in _VERBATIM_FIELDS:
        return text.replace('{', '').replace('}', '')
    return _escape_bibtex(text)


_CLASSIC_TYPES = {'online': 'misc', 'preprint': 'misc', 'dataset': 'misc', 'software': 'misc',
                  'report': 'techreport', 'thesis': 'phdthesis'}
_MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']


def _classic_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """BibLaTeX fields mapped to classic BibTeX (natbib, plain bibtex)."""
    out = dict(fields)
    if 'journaltitle' in out:
        out['journal'] = out.pop('journaltitle')
    date = out.pop('date', None)
    if date is not None:
        text = str(date)
        out.setdefault('year', text[:4])
        if len(text) >= 7 and text[5:7].isdigit():
            out.setdefault('month', _MONTHS[int(text[5:7]) - 1])
    for key in ('related', 'relatedtype', 'eprinttype', 'urldate'):
        out.pop(key, None)
    return out


def to_bibtex(paper: Paper, include_anthive: bool = True, classic: bool = False) -> str:
    """One BibTeX entry. ``classic`` maps BibLaTeX types and fields to plain
    BibTeX (online -> misc, journaltitle -> journal, date -> year/month)."""
    fields = _classic_fields(paper.fields) if classic else paper.fields
    entrytype = fields.get('entrytype') or 'misc'
    if classic:
        entrytype = _CLASSIC_TYPES.get(entrytype, entrytype)
    lines = [f"@{entrytype}{{{paper.citekey},"]
    for key, value in ordered_fields(fields).items():
        if key in ('entrytype', 'citekey') or value in (None, '', []):
            continue
        if key.startswith('anthive_') and not include_anthive:
            continue
        if key == 'date' and isinstance(value, int):
            key = 'year'
        lines.append(f"  {key} = {{{_bib_value(key, value)}}},")
    lines.append('}')
    return '\n'.join(lines)


def export_bibtex(papers: Iterable[Paper], include_anthive: bool = True, classic: bool = False) -> str:
    return '\n\n'.join(to_bibtex(p, include_anthive, classic) for p in
                       sorted(papers, key=lambda p: p.citekey)) + '\n'


def _unescape_bibtex(text: str) -> str:
    for plain, escaped in _BIBTEX_SPECIALS.items():
        text = text.replace(escaped, plain)
    return text


_COMMA_LIST_FIELDS = {'keywords', 'related', 'anthive_cites'}


def fields_from_bibtex_entry(entry: Dict[str, str]) -> Dict[str, Any]:
    """Sidecar fields from one parsed BibTeX entry (bibtexparser dict).

    Inverse of to_bibtex: author/editor split on ' and ', keywords/related/
    anthive_cites on commas, other anthive list fields on semicolons,
    anthive_orcids back to a mapping; classic journal/year become
    journaltitle/date."""
    fields: Dict[str, Any] = {'entrytype': str(entry.get('ENTRYTYPE', 'misc')).lower()}
    for key, value in entry.items():
        if key in ('ENTRYTYPE', 'ID'):
            continue
        key = key.lower()
        value = ' '.join(str(value).split())
        if key not in _VERBATIM_FIELDS:
            value = _unescape_bibtex(value)
        if key in ('author', 'editor'):
            fields[key] = [a.strip() for a in re.split(r'\s+and\s+', value) if a.strip()]
        elif key in _COMMA_LIST_FIELDS:
            fields[key] = [v.strip() for v in value.split(',') if v.strip()]
        elif key == 'anthive_orcids':
            pairs = [item.rsplit(':', 1) for item in value.split(';') if ':' in item]
            fields[key] = {name.strip(): orcid.strip() for name, orcid in pairs}
        elif key in LIST_FIELDS:
            fields[key] = [v.strip() for v in value.split(';') if v.strip()]
        elif key == 'doi':
            fields[key] = normalize_doi(value)
        else:
            fields[key] = value
    if 'journal' in fields and 'journaltitle' not in fields:
        fields['journaltitle'] = fields.pop('journal')
    if 'year' in fields and 'date' not in fields:
        year = fields.pop('year')
        month = fields.pop('month', None)
        if month and str(month)[:3].lower() in _MONTHS:
            fields['date'] = f"{year}-{_MONTHS.index(str(month)[:3].lower()) + 1:02d}"
        else:
            fields['date'] = int(year) if str(year).isdigit() else year
    if fields['entrytype'] == 'misc' and str(fields.get('note', '')).lower().startswith('preprint'):
        fields['entrytype'] = 'online'
    return fields


def ontology_terms(labels: Iterable[str]) -> List[Dict[str, Optional[str]]]:
    return [{'label': label, 'id': ONTOLOGY_IDS.get(label)} for label in labels]


# ── metadata fetching (CrossRef, OpenAlex) ──────────────────────────────

USER_AGENT = 'anthive-library/1.0 (mailto:mark.fiers@kuleuven.be)'
_last_call = [0.0]


def _get_json(url: str, timeout: int = 30, min_interval: float = 0.25) -> Optional[dict]:
    wait = min_interval - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                                   'Accept': 'application/json'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                _last_call[0] = time.time()
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            _last_call[0] = time.time()
            if exc.code == 404:
                return None
            if exc.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return None


def _strip_jats(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text or '')
    return ' '.join(text.split())


def fetch_crossref(doi: str) -> Optional[dict]:
    data = _get_json(f"https://api.crossref.org/works/{urllib.parse.quote(normalize_doi(doi))}")
    return (data or {}).get('message')


def fetch_openalex(doi: str) -> Optional[dict]:
    return _get_json("https://api.openalex.org/works/doi:"
                     f"{urllib.parse.quote(normalize_doi(doi))}?mailto=mark.fiers@kuleuven.be")


def openalex_dois(work_ids: List[str], limit: int = 200) -> List[str]:
    """DOIs for OpenAlex work ids (W...), 50 per request."""
    dois: List[str] = []
    ids = [w.rsplit('/', 1)[-1] for w in work_ids][:limit]
    for start in range(0, len(ids), 50):
        chunk = '|'.join(ids[start:start + 50])
        data = _get_json("https://api.openalex.org/works?filter=openalex_id:"
                         f"{chunk}&select=doi&per-page=50&mailto=mark.fiers@kuleuven.be")
        for work in (data or {}).get('results', []):
            if work.get('doi'):
                dois.append(normalize_doi(work['doi']))
    return sorted(set(dois))


def doi_from_pmid(pmid: str) -> Optional[str]:
    data = _get_json("https://api.openalex.org/works/pmid:"
                     f"{urllib.parse.quote(str(pmid))}?mailto=mark.fiers@kuleuven.be")
    return normalize_doi(data['doi']) if data and data.get('doi') else None


def _abstract_from_inverted(index: Optional[dict]) -> str:
    if not index:
        return ''
    positions = sorted((pos, word) for word, poss in index.items() for pos in poss)
    return ' '.join(word for _, word in positions)


_CROSSREF_TYPES = {'journal-article': 'article', 'posted-content': 'online',
                   'book': 'book', 'book-chapter': 'incollection',
                   'proceedings-article': 'inproceedings', 'dissertation': 'thesis',
                   'report': 'report', 'dataset': 'dataset'}


def metadata_from_doi(doi: str, with_references: bool = True) -> Dict[str, Any]:
    """BibLaTeX fields for a DOI from CrossRef, completed with OpenAlex
    (ORCID-linked authors, abstract, PMID, referenced DOIs)."""
    doi = normalize_doi(doi)
    cr = fetch_crossref(doi) or {}
    oa = fetch_openalex(doi) or {}
    if not cr and not oa:
        raise LookupError(f"DOI {doi} not found in CrossRef or OpenAlex")
    fields: Dict[str, Any] = {'doi': doi}
    ctype = cr.get('type')
    fields['entrytype'] = _CROSSREF_TYPES.get(ctype, 'article' if oa.get('type') == 'article' else 'misc')
    if ctype == 'posted-content' or oa.get('type') == 'preprint':
        fields['entrytype'] = 'online'
    title = _strip_jats(' '.join(cr.get('title') or [])) or oa.get('title') or oa.get('display_name')
    if title:
        fields['title'] = title
    authors = []
    for person in cr.get('author') or []:
        if person.get('family'):
            authors.append(f"{person['family']}, {person.get('given', '')}".strip().rstrip(','))
        elif person.get('name'):
            authors.append(person['name'])
    if not authors:
        for authorship in oa.get('authorships') or []:
            name = (authorship.get('author') or {}).get('display_name')
            if name:
                parts = name.rsplit(' ', 1)
                authors.append(f"{parts[-1]}, {parts[0]}" if len(parts) == 2 else name)
    if authors:
        fields['author'] = authors
    container = (cr.get('container-title') or [None])[0] or \
        ((oa.get('primary_location') or {}).get('source') or {}).get('display_name')
    if container:
        if fields['entrytype'] == 'article':
            fields['journaltitle'] = container
        elif fields['entrytype'] == 'online':
            fields['note'] = f"Preprint, {container}"
        else:
            fields['booktitle'] = container
    date_parts = ((cr.get('published') or cr.get('published-print') or cr.get('published-online')
                   or cr.get('issued') or {}).get('date-parts') or [[None]])[0]
    if date_parts and date_parts[0]:
        if len(date_parts) >= 3:
            fields['date'] = f"{date_parts[0]:04d}-{date_parts[1]:02d}-{date_parts[2]:02d}"
        elif len(date_parts) == 2:
            fields['date'] = f"{date_parts[0]:04d}-{date_parts[1]:02d}"
        else:
            fields['date'] = int(date_parts[0])
    elif oa.get('publication_year'):
        fields['date'] = int(oa['publication_year'])
    for src, dst in (('volume', 'volume'), ('issue', 'number'), ('page', 'pages'),
                     ('publisher', 'publisher')):
        if cr.get(src):
            fields[dst] = str(cr[src])
    if cr.get('article-number') and 'pages' not in fields:
        fields['eid'] = str(cr['article-number'])
    if cr.get('ISSN'):
        fields['issn'] = cr['ISSN'][0]
    ids = oa.get('ids') or {}
    if ids.get('pmid'):
        fields['pmid'] = str(ids['pmid']).rsplit('/', 1)[-1]
    if ids.get('pmcid'):
        fields['pmcid'] = str(ids['pmcid']).rsplit('/', 1)[-1]
    if oa.get('id'):
        fields['anthive_openalex'] = str(oa['id']).rsplit('/', 1)[-1]
    abstract = _strip_jats(cr.get('abstract') or '') or _abstract_from_inverted(oa.get('abstract_inverted_index'))
    if abstract:
        fields['abstract'] = re.sub(r'^(abstract|summary)\s*', '', abstract, flags=re.I)
    fields['url'] = f"https://doi.org/{doi}"
    if with_references and oa.get('referenced_works'):
        fields['anthive_cites'] = openalex_dois(oa['referenced_works'])
    orcids = {}
    for authorship in oa.get('authorships') or []:
        author = authorship.get('author') or {}
        if author.get('orcid') and author.get('display_name'):
            orcids[' '.join(author['display_name'].split())] = author['orcid'].rsplit('/', 1)[-1]
    if orcids:
        fields['anthive_orcids'] = orcids
    return fields


def doi_from_pdf(pdf_path: Path) -> Optional[str]:
    """First DOI in the PDF metadata or the text of its first two pages."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # older pymupdf releases
        except ImportError:
            return None
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return None
    with doc:
        meta = ' '.join(str(v) for v in (doc.metadata or {}).values() if v)
        text = meta + ' ' + ' '.join(doc[i].get_text() for i in range(min(2, doc.page_count)))
    match = DOI_RE.search(text)
    return normalize_doi(match.group(1)) if match else None
