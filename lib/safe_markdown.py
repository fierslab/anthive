"""Markdown from the store, the library and depositors, rendered without raw HTML.

Dataset ``.md`` bodies, collection ``index.md`` bodies (which dataset and collection deposits write), paper
notes, gene set bodies and artefact titles, descriptions and change notes can all come from people outside the
admins, so none of them may carry markup of their own (security review round 3):

1. markdown-it-py parses CommonMark plus tables and strikethrough with raw HTML disabled: ``<script>``,
   ``<img onerror>`` and friends become text, entities, code spans and autolinks render correctly;
2. its output is re-serialised through an allowlist: paragraphs, emphasis, code, lists, quotes, headings, tables,
   rules, strikethrough and links. Every attribute is dropped except a link's ``href``, which must be ``http(s)``,
   ``mailto``, a fragment, a site path (``/x``, not ``//host``) or a relative path without a scheme; a backslash or
   a control character anywhere refuses it (``/\\host`` is another site to a browser). Refused links keep their
   text. Links get ``rel="nofollow noopener noreferrer"``. Images are dropped, except plot thumbnails
   (``![caption](plot:GEOM?...)``) when the caller allows them; the web UI turns those into its own plot PNG URL.

``render(text, demote_headings=True, allow_plot_images=False)``: artefact text demotes headings (h1 becomes h4);
store bodies keep them. Output: an HTML string safe to insert with Jinja's ``|safe``.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

ALLOWED_TAGS = {'p', 'br', 'em', 'strong', 'code', 'pre', 'ul', 'ol', 'li', 'blockquote', 'hr', 'a',
                'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 's', 'del'}
VOID_TAGS = {'br', 'hr', 'img'}
DEMOTED = {'h1': 'h4', 'h2': 'h4', 'h3': 'h5', 'h4': 'h5', 'h5': 'h6', 'h6': 'h6'}
SCHEME_RE = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*):')
PLOT_SRC_RE = re.compile(r'plot:[a-z0-9_]+(?:\?[^\s"<>\\]*)?')
MAX_SOURCE_CHARS = 200000


def safe_href(value: Optional[str]) -> Optional[str]:
    """``value`` when it is a safe link target (see the module docstring), else None."""
    if not value:
        return None
    value = value.strip()
    if not value or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    lowered = value.lower()
    if lowered.startswith(('http://', 'https://')):
        return value if len(value) > len('https://') and not value.split('://', 1)[1].startswith('/') else None
    if lowered.startswith('mailto:'):
        return value if len(value) > len('mailto:') else None
    if value.startswith('//'):
        return None                                      # protocol-relative: another host
    if value.startswith(('/', '#', '?')):
        return value
    head = re.split(r'[/?#]', value, maxsplit=1)[0]
    if SCHEME_RE.match(head) and ':' in head:
        return None                                      # any other scheme (javascript:, data:, vbscript:, ...)
    return value                                         # a relative path


class _Allowlist(HTMLParser):
    def __init__(self, demote_headings: bool, allow_plot_images: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.stack: List[str] = []
        self.demote = demote_headings
        self.plots = allow_plot_images

    def _tag(self, tag: str) -> str:
        return DEMOTED.get(tag, tag) if self.demote else tag

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        values = dict(attrs)
        if tag == 'img':
            src = values.get('src') or ''
            if self.plots and PLOT_SRC_RE.fullmatch(src):
                alt = values.get('alt') or ''
                self.out.append(f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}">')
            return
        tag = self._tag(tag)
        if tag not in ALLOWED_TAGS:
            return
        if tag == 'a':
            href = safe_href(values.get('href'))
            if href is None:
                self.stack.append('a-dropped')
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}" rel="nofollow noopener noreferrer">')
            self.stack.append('a')
            return
        self.out.append(f'<{tag}>')
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = self._tag(tag)
        if tag == 'a' and self.stack and self.stack[-1] == 'a-dropped':
            self.stack.pop()
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS or tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            if open_tag != 'a-dropped':
                self.out.append(f'</{open_tag}>')
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data, quote=True))

    def close_all(self) -> str:
        while self.stack:
            open_tag = self.stack.pop()
            if open_tag != 'a-dropped':
                self.out.append(f'</{open_tag}>')
        return ''.join(self.out)


_PARSER = None


def _markdown_it():
    global _PARSER
    if _PARSER is None:
        from markdown_it import MarkdownIt  # noqa: PLC0415
        _PARSER = MarkdownIt('commonmark', {'html': False}).enable(['table', 'strikethrough'])
    return _PARSER


def render(text: Optional[str], demote_headings: bool = True, allow_plot_images: bool = False) -> str:
    """Safe HTML for untrusted markdown (see the module docstring)."""
    if not text:
        return ''
    rendered = _markdown_it().render(str(text)[:MAX_SOURCE_CHARS])
    parser = _Allowlist(demote_headings, allow_plot_images)
    parser.feed(rendered)
    parser.close()
    return parser.close_all()


def raw_html(text: Optional[str]) -> List[str]:
    """The raw HTML a CommonMark renderer with HTML enabled would pass through (blocks, inline tags, comments),
    outside code spans and blocks. Used to refuse such text where it is written into a file that other tools
    render (a collection's index.md), instead of escaping it (escaping shows as &lt; inside code)."""
    if not text:
        return []
    from markdown_it import MarkdownIt  # noqa: PLC0415
    parser = MarkdownIt('commonmark', {'html': True}).enable(['table', 'strikethrough'])
    found: List[str] = []
    for token in parser.parse(str(text)[:MAX_SOURCE_CHARS]):
        if token.type == 'html_block':
            found.append(token.content.strip())
        for child in token.children or []:
            if child.type == 'html_inline':
                found.append(child.content.strip())
    return found


def plain(text: Optional[str], max_chars: int = 300) -> str:
    """A single line of plain text (titles): HTML tags, markdown markers and whitespace runs removed; the
    caller's template autoescape escapes what is left."""
    if not text:
        return ''
    value = re.sub(r'<[^>]*>', '', str(text))           # tags go, their text stays
    value = re.sub(r'[`*_#>\[\]]', '', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:max_chars]
