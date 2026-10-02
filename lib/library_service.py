"""Server-side view of the paper library and the literature index.

ant-serve builds one ``LibraryService`` at startup. It joins three sources:

- the library repository (``lib/library.py``): one BibLaTeX-compatible
  ``.md`` record per paper, plus its PDF;
- ``papers:`` references on the pointing side: collection ``index.md`` and
  dataset ``.md`` files in the h5adstore, study and experiment ``index.md``
  files in the genesetstore;
- the precomputed literature index (``lib/library_index.py``,
  ``library_chunks.parquet``) for passage search, page text and the
  per-dataset background pack.

Access rules, applied to every answer:

- ``anthive_access: restricted`` papers are visible to admins only;
  ``public`` and ``internal`` follow the dataset rule (anyone who can reach
  the API sees them).
- Callers that forward answers to an external LLM (ant-mcp) set
  ``for_llm``: papers with ``anthive_llm: never`` disappear entirely,
  ``summary_only`` papers keep their metadata and summary but no passages,
  notes, abstract or page text.

Nothing here talks to HTTP; ant-serve passes the caller's flags in.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from library import (LibraryIssues, Paper, export_bibtex, find_references,
                     load_library, normalize_doi, parse_front_matter, to_bibtex)

log = logging.getLogger(__name__)

# S3 key prefixes (under s3.prefix) for library artifacts. The leading "_"
# keeps them out of the h5adstore sync and the S3 catalog scan.
LIBRARY_S3_PREFIX = '_library/papers'
INDEX_S3_PREFIX = '_library_index'
INDEX_FILENAME = 'library_chunks.parquet'

TOPIC_FIELDS = {
    'cohort': 'anthive_cohorts',
    'celltype': 'anthive_celltypes',
    'tissue': 'anthive_tissues',
    'disease': 'anthive_diseases',
    'method': 'anthive_methods',
    'organism': 'anthive_organisms',
}
RELATION_KINDS = ('explicit', 'cohort', 'celltype', 'tissue', 'disease', 'method',
                  'author', 'cites', 'cited_by')
# Terms too generic to make two papers "related" on their own.
_GENERIC_TERMS = {'normal', 'human', 'mouse', 'brain', 'snrna-seq', 'scrna-seq'}
_LLM_FULL = 'allowed'
_LLM_SUMMARY = 'summary_only'
_LLM_NEVER = 'never'


def _as_list(value: Any) -> List[str]:
    if value in (None, ''):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v not in (None, '')]
    return [str(value)]


def _norm(text: str) -> str:
    text = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', text).strip().lower()


def _author_key(name: str, orcids: Dict[str, str]) -> str:
    orcid = orcids.get(name)
    if orcid:
        return 'orcid:' + str(orcid).rsplit('/', 1)[-1]
    last, _, first = str(name).partition(',')
    initial = _norm(first).strip()[:1]
    return f"name:{_norm(last)},{initial}"


def _year(fields: Dict[str, Any]) -> Optional[int]:
    for key in ('year', 'date'):
        match = re.match(r'(\d{4})', str(fields.get(key) or ''))
        if match:
            return int(match.group(1))
    return None


def _short_citation(fields: Dict[str, Any]) -> str:
    authors = _as_list(fields.get('author'))
    first = authors[0].split(',')[0] if authors else (fields.get('citekey') or '?')
    more = ' et al.' if len(authors) > 1 else ''
    year = _year(fields)
    return f"{first}{more} {year}" if year else f"{first}{more}"


def _scan_front_matter(path: Path) -> Dict[str, Any]:
    try:
        data, _ = parse_front_matter(path.read_text(encoding='utf-8'))
        return data
    except (OSError, ValueError, yaml.YAMLError, UnicodeDecodeError):
        return {}


def geneset_references(geneset_dir: Optional[Path]) -> List[Tuple[str, str, str]]:
    """``papers:`` entries in the genesetstore: (scope_id, citekey, role).
    scope_id is ``<study>`` for a study index.md and ``<study>/<experiment>``
    for an experiment index.md."""
    out: List[Tuple[str, str, str]] = []
    if not geneset_dir or not Path(geneset_dir).is_dir():
        return out
    root = Path(geneset_dir)
    for index in sorted(root.glob('*/index.md')) + sorted(root.glob('*/*/index.md')):
        rel = index.parent.relative_to(root)
        if any(part.startswith(('_', '.')) for part in rel.parts):
            continue
        papers = _scan_front_matter(index).get('papers')
        if isinstance(papers, dict):
            items = papers.items()
        elif isinstance(papers, list):
            items = [(key, 'related') for key in papers]
        else:
            continue
        for citekey, role in items:
            out.append(('/'.join(rel.parts), str(citekey), str(role)))
    return out


def highlight_snippet(text: str, query: str, width: int = 320) -> str:
    """A window of ``text`` around the first query term, terms in <mark>."""
    from library_index import _STOPWORDS  # noqa: PLC0415
    terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+", query)
             if len(t) > 2 and t.lower() not in _STOPWORDS]
    lowered = text.lower()
    first = min((lowered.find(t.lower()) for t in terms if lowered.find(t.lower()) >= 0), default=0)
    start = max(0, first - width // 3)
    snippet = text[start:start + width].strip()
    if start > 0:
        snippet = '...' + snippet
    if start + width < len(text):
        snippet = snippet + '...'
    snippet = snippet.replace('<', '&lt;').replace('>', '&gt;')
    for term in sorted(set(terms), key=len, reverse=True):
        snippet = re.sub(rf"(?i)\b({re.escape(term)})", r"<mark>\1</mark>", snippet)
    return snippet


@dataclass
class DatasetLink:
    db_id: str
    role: str
    scope: str           # 'collection' (index.md) or 'dataset' (<file>.md)
    source: str          # relative path of the referencing file


@dataclass
class LibraryState:
    papers: Dict[str, Paper] = field(default_factory=dict)
    issues: LibraryIssues = field(default_factory=LibraryIssues)
    dataset_links: Dict[str, List[DatasetLink]] = field(default_factory=dict)   # citekey -> links
    by_db: Dict[str, List[DatasetLink]] = field(default_factory=dict)            # db_id -> links (citekey in .source)
    papers_by_db: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)  # db_id -> [(citekey, role)]
    geneset_links: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)  # citekey -> [(scope_id, role)]
    unresolved: List[str] = field(default_factory=list)


class LibraryService:
    """Library records, references and the literature index, behind access rules."""

    def __init__(self, library_dir: Optional[Path], index_dir: Optional[Path],
                 store_dir: Optional[Path], geneset_dir: Optional[Path] = None,
                 embedder_factory: Optional[Callable[[str], Any]] = None) -> None:
        self.library_dir = Path(library_dir) if library_dir else None
        self.index_dir = Path(index_dir) if index_dir else None
        self.store_dir = Path(store_dir) if store_dir else None
        self.geneset_dir = Path(geneset_dir) if geneset_dir else None
        self.embedder_factory = embedder_factory
        self.state = LibraryState()
        self.index = None                  # LibraryIndex once loaded
        self.index_error: Optional[str] = None
        self.index_loading = False
        self._lock = threading.Lock()
        self._db_md: Dict[str, Path] = {}  # db_id -> dataset .md (absolute)

    # ── loading ───────────────────────────────────────────────────────────
    @property
    def index_path(self) -> Optional[Path]:
        return self.index_dir / INDEX_FILENAME if self.index_dir else None

    def set_datasets(self, db_md_paths: Dict[str, Path]) -> None:
        """db_id -> absolute path of the dataset's .md (next to its .duckdb)."""
        self._db_md = {db: Path(p) for db, p in db_md_paths.items()}

    def load(self) -> LibraryState:
        """(Re)load records and references. Cheap: a few hundred small files."""
        state = LibraryState()
        if self.library_dir and self.library_dir.is_dir():
            state.papers, state.issues = load_library(self.library_dir)
        md_to_db: Dict[Path, str] = {p.resolve(): db for db, p in self._db_md.items()}
        dirs_to_dbs: Dict[Path, List[str]] = defaultdict(list)
        for db, md in self._db_md.items():
            dirs_to_dbs[md.parent.resolve()].append(db)
        if self.store_dir and self.store_dir.is_dir():
            store = self.store_dir.resolve()
            for ref in find_references(self.store_dir):
                path = ref.path.resolve()
                if ref.scope == 'dataset':
                    dbs = [md_to_db[path]] if path in md_to_db else []
                else:
                    folder = path.parent
                    dbs = [db for d, members in dirs_to_dbs.items()
                           if d == folder or folder in d.parents for db in members]
                try:
                    source = str(path.relative_to(store))
                except ValueError:
                    source = str(path)
                if ref.citekey not in state.papers:
                    state.unresolved.append(f"{source}: {ref.citekey}")
                for db in dbs:
                    link = DatasetLink(db_id=db, role=ref.role, scope=ref.scope, source=source)
                    state.dataset_links.setdefault(ref.citekey, []).append(link)
                    state.by_db.setdefault(db, []).append(
                        DatasetLink(db_id=ref.citekey, role=ref.role, scope=ref.scope, source=source))
        # A dataset-level reference overrides the collection-level role for that dataset.
        for db, links in state.by_db.items():
            chosen: Dict[str, Tuple[str, int]] = {}
            for link in links:
                rank = 1 if link.scope == 'dataset' else 0
                if link.db_id not in chosen or rank >= chosen[link.db_id][1]:
                    chosen[link.db_id] = (link.role, rank)
            state.papers_by_db[db] = sorted(((ck, role) for ck, (role, _) in chosen.items()),
                                            key=lambda item: (item[1] != 'source', item[0]))
        for scope_id, citekey, role in geneset_references(self.geneset_dir):
            state.geneset_links.setdefault(citekey, []).append((scope_id, role))
            if citekey not in state.papers:
                state.unresolved.append(f"genesetstore {scope_id}: {citekey}")
        with self._lock:
            self.state = state
        return state

    def load_index(self) -> None:
        """Load library_chunks.parquet (seconds). Safe to call from a thread."""
        path = self.index_path
        self.index_loading = True
        try:
            if path is None or not path.is_file():
                self.index, self.index_error = None, f"no literature index at {path}"
                return
            from library_index import LibraryIndex, read_index_metadata  # noqa: PLC0415
            embedder = None
            model = read_index_metadata(path).get('embedding_model')
            if self.embedder_factory and model:
                try:
                    embedder = self.embedder_factory(model)
                except Exception as exc:  # noqa: BLE001 - missing model/runtime: keyword only
                    log.warning("literature index: no query embedder (%s); keyword search only", exc)
            index = LibraryIndex(path, embedder=embedder)
            if embedder is None:
                index.warnings.append('semantic search unavailable on this server: keyword search only')
            self.index, self.index_error = index, None
        except Exception as exc:  # noqa: BLE001
            self.index, self.index_error = None, f"literature index failed to load: {exc}"
            log.warning(self.index_error)
        finally:
            self.index_loading = False

    def status(self) -> Dict[str, Any]:
        state = self.state
        meta = self.index.meta if self.index is not None else {}
        return {
            'library_dir': str(self.library_dir) if self.library_dir else None,
            'papers': len(state.papers),
            'issues': {'errors': state.issues.errors, 'warnings': state.issues.warnings},
            'unresolved_references': state.unresolved,
            'linked_datasets': len(state.papers_by_db),
            'index': {
                'path': str(self.index_path) if self.index_path else None,
                'loaded': self.index is not None,
                'loading': self.index_loading,
                'error': self.index_error,
                'chunks': len(self.index.rows) if self.index is not None else 0,
                'embedding_model': meta.get('embedding_model'),
                'built_at': meta.get('built_at'),
                'semantic': bool(self.index is not None and self.index.semantic_available),
                'warnings': list(self.index.warnings) if self.index is not None else [],
            },
        }

    # ── access ────────────────────────────────────────────────────────────
    @staticmethod
    def llm_policy(paper: Paper) -> str:
        value = str(paper.fields.get('anthive_llm') or _LLM_FULL)
        return value if value in (_LLM_FULL, _LLM_SUMMARY, _LLM_NEVER) else _LLM_FULL

    def visible(self, paper: Paper, *, is_admin: bool, for_llm: bool) -> bool:
        if str(paper.fields.get('anthive_access') or 'public') == 'restricted' and not is_admin:
            return False
        if for_llm and self.llm_policy(paper) == _LLM_NEVER:
            return False
        return True

    def full_text_allowed(self, paper: Paper, *, for_llm: bool) -> bool:
        return not (for_llm and self.llm_policy(paper) != _LLM_FULL)

    def _visible_papers(self, is_admin: bool, for_llm: bool) -> Dict[str, Paper]:
        return {ck: p for ck, p in self.state.papers.items()
                if self.visible(p, is_admin=is_admin, for_llm=for_llm)}

    def get(self, citekey: str, *, is_admin: bool, for_llm: bool) -> Optional[Paper]:
        paper = self.state.papers.get(citekey)
        if paper is None or not self.visible(paper, is_admin=is_admin, for_llm=for_llm):
            return None
        return paper

    # ── records ───────────────────────────────────────────────────────────
    def summary(self, paper: Paper) -> Dict[str, Any]:
        f = paper.fields
        authors = _as_list(f.get('author'))
        links = self.state.dataset_links.get(paper.citekey, [])
        return {
            'citekey': paper.citekey,
            'entrytype': f.get('entrytype'),
            'title': f.get('title'),
            'authors': authors,
            'first_author': authors[0] if authors else None,
            'last_author': authors[-1] if len(authors) > 1 else None,
            'citation': _short_citation(f),
            'year': _year(f),
            'journal': f.get('journaltitle') or f.get('journal'),
            'doi': normalize_doi(f['doi']) if f.get('doi') else None,
            'pmid': str(f['pmid']) if f.get('pmid') else None,
            'role': f.get('anthive_role'),
            'celltypes': _as_list(f.get('anthive_celltypes')),
            'tissues': _as_list(f.get('anthive_tissues')),
            'diseases': _as_list(f.get('anthive_diseases')),
            'organisms': _as_list(f.get('anthive_organisms')),
            'methods': _as_list(f.get('anthive_methods')),
            'cohorts': _as_list(f.get('anthive_cohorts')),
            'summary': f.get('anthive_summary'),
            'access': f.get('anthive_access') or 'public',
            'llm': self.llm_policy(paper),
            'has_pdf': self.pdf_path(paper) is not None,
            'n_datasets': len({link.db_id for link in links}),
            'draft_fields': _as_list(f.get('anthive_draft')),
        }

    def record(self, paper: Paper, *, is_admin: bool, for_llm: bool) -> Dict[str, Any]:
        out = self.summary(paper)
        full = self.full_text_allowed(paper, for_llm=for_llm)
        paper = self._without_hidden_links(paper, self._visible_papers(is_admin, for_llm))
        fields = {k: v for k, v in paper.fields.items() if k != 'anthive_cites'}
        if not full:
            fields.pop('abstract', None)
        out['fields'] = fields
        out['n_cites'] = len(_as_list(paper.fields.get('anthive_cites')))
        out['notes'] = paper.body if full else None
        out['datasets'] = [{'db_id': link.db_id, 'role': link.role, 'scope': link.scope,
                            'via': link.source}
                           for link in sorted(self.state.dataset_links.get(paper.citekey, []),
                                              key=lambda l: (l.role != 'source', l.db_id))]
        out['genesets'] = [{'id': scope, 'role': role}
                           for scope, role in sorted(self.state.geneset_links.get(paper.citekey, []))]
        out['related'] = self.related(paper.citekey, is_admin=is_admin, for_llm=for_llm, limit=10)
        return out

    def list(self, *, is_admin: bool, for_llm: bool, role: Optional[str] = None,
             celltype: Optional[str] = None, tissue: Optional[str] = None,
             disease: Optional[str] = None, method: Optional[str] = None,
             cohort: Optional[str] = None, organism: Optional[str] = None,
             author: Optional[str] = None, year: Optional[str] = None,
             q: Optional[str] = None) -> List[Dict[str, Any]]:
        filters = {'celltype': celltype, 'tissue': tissue, 'disease': disease,
                   'method': method, 'cohort': cohort, 'organism': organism}
        year_lo = year_hi = None
        if year:
            parts = str(year).split('..')
            year_lo = int(parts[0]) if parts[0] else None
            year_hi = int(parts[-1]) if parts[-1] else None
        words = [_norm(w) for w in (q or '').split() if w.strip()]
        out = []
        for paper in self._visible_papers(is_admin, for_llm).values():
            f = paper.fields
            if role and str(f.get('anthive_role')) != role:
                continue
            ok = True
            for kind, wanted in filters.items():
                if wanted and not any(_norm(wanted) in _norm(v) for v in _as_list(f.get(TOPIC_FIELDS[kind]))):
                    ok = False
                    break
            if not ok:
                continue
            if author and not any(_norm(author) in _norm(a) for a in _as_list(f.get('author'))):
                continue
            y = _year(f)
            if (year_lo is not None and (y is None or y < year_lo)) or \
               (year_hi is not None and (y is None or y > year_hi)):
                continue
            if words:
                blob = _norm(' '.join([paper.citekey, str(f.get('title') or ''), str(f.get('doi') or ''),
                                       str(f.get('journaltitle') or f.get('journal') or ''),
                                       str(f.get('anthive_summary') or ''), ' '.join(_as_list(f.get('author'))),
                                       ' '.join(_as_list(f.get('keywords')))]
                                      + [' '.join(_as_list(f.get(v))) for v in TOPIC_FIELDS.values()]))
                if not all(w in blob for w in words):
                    continue
            out.append(self.summary(paper))
        out.sort(key=lambda s: (-(s['year'] or 0), s['citekey']))
        return out

    def _without_hidden_links(self, paper: Paper, visible: Iterable[str]) -> Paper:
        """A copy whose ``related`` list only names papers the caller may see,
        so a restricted paper's citekey never leaks through its neighbours."""
        visible = set(visible)
        related = _as_list(paper.fields.get('related'))
        if all(ck in visible for ck in related):
            return paper
        fields = dict(paper.fields)
        kept = [ck for ck in related if ck in visible]
        if kept:
            fields['related'] = kept
        else:
            fields.pop('related', None)
            fields.pop('relatedtype', None)
        return Paper(citekey=paper.citekey, fields=fields, body=paper.body, path=paper.path)

    def bibtex(self, citekeys: Optional[Iterable[str]], *, is_admin: bool,
               classic: bool = False) -> str:
        papers = self._visible_papers(is_admin, for_llm=False)
        chosen = [papers[ck] for ck in citekeys if ck in papers] if citekeys is not None \
            else list(papers.values())
        chosen = [self._without_hidden_links(p, papers) for p in chosen]
        return export_bibtex(chosen, include_anthive=True, classic=classic)

    def bibtex_one(self, paper: Paper, classic: bool = False, *, is_admin: bool = False) -> str:
        visible = self._visible_papers(is_admin, for_llm=False)
        return to_bibtex(self._without_hidden_links(paper, visible), include_anthive=True,
                         classic=classic) + '\n'

    def pdf_path(self, paper: Paper) -> Optional[Path]:
        name = paper.fields.get('file')
        if not name or paper.path is None:
            return None
        # Only a .pdf named relative to the record's own folder, never a path
        # that climbs out of it (the file itself may be a symlink).
        rel = Path(str(name))
        if rel.is_absolute() or '..' in rel.parts or rel.suffix.lower() != '.pdf':
            return None
        candidate = paper.path.parent.resolve() / rel
        return candidate if candidate.is_file() else None

    def pdf_s3_relpath(self, paper: Paper) -> Optional[str]:
        """The PDF's path relative to ``<library>/papers`` (e.g.
        ``reading/2024/foo.pdf``), which is also its key under the library
        S3 prefix. None when the record names no PDF or names anything else."""
        from library import library_papers_dir  # noqa: PLC0415
        name = paper.fields.get('file')
        if (not name or paper.path is None or self.library_dir is None
                or Path(str(name)).suffix.lower() != '.pdf' or '..' in Path(str(name)).parts):
            return None
        papers_dir = library_papers_dir(self.library_dir).resolve()
        target = (paper.path.parent / str(name)).resolve()
        try:
            return target.relative_to(papers_dir).as_posix()
        except ValueError:
            return None

    # ── relations ─────────────────────────────────────────────────────────
    def related(self, citekey: str, *, is_admin: bool, for_llm: bool,
                kinds: Optional[Sequence[str]] = None, limit: int = 25) -> List[Dict[str, Any]]:
        papers = self._visible_papers(is_admin, for_llm)
        me = papers.get(citekey)
        if me is None:
            return []
        wanted = set(kinds) if kinds else set(RELATION_KINDS)
        scores: Dict[str, Dict[str, Any]] = {}

        def add(other: str, kind: str, weight: float, evidence: Any) -> None:
            if other == citekey or other not in papers or kind not in wanted:
                return
            weight = min(weight, 3.0)   # no single kind (e.g. a long shared author list) dominates
            entry = scores.setdefault(other, {'citekey': other, 'score': 0.0, 'relations': []})
            entry['score'] += weight
            entry['relations'].append({'kind': kind, 'weight': round(weight, 3), 'evidence': evidence})

        # explicit, both directions
        for other in _as_list(me.fields.get('related')):
            add(other, 'explicit', 3.0, me.fields.get('relatedtype') or 'related')
        for ck, paper in papers.items():
            if citekey in _as_list(paper.fields.get('related')):
                add(ck, 'explicit', 3.0, paper.fields.get('relatedtype') or 'related')
        # shared topical terms, weighted by rarity across the library
        for kind, field_name in TOPIC_FIELDS.items():
            if kind == 'organism':
                continue
            freq = Counter(_norm(v) for p in papers.values() for v in set(_as_list(p.fields.get(field_name))))
            mine = {_norm(v) for v in _as_list(me.fields.get(field_name))} - _GENERIC_TERMS
            if not mine:
                continue
            for ck, paper in papers.items():
                shared = mine & {_norm(v) for v in _as_list(paper.fields.get(field_name))}
                if shared:
                    weight = sum(1.0 / math.log(1 + freq[t]) for t in shared if freq[t])
                    if kind == 'cohort':
                        weight *= 2.0
                    add(ck, kind, weight, sorted(shared))
        # shared authors (ORCID when known), rarity-weighted so consortia do not link everything
        def keys(p: Paper) -> Dict[str, str]:
            orcids = p.fields.get('anthive_orcids') or {}
            orcids = orcids if isinstance(orcids, dict) else {}
            return {_author_key(a, orcids): a for a in _as_list(p.fields.get('author'))}
        author_freq = Counter(k for p in papers.values() for k in keys(p))
        mine_authors = keys(me)
        for ck, paper in papers.items():
            other_authors = keys(paper)
            shared = set(mine_authors) & set(other_authors)
            if shared:
                weight = sum(1.0 / math.log(1 + author_freq[k]) for k in shared)
                add(ck, 'author', weight, sorted(mine_authors[k] for k in shared))
        # citations
        doi_to_ck = {normalize_doi(p.fields['doi']): ck for ck, p in papers.items() if p.fields.get('doi')}
        for doi in _as_list(me.fields.get('anthive_cites')):
            target = doi_to_ck.get(normalize_doi(doi))
            if target:
                add(target, 'cites', 1.5, doi)
        my_doi = normalize_doi(me.fields['doi']) if me.fields.get('doi') else None
        if my_doi:
            for ck, paper in papers.items():
                if my_doi in {normalize_doi(d) for d in _as_list(paper.fields.get('anthive_cites'))}:
                    add(ck, 'cited_by', 1.5, my_doi)
        ranked = sorted(scores.values(), key=lambda e: (-e['score'], e['citekey']))[:limit]
        for entry in ranked:
            paper = papers[entry['citekey']]
            entry['score'] = round(entry['score'], 3)
            entry['title'] = paper.fields.get('title')
            entry['citation'] = _short_citation(paper.fields)
            entry['year'] = _year(paper.fields)
        return ranked

    def papers_for_dataset(self, db_id: str, *, is_admin: bool, for_llm: bool) -> List[Dict[str, Any]]:
        out = []
        for citekey, role in self.state.papers_by_db.get(db_id, []):
            paper = self.get(citekey, is_admin=is_admin, for_llm=for_llm)
            if paper is None:
                continue
            item = self.summary(paper)
            item['link_role'] = role
            out.append(item)
        return out

    # ── literature search and pages ───────────────────────────────────────
    def search(self, query: str, *, is_admin: bool, for_llm: bool, k: int = 10,
               mode: str = 'hybrid', sections: Optional[Sequence[str]] = None,
               citekeys: Optional[Sequence[str]] = None, year: Optional[str] = None) -> Dict[str, Any]:
        if self.index is None:
            return {'query': query, 'mode': None, 'results': [],
                    'warnings': [self.index_error or 'literature index still loading']}
        allowed = {ck for ck, p in self._visible_papers(is_admin, for_llm).items()
                   if self.full_text_allowed(p, for_llm=for_llm)}
        if citekeys:
            allowed &= set(citekeys)
        year_lo = year_hi = None
        if year:
            parts = str(year).split('..')
            year_lo = int(parts[0]) if parts[0] else None
            year_hi = int(parts[-1]) if parts[-1] else None
        # PDFs without a library record (no citekey) are not served: their
        # access policy is unknown.
        raw = self.index.search(query, k=max(k * 3, k), mode=mode, citekeys=allowed or {'\x00'},
                                sections=sections or None, year_min=year_lo, year_max=year_hi)
        results = []
        for row in raw['results']:
            paper = self.state.papers.get(row.get('citekey'))
            if paper is None:
                continue
            results.append({
                'citekey': row['citekey'],
                'citation': _short_citation(paper.fields),
                'title': paper.fields.get('title') or row.get('title'),
                'year': _year(paper.fields) or row.get('year'),
                'doi': row.get('doi'),
                'page_start': row.get('page_start'),
                'page_end': row.get('page_end'),
                'section': row.get('section'),
                'heading': row.get('heading'),
                'snippet': highlight_snippet(row.get('text') or '', query),
                'text': row.get('text'),
                'score': row.get('score'),
                'chunk_id': row.get('chunk_id'),
            })
            if len(results) >= k:
                break
        return {'query': query, 'mode': raw['mode'], 'results': results, 'warnings': raw['warnings']}

    def page_text(self, paper: Paper, page: int) -> Optional[str]:
        if self.index is None:
            return None
        parts = [r for r in self.index.rows
                 if r.get('citekey') == paper.citekey and (r.get('page_start') or 0) <= page <= (r.get('page_end') or 0)]
        if not parts:
            return None
        parts.sort(key=lambda r: r.get('chunk_id') or '')
        return '\n\n'.join(r['text'] for r in parts)

    def n_pages(self, paper: Paper) -> int:
        if self.index is None:
            return 0
        return max((r.get('page_end') or 0 for r in self.index.rows if r.get('citekey') == paper.citekey), default=0)

    # ── per-dataset background pack ───────────────────────────────────────
    def context(self, db_id: str, info: Dict[str, Any], *, module_scores: Sequence[str] = (),
                level: str = 'brief', budget_tokens: int = 4000,
                is_admin: bool, for_llm: bool) -> Dict[str, Any]:
        """Everything an LLM should know before analysing ``db_id``, trimmed
        to ``budget_tokens`` (approximate, same estimate as the index)."""
        from library_index import count_tokens  # noqa: PLC0415
        full = level == 'full'
        sections: List[Dict[str, Any]] = []

        lines = [f"# Dataset {db_id}: {info.get('title') or ''}".rstrip()]
        for label, key in (('Organism', 'organism'), ('Tissue', 'tissue'), ('Region', 'region'),
                           ('Cell type', 'cell_type'), ('Disease', 'disease'), ('Condition', 'condition'),
                           ('Technology', 'technology'), ('Suspension', 'suspension_type'),
                           ('Experimental system', 'experimental_system')):
            value = info.get(key)
            if value not in (None, '', []):
                lines.append(f"- {label}: {', '.join(map(str, value)) if isinstance(value, list) else value}")
        if info.get('n_cells'):
            lines.append(f"- Size: {info.get('n_cells')} cells x {info.get('n_genes')} genes")
        layers = info.get('layers') or {}
        if layers:
            lines.append(f"- Expression layers: {', '.join(f'{k} ({v})' for k, v in layers.items())}; "
                         f"default {info.get('default_layer') or 'X'}")
        description = (info.get('description') or '').strip()
        if description:
            lines.append('')
            lines.append(description[:1500] if full else description[:600])
        sections.append({'name': 'dataset', 'priority': 0, 'text': '\n'.join(lines)})

        caveats = info.get('caveats') or []
        if caveats:
            rendered = []
            for cav in caveats:
                if isinstance(cav, dict):
                    rendered.append(f"- [{cav.get('level', 'note')}] {cav.get('message') or cav.get('text') or cav}")
                else:
                    rendered.append(f"- {cav}")
            sections.append({'name': 'caveats', 'priority': 0,
                             'text': '## Known caveats\n' + '\n'.join(rendered)})

        obs = info.get('obs') or {}
        cat = [c['name'] if isinstance(c, dict) else c for c in obs.get('categorical') or []]
        num = [c['name'] if isinstance(c, dict) else c for c in obs.get('numerical') or []]
        facets = [c for c in cat + num if str(c).startswith('ant_')]
        obs_lines = ['## Cell metadata (obs columns, use these exact names)']
        if facets:
            obs_lines.append(f"- Standardised facets (same meaning across datasets): {', '.join(facets)}")
        obs_lines.append(f"- Categorical: {', '.join(map(str, cat[:60]))}{' ...' if len(cat) > 60 else ''}")
        obs_lines.append(f"- Numerical: {', '.join(map(str, num[:60]))}{' ...' if len(num) > 60 else ''}")
        obs_lines.append('- Statistics across cells are pseudoreplicated: for donor-level claims '
                         'aggregate per donor (ant_donor) first.')
        sections.append({'name': 'obs', 'priority': 1, 'text': '\n'.join(obs_lines)})

        if module_scores:
            shown = list(module_scores)[: (80 if full else 25)]
            sections.append({'name': 'module_scores', 'priority': 3,
                             'text': '## Precomputed module scores\n' + ', '.join(shown)
                             + (f" (+{len(module_scores) - len(shown)} more)" if len(module_scores) > len(shown) else '')})

        papers = self.papers_for_dataset(db_id, is_admin=is_admin, for_llm=for_llm)
        sources = [p for p in papers if p['link_role'] == 'source']
        others = [p for p in papers if p['link_role'] != 'source']
        for paper in sources:
            text = [f"## Source paper: {paper['citation']} ({paper['citekey']})",
                    f"{paper['title']}. {paper.get('journal') or ''} {paper.get('year') or ''}. "
                    f"doi:{paper.get('doi') or '-'}".strip()]
            if paper.get('summary'):
                text.append(f"Summary: {paper['summary']}")
            for label, key in (('Cohorts', 'cohorts'), ('Methods', 'methods'), ('Cell types', 'celltypes')):
                if paper.get(key):
                    text.append(f"{label}: {', '.join(paper[key])}")
            sections.append({'name': f"source:{paper['citekey']}", 'priority': 2, 'text': '\n'.join(text)})
            record = self.state.papers.get(paper['citekey'])
            if self.index is not None and record is not None and self.full_text_allowed(record, for_llm=for_llm):
                queries = [('methods', 'tissue dissociation nuclei isolation sequencing quality control '
                                       'normalization clustering cell type annotation donors cohort', 3 if full else 2),
                           ('results', 'main findings microglia states disease association', 2 if full else 1)]
                for section, query, k in queries:
                    hits = self.index.search(query, k=k, mode='hybrid', citekeys={paper['citekey']},
                                             sections=[section])['results']
                    for hit in hits:
                        sections.append({
                            'name': f"passage:{paper['citekey']}:{section}", 'priority': 4 if section == 'methods' else 5,
                            'text': f"### {paper['citation']}, {section}, p.{hit.get('page_start')}\n{hit['text']}"})
        if others:
            text = ['## Other papers linked to this dataset']
            text += [f"- {p['citation']} ({p['citekey']}, {p['link_role']}): {p['title']}" for p in others]
            sections.append({'name': 'linked_papers', 'priority': 3, 'text': '\n'.join(text)})
        if sources:
            related = {}
            for paper in sources:
                for item in self.related(paper['citekey'], is_admin=is_admin, for_llm=for_llm,
                                         limit=8 if full else 4):
                    related.setdefault(item['citekey'], item)
            if related:
                text = ['## Related papers in the library']
                for item in list(related.values())[: (8 if full else 4)]:
                    kinds = ', '.join(sorted({r['kind'] for r in item['relations']}))
                    text.append(f"- {item['citation']} ({item['citekey']}; {kinds}): {item['title']}")
                sections.append({'name': 'related_papers', 'priority': 6, 'text': '\n'.join(text)})

        # Trim to budget by priority, keeping document order for what stays.
        budget = max(200, int(budget_tokens))
        for index, sec in enumerate(sections):
            sec['order'] = index
            sec['tokens'] = count_tokens(sec['text'])
        kept, used, dropped = [], 0, []
        for sec in sorted(sections, key=lambda s: (s['priority'], s['order'])):
            if used + sec['tokens'] <= budget:
                kept.append(sec)
                used += sec['tokens']
            else:
                dropped.append(sec['name'])
        kept.sort(key=lambda s: s['order'])
        return {
            'db_id': db_id,
            'level': level,
            'budget_tokens': budget,
            'tokens': used,
            'truncated': bool(dropped),
            'dropped_sections': dropped,
            'papers': [{'citekey': p['citekey'], 'role': p['link_role'], 'citation': p['citation']} for p in papers],
            'sections': [{'name': s['name'], 'tokens': s['tokens'], 'text': s['text']} for s in kept],
            'text': '\n\n'.join(s['text'] for s in kept),
            'index_available': self.index is not None,
        }
