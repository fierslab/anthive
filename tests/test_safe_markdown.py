"""lib/safe_markdown.py: untrusted markdown never carries markup, scripts, foreign images or unsafe links, and
ordinary markdown (code, entities, quotes, autolinks, tables) renders correctly (security review round 3)."""
from __future__ import annotations

import html
import re

import pytest

import safe_markdown

#: the 31 payloads of the round-3 review
PAYLOADS = [
    '[x](javascript:alert(1))', '[x](JaVaScRiPt:alert(1))', '[x]( javascript:alert(1))',
    '[x](&#106;avascript:alert(1))', '[x](&#x6A;avascript:alert(1))', '[x](java&#09;script:alert(1))',
    '[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)', '<javascript:alert(1)>',
    '<https://ok.example/"onmouseover="alert(1)>', '[x][r]\n\n[r]: javascript:alert(1)',
    '[x][r]\n\n[r]: https://ok.example "title\\" onmouseover=alert(1) x="', '![i](https://evil.example/p.png)',
    '<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '`<script>`', '```\n</code><script>x</script>\n```',
    '[x](https://ok.example/a"onclick="alert(1))', '[x](/\\evil.example)', '[x](/%5Cevil.example)',
    '[x](https://ok.example/?a=<script>)', '&lt;script&gt;alert(1)&lt;/script&gt;', '&#60;script&#62;',
    '[x](vbscript:msgbox(1))', '[x](//evil.example)', '[x](\\\\evil.example)', '<a href="javascript:x">y</a>',
    '*[x](javascript:alert(1))*', '> [x](javascript:alert(1))', '| a |\n|---|\n| [x](javascript:alert(1)) |',
    '[x](#"><script>alert(1)</script>)', '[x](mailto:a@b.c?body=<script>)',
]
OTHER = ['<iframe src="https://evil.example"></iframe>', '<svg onload=alert(1)>',
         '<p style="background:url(javascript:alert(1))">x</p>']


def _tags(out):
    return {tag.lower() for tag in re.findall(r'<([A-Za-z][A-Za-z0-9]*)', out)}


@pytest.mark.parametrize('payload', PAYLOADS + OTHER)
def test_no_payload_gets_through(payload):
    out = safe_markdown.render(payload)
    assert _tags(out) <= safe_markdown.ALLOWED_TAGS
    assert not re.search(r'<[a-z0-9]+\s+(?!href=)[a-z-]+=', out), out          # no attribute but href / rel
    for href in re.findall(r'href="([^"]*)"', out):
        decoded = html.unescape(href).strip().lower()
        assert not decoded.startswith(('javascript:', 'data:', 'vbscript:', '//')) and '\\' not in decoded, href
    assert 'evil.example' not in ' '.join(re.findall(r'href="([^"]*)"', out)) or 'evil.example' in payload and \
        not re.search(r'href="[^"]*//evil', out)


@pytest.mark.parametrize('href', ['/\\evil.example/login', '\\\\evil.example', 'https:///evil', 'java\nscript:x',
                                  'javascript:x', ' data:x', '//evil.example', 'jar:file:x'])
def test_unsafe_hrefs(href):
    assert safe_markdown.safe_href(href) is None


@pytest.mark.parametrize('href', ['https://doi.org/10.1/x', '/browse/S20', '#top', 'mailto:a@b.c', 'notes.html',
                                  '../static/a.pdf', '?tab=genes'])
def test_safe_hrefs(href):
    assert safe_markdown.safe_href(href) == href


def test_ordinary_markdown_renders_correctly():
    out = safe_markdown.render('Use `a < b` and `<b>`, &amp; "quotes", see <https://doi.org/10.1/x> and '
                               '[the page](/browse/S20).\n\n| a | b |\n|---|---|\n| 1 | ~~2~~ |\n')
    assert '<code>a &lt; b</code>' in out and '<code>&lt;b&gt;</code>' in out
    assert '&amp; &quot;quotes&quot;' in out
    assert '<a href="https://doi.org/10.1/x" rel="nofollow noopener noreferrer">https://doi.org/10.1/x</a>' in out
    assert '<a href="/browse/S20" rel="nofollow noopener noreferrer">the page</a>' in out
    assert '<td>1</td>' in out and '<s>2</s>' in out


def test_headings_images_and_plot_thumbnails():
    source = '# Big\n\n![t](https://evil.example/track.png)\n\n![umap](plot:scatter?x=X_umap&db=A/b)'
    artefact = safe_markdown.render(source)
    assert '<h4>Big</h4>' in artefact and '<img' not in artefact
    store = safe_markdown.render(source, demote_headings=False, allow_plot_images=True)
    assert '<h1>Big</h1>' in store and 'evil.example' not in store
    assert '<img src="plot:scatter?x=X_umap&amp;db=A/b" alt="umap">' in store


def test_empty_and_plain():
    assert safe_markdown.render(None) == '' and safe_markdown.render('') == ''
    assert safe_markdown.plain('**Volcano** `plot`\n of  #DE') == 'Volcano plot of DE'
