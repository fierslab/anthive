"""lib/safe_markdown.py: depositor markdown never carries markup, scripts, foreign images or unsafe links."""
from __future__ import annotations

import re

import pytest

import safe_markdown


@pytest.mark.parametrize('source', [
    '<script>alert(1)</script>',
    '<img src=x onerror=alert(1)>',
    '<iframe src="https://evil.example"></iframe>',
    '<a href="javascript:alert(1)">x</a>',
    '<svg onload=alert(1)>',
    '<p style="background:url(javascript:alert(1))">x</p>',
])
def test_raw_html_only_shows_as_text(source):
    out = safe_markdown.render(source)
    assert {tag for _slash, tag in re.findall(r'<(/?)([A-Za-z0-9]+)', out)} <= {'p'}
    assert '&lt;' in out                                   # the markup shows as text


@pytest.mark.parametrize('link', ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,<b>x</b>',
                                  '//evil.example/x', 'vbscript:x', 'java\nscript:alert(1)'])
def test_unsafe_link_targets_are_dropped_and_the_text_kept(link):
    out = safe_markdown.render(f'[click me]({link})')
    assert 'click me' in out and 'href' not in out


def test_safe_links_are_kept_with_rel():
    out = safe_markdown.render('see [the paper](https://doi.org/10.1/x) and [the page](/browse/S20)')
    assert '<a href="https://doi.org/10.1/x" rel="nofollow noopener noreferrer">the paper</a>' in out
    assert '<a href="/browse/S20" rel="nofollow noopener noreferrer">the page</a>' in out


def test_images_are_dropped_headings_demoted_and_basic_markdown_kept():
    out = safe_markdown.render('# Big\n\n![t](https://evil.example/track.png)\n\n*em* **strong** `code`\n\n'
                               '- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n')
    assert '<img' not in out and 'evil.example' not in out
    assert '<h4>Big</h4>' in out and '<h1' not in out
    for tag in ('<em>em</em>', '<strong>strong</strong>', '<code>code</code>', '<li>one</li>', '<td>1</td>'):
        assert tag in out


def test_empty_and_plain():
    assert safe_markdown.render(None) == '' and safe_markdown.render('') == ''
    assert safe_markdown.plain('**Volcano** `plot`\n of  #DE') == 'Volcano plot of DE'
