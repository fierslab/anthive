"""Single source of truth for anthive's conceptual help.

The web help page (``web/templates/help.html``), the REST API
(``/api/help``), the MCP tool ``explain`` and the served skill all read the
files in this package, so they cannot drift apart.

Layout::

    lib/help_content/
        topics/<topic-id>.yaml   one file per topic
        glossary.yaml            glossary terms (anchor ``#glossary-<slug>``)

A topic file has the keys ``id``, ``title``, ``summary`` (one or two plain
sentences), ``body_html`` (the HTML fragment the help page shows), and the
optional ``title_html`` (heading with inline markup), ``related`` (topic ids), ``terms`` (glossary slugs), ``children``
(topic ids rendered after this body as ``<h3>`` subsections of it),
``aliases`` (extra words ``find`` matches) and ``ui`` (the part of the web UI
the topic describes: facets, caveats, api or mcp). The literal ``{{ docs }}``
in HTML is replaced by the documentation base URL when rendering.

A site can switch parts of its web UI off (``ui:`` in anthive.yaml). The web
help page then leaves out topics and glossary terms whose ``ui`` is off, and
any element of a body marked ``data-ui="<part>"`` (:func:`strip_ui`). The API
and the MCP ``explain`` tool always serve everything.

The markdown served by the API is derived from the HTML by
:func:`html_to_markdown`, so there is exactly one text to maintain.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional

import yaml

CONTENT_DIR = Path(__file__).resolve().parent
DOCS_PLACEHOLDER = '{{ docs }}'
DEFAULT_DOCS_URL = 'https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/blob/main/doc'


@dataclass(frozen=True)
class GlossaryTerm:
    """One glossary entry.

    Attributes:
        slug: Anchor slug; the help page anchor is ``#glossary-<slug>``.
        term: Display label.
        definition_html: Definition as an HTML fragment.
    """

    slug: str
    term: str
    definition_html: str
    ui: str = ''


@dataclass(frozen=True)
class Topic:
    """One help topic (a section or subsection of the help page).

    Attributes:
        id: Stable id, also the anchor on the help page.
        title: Heading text.
        title_html: Heading with inline markup (``<code>``), or empty.
        summary: One or two plain sentences.
        body_html: HTML fragment (own text only, without children).
        related: Ids of related topics.
        terms: Glossary slugs that belong to this topic.
        children: Ids of subsection topics rendered after the body.
        aliases: Extra search words.
        ui: Part of the web UI the topic describes ('' for none).
    """

    id: str
    title: str
    summary: str
    body_html: str
    title_html: str = ''
    related: List[str] = field(default_factory=list)
    terms: List[str] = field(default_factory=list)
    children: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    ui: str = ''


# ── HTML to markdown ──────────────────────────────────────────────────────────

class _MarkdownBuilder(HTMLParser):
    """Convert the small HTML subset used in help bodies to markdown."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._list_stack: List[List] = []   # [kind, counter]
        self._link_stack: List[str] = []
        self._in_pre = False
        self._row_cells = 0
        self._header_row = False
        self._table_rows = 0

    def _emit(self, text: str) -> None:
        self.parts.append(text)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ('p', 'dl', 'table'):
            self._emit('\n\n')
        elif tag in ('h2', 'h3', 'h4'):
            self._emit('\n\n' + '#' * int(tag[1]) + ' ')
        elif tag in ('ul', 'ol'):
            if not self._list_stack:
                self._emit('\n\n')
            self._list_stack.append([tag, 0])
        elif tag == 'li':
            depth = max(len(self._list_stack) - 1, 0)
            marker = '-'
            if self._list_stack and self._list_stack[-1][0] == 'ol':
                self._list_stack[-1][1] += 1
                marker = f'{self._list_stack[-1][1]}.'
            self._emit('\n' + '  ' * depth + marker + ' ')
        elif tag == 'dt':
            self._emit('\n\n**')
        elif tag == 'dd':
            self._emit('\n')
        elif tag == 'tr':
            self._row_cells = 0
            self._header_row = False
            self._emit('\n| ')
        elif tag in ('td', 'th'):
            self._row_cells += 1
            self._header_row = self._header_row or tag == 'th'
        elif tag in ('strong', 'b'):
            self._emit('**')
        elif tag in ('em', 'i'):
            self._emit('*')
        elif tag == 'code':
            self._emit('`')
        elif tag == 'pre':
            self._in_pre = True
            self._emit('\n\n```\n')
        elif tag == 'br':
            self._emit('\n')
        elif tag == 'a':
            href = dict(attrs).get('href') or ''
            self._link_stack.append(href)
            if href:
                self._emit('[')

    def handle_endtag(self, tag: str) -> None:
        if tag in ('ul', 'ol'):
            if self._list_stack:
                self._list_stack.pop()
            if not self._list_stack:
                self._emit('\n')
        elif tag == 'dt':
            self._emit('**')
        elif tag in ('td', 'th'):
            self._emit(' | ')
        elif tag == 'tr':
            self._table_rows += 1
            if self._header_row:
                self._emit('\n|' + ' --- |' * self._row_cells)
        elif tag in ('strong', 'b'):
            self._emit('**')
        elif tag in ('em', 'i'):
            self._emit('*')
        elif tag == 'code':
            self._emit('`')
        elif tag == 'pre':
            self._in_pre = False
            self._emit('\n```\n')
        elif tag == 'a':
            href = self._link_stack.pop() if self._link_stack else ''
            if href:
                self._emit(f']({href})')

    def handle_data(self, data: str) -> None:
        self._emit(data if self._in_pre else re.sub(r'\s+', ' ', data))

    def result(self) -> str:
        """Return the tidied markdown text."""
        text = ''.join(self.parts)
        text = re.sub(r'[ \t]+\n', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'^(\s*(?:-|\d+\.)) +', r'\1 ', text, flags=re.M)
        return text.strip()


