"""Markdown written by depositors (artefact titles, descriptions and change notes), rendered safely.

Collection and dataset ``.md`` bodies are curated by admins and rendered as they are (``web/browser.py``).
Artefact text comes from any member allowed to deposit, so it must never carry markup of its own:

1. the source is HTML-escaped before markdown runs, so raw HTML (``<script>``, ``<img onerror>``,
   ``<iframe>``) can only ever show as text;
2. the markdown output is re-serialised through an allowlist: paragraphs, emphasis, code, lists, quotes,
   headings (demoted to h4 to h6), tables, rules and links. Every attribute is dropped except a link's
   ``href``, which must be ``http(s)``, ``mailto`` or a site-relative path (``javascript:``, ``data:``,
   protocol-relative ``//host`` and anything else is removed, the text kept). Links get
   ``rel="nofollow noopener noreferrer"``. Images are dropped (no requests to other hosts from a page view).

Input: a string (or None). Output: an HTML string safe to insert with Jinja's ``|safe``.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

ALLOWED_TAGS = {'p', 'br', 'em', 'strong', 'code', 'pre', 'ul', 'ol', 'li', 'blockquote', 'hr', 'a',
                'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'del'}
VOID_TAGS = {'br', 'hr'}
HEADING_MAP = {'h1': 'h4', 'h2': 'h4', 'h3': 'h5', 'h4': 'h5', 'h5': 'h6', 'h6': 'h6'}
SAFE_HREF_RE = re.compile(r'(?:https?://[^\s/][^\s]*|mailto:[^\s]+|/(?!/)[^\s]*|#[^\s]*)', re.I)
MAX_SOURCE_CHARS = 20000


def safe_href(value: Optional[str]) -> Optional[str]:
    """``value`` when it is an http(s), mailto, site-relative or fragment link, else None."""
    if not value:
        return None
    value = value.strip()
    if any(ord(char) < 32 for char in value):
        return None
    return value if SAFE_HREF_RE.fullmatch(value) else None


class _Allowlist(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: List[str] = []
        self.stack: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = HEADING_MAP.get(tag, tag)
        if tag not in ALLOWED_TAGS:
            return
        if tag == 'a':
            href = safe_href(dict(attrs).get('href'))
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
            self.out.append(f'<{tag}>')

    def handle_endtag(self, tag: str) -> None:
        tag = HEADING_MAP.get(tag, tag)
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
        self.out.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.out.append(f'&{name};' if re.fullmatch(r'[A-Za-z][A-Za-z0-9]{1,31}', name) else html.escape(f'&{name}'))

    def handle_charref(self, name: str) -> None:
        self.out.append(f'&#{name};' if re.fullmatch(r'[0-9]{1,7}|[xX][0-9a-fA-F]{1,6}', name) else '')

    def close_all(self) -> str:
        while self.stack:
            open_tag = self.stack.pop()
            if open_tag != 'a-dropped':
                self.out.append(f'</{open_tag}>')
        return ''.join(self.out)


def render(text: Optional[str]) -> str:
    """Safe HTML for depositor markdown (see the module docstring)."""
    if not text:
        return ''
    import markdown  # noqa: PLC0415
    source = html.escape(str(text)[:MAX_SOURCE_CHARS], quote=False)
    rendered = markdown.markdown(source, extensions=['tables', 'fenced_code', 'sane_lists'])
    parser = _Allowlist()
    parser.feed(rendered)
    parser.close()
    return parser.close_all()


def plain(text: Optional[str], max_chars: int = 300) -> str:
    """A single line of plain text (titles): HTML tags, markdown markers and whitespace runs removed; the
    caller's template autoescape escapes what is left."""
    if not text:
        return ''
    value = re.sub(r'<[^>]*>', '', str(text))           # tags go, their text stays
    value = re.sub(r'[`*_#>\[\]]', '', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:max_chars]