def html_to_markdown(fragment: str) -> str:
    """Convert a help HTML fragment to markdown.

    Supports p, h2-h4, ul/ol/li (nested), dl/dt/dd, strong, em, code, pre,
    a, br and simple tables; any other tag is dropped but its text kept.

    Args:
        fragment: HTML fragment.

    Returns:
        Markdown text without em-dashes added by the converter.
    """
    builder = _MarkdownBuilder()
    builder.feed(fragment)
    builder.close()
    return builder.result()


def html_to_text(fragment: str) -> str:
    """Return the plain text of an HTML fragment (tags removed).

    Args:
        fragment: HTML fragment.

    Returns:
        Whitespace-normalised text.
    """
    markdown = html_to_markdown(fragment)
    return re.sub(r'\s+', ' ', re.sub(r'[*`#]|\]\([^)]*\)|\[', '', markdown)).strip()


# ── loading ───────────────────────────────────────────────────────────────────

def _read_yaml(path: Path) -> dict:
    with path.open(encoding='utf-8') as handle:
        return yaml.safe_load(handle) or {}


UI_PARTS = ('facets', 'caveats', 'api', 'mcp')


def _ui_part(value, where: str) -> str:
    """Validate the optional ``ui`` key of a topic or term."""
    if value in (None, ''):
        return ''
    if value not in UI_PARTS:
        raise ValueError(f'{where}: ui must be one of {", ".join(UI_PARTS)}, not {value!r}')
    return str(value)


_UI_ATTR = re.compile(r'<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*\bdata-ui="([a-z]+)"[^>]*>')
_VOID_TAGS = frozenset({'br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'})


def strip_ui(fragment: str, hidden=()) -> str:
    """Remove every element marked ``data-ui="<part>"`` whose part is in ``hidden``.

    The element is removed with its content up to the matching close tag
    (same tag name, nesting counted).

    Args:
        fragment: HTML fragment.
        hidden: Parts that are switched off.

    Returns:
        The fragment without those elements.
    """
    hidden = set(hidden or ())
    if not hidden or 'data-ui=' not in fragment:
        return fragment
    out: List[str] = []
    position = 0
    while True:
        match = _UI_ATTR.search(fragment, position)
        if match is None:
            out.append(fragment[position:])
            break
        tag, part = match.group(1).lower(), match.group(2)
        if part not in hidden:
            out.append(fragment[position:match.end()])
            position = match.end()
            continue
        out.append(fragment[position:match.start()])
        if tag in _VOID_TAGS or match.group(0).endswith('/>'):
            position = match.end()
            continue
        depth, cursor = 1, match.end()
        pattern = re.compile(rf'<(/?){tag}\b[^>]*?(/?)>', re.I)
        while depth and cursor < len(fragment):
            inner = pattern.search(fragment, cursor)
            if inner is None:
                cursor = len(fragment)
                break
            if inner.group(1):
                depth -= 1
            elif not inner.group(2):
                depth += 1
            cursor = inner.end()
        position = cursor
    return ''.join(out)


@lru_cache(maxsize=1)
def _load() -> tuple:
    """Load and validate all help files once per process.

    Returns:
        ``(topics, glossary)``: ordered dict of topics and ordered list of terms.

    Raises:
        ValueError: A topic or term is malformed, ids repeat, or a reference
            (related, children, terms) points at nothing.
    """
    topics: Dict[str, Topic] = {}
    for path in sorted((CONTENT_DIR / 'topics').glob('*.yaml')):
        raw = _read_yaml(path)
        missing = [key for key in ('id', 'title', 'summary', 'body_html') if not raw.get(key)]
        if missing:
            raise ValueError(f'{path.name}: missing {missing}')
        if raw['id'] != path.stem:
            raise ValueError(f'{path.name}: id {raw["id"]!r} must equal the file name')
        topics[raw['id']] = Topic(
            id=raw['id'], title=raw['title'].strip(), summary=' '.join(raw['summary'].split()),
            body_html=raw['body_html'].strip('\n'), title_html=(raw.get('title_html') or '').strip(),
            related=list(raw.get('related') or []),
            terms=list(raw.get('terms') or []), children=list(raw.get('children') or []),
            aliases=list(raw.get('aliases') or []), ui=_ui_part(raw.get('ui'), path.name),
        )
    glossary: List[GlossaryTerm] = []
    for entry in _read_yaml(CONTENT_DIR / 'glossary.yaml').get('terms', []):
        glossary.append(GlossaryTerm(slug=entry['slug'], term=entry['term'].strip(),
                                     definition_html=entry['definition_html'].strip(),
                                     ui=_ui_part(entry.get('ui'), f"glossary.yaml {entry['slug']}")))
    slugs = [term.slug for term in glossary]
    if len(slugs) != len(set(slugs)):
        raise ValueError('glossary.yaml: duplicate slug')
    for topic in topics.values():
        for ref in topic.related + topic.children:
            if ref not in topics:
                raise ValueError(f'{topic.id}: unknown topic {ref!r}')
        for slug in topic.terms:
            if slug not in slugs:
                raise ValueError(f'{topic.id}: unknown glossary term {slug!r}')
    return topics, glossary


def reload() -> None:
    """Drop the cache so the next call re-reads the files (tests, dev)."""
    _load.cache_clear()


def topics() -> List[Topic]:
    """Return all topics in file-name order."""
    return list(_load()[0].values())


def glossary() -> List[GlossaryTerm]:
    """Return all glossary terms in file order."""
    return list(_load()[1])


def get_topic(topic_id: str) -> Optional[Topic]:
    """Return one topic by exact id, or None."""
    return _load()[0].get(topic_id)


def get_term(slug: str) -> Optional[GlossaryTerm]:
    """Return one glossary term by exact slug, or None."""
    return next((term for term in _load()[1] if term.slug == slug), None)


# ── rendering ─────────────────────────────────────────────────────────────────

def fill(text: str, docs_url: str = DEFAULT_DOCS_URL) -> str:
    """Replace the ``{{ docs }}`` placeholder in ``text`` with ``docs_url``."""
    return text.replace(DOCS_PLACEHOLDER, docs_url.rstrip('/'))


def topic_html(topic_id: str, docs_url: str = DEFAULT_DOCS_URL, hidden=()) -> str:
    """Render a topic for the help page: own body, then each child as ``<h3>``.

    Args:
        topic_id: Topic id.
        docs_url: Base URL substituted for ``{{ docs }}``.
        hidden: Parts of the web UI that are switched off: children with
            such a ``ui`` and ``data-ui`` elements of those parts are left out.

    Returns:
        HTML fragment ('' when the topic itself belongs to a hidden part).

    Raises:
        KeyError: Unknown topic id.
    """
    hidden = set(hidden or ())
    topic = _load()[0][topic_id]
    if topic.ui and topic.ui in hidden:
        return ''
    chunks = [strip_ui(fill(topic.body_html, docs_url), hidden)]
    for child_id in topic.children:
        child = _load()[0][child_id]
        if child.ui and child.ui in hidden:
            continue
        chunks.append(f'<h3 class="subsection-head" id="{escape(child.id)}">{child.title_html or escape(child.title, quote=False)}</h3>')
        chunks.append(strip_ui(fill(child.body_html, docs_url), hidden))
    return '\n'.join(chunks)


def glossary_html(docs_url: str = DEFAULT_DOCS_URL, hidden=()) -> str:
    """Render the glossary ``<dt>/<dd>`` pairs for the help page (terms of hidden parts left out)."""
    hidden = set(hidden or ())
    return '\n'.join(
        f'<dt id="glossary-{term.slug}">{escape(term.term, quote=False)}</dt>\n'
        f'<dd>{strip_ui(fill(term.definition_html, docs_url), hidden)}</dd>'
        for term in _load()[1] if not (term.ui and term.ui in hidden))


def topic_markdown(topic_id: str, include_children: bool = True, docs_url: str = DEFAULT_DOCS_URL) -> str:
    """Return a topic as markdown, optionally followed by its children.

    Args:
        topic_id: Topic id.
        include_children: Append child topics as ``###`` sections.
        docs_url: Base URL substituted for ``{{ docs }}``.

    Returns:
        Markdown text.

    Raises:
        KeyError: Unknown topic id.
    """
    topic = _load()[0][topic_id]
    parts = [html_to_markdown(fill(topic.body_html, docs_url))]
    if include_children:
        for child_id in topic.children:
            child = _load()[0][child_id]
            parts.append(f'### {child.title}\n\n' + html_to_markdown(fill(child.body_html, docs_url)))
    return '\n\n'.join(part for part in parts if part)


def term_markdown(slug: str, docs_url: str = DEFAULT_DOCS_URL) -> str:
    """Return a glossary definition as markdown.

    Raises:
        KeyError: Unknown slug.
    """
    term = get_term(slug)
    if term is None:
        raise KeyError(slug)
    return html_to_markdown(fill(term.definition_html, docs_url))


def topic_links(topic_id: str) -> tuple:
    """Related topics and glossary terms of a topic including its children.

    Args:
        topic_id: Topic id.

    Returns:
        ``(related, terms)``: ids of related topics (own, then children's,
        excluding the topic itself and its children) and glossary slugs.

    Raises:
        KeyError: Unknown topic id.
    """
    all_topics = _load()[0]
    topic = all_topics[topic_id]
    members = [topic] + [all_topics[child] for child in topic.children]
    skip = {topic.id, *topic.children}
    related: List[str] = []
    terms: List[str] = []
    for member in members:
        related += [ref for ref in member.related if ref not in skip and ref not in related]
        terms += [slug for slug in member.terms if slug not in terms]
    return related, terms


# ── lookup ────────────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def find(query: str, limit: int = 5) -> List[dict]:
    """Fuzzy-match a query against topic ids/titles/aliases and glossary terms.

    Exact (normalised) matches rank first, then substring matches, then
    close spellings.

    Args:
        query: Free text such as ``"replicate unit"`` or ``"facet counting"``.
        limit: Maximum number of hits.

    Returns:
        Hits ``{'kind': 'topic'|'term', 'id': str, 'title': str, 'score': float}``,
        best first. Empty when nothing is close.
    """
    needle = _normalise(query)
    if not needle:
        return []
    candidates = []
    for topic in _load()[0].values():
        names = [topic.id, topic.title, *topic.aliases]
        candidates.append(('topic', topic.id, topic.title, [_normalise(name) for name in names]))
    for term in _load()[1]:
        candidates.append(('term', term.slug, term.term, [_normalise(term.slug), _normalise(term.term)]))
    hits = []
    for kind, ident, title, names in candidates:
        if needle in names:
            score = 1.0
        elif any(needle in name or (len(name) > 3 and name in needle) for name in names):
            score = 0.8
        else:
            score = max(difflib.SequenceMatcher(None, needle, name).ratio() for name in names)
            if score < 0.7:
                continue
        hits.append({'kind': kind, 'id': ident, 'title': title, 'score': round(score, 3)})
    hits.sort(key=lambda hit: (-hit['score'], hit['kind'] != 'topic', hit['id']))
    return hits[:limit]


def index() -> dict:
    """Return the compact index served at ``/api/help``.

    Returns:
        ``{'topics': [{id, title, summary}], 'glossary': [{slug, term}]}``.
    """
    return {
        'topics': [{'id': topic.id, 'title': topic.title, 'summary': topic.summary}
                   for topic in _load()[0].values()],
        'glossary': [{'slug': term.slug, 'term': term.term} for term in _load()[1]],
    }
