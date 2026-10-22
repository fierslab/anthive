"""Dependencies whose next major version can make stored files unreadable stay capped (2026-10-22).

duckdb 2.0 writes database files (storage version 69) that duckdb 1.5 cannot read; anndata changes its h5ad
encodings between 0.x minors. Every PEP-723 script header that lists one of them must give an upper bound,
and the image lock, the lock overrides and `make test` must stay on the same line, so a script environment
refreshed on one host can never write files the others (or the image) cannot read.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HEADER = re.compile(r'^# /// script\s*$(.*?)^# ///\s*$', re.M | re.S)
DEPS = re.compile(r'dependencies\s*=\s*\[(.*?)\]', re.S)
ITEM = re.compile(r'"([^"]+)"|\'([^\']+)\'')
# package -> the first major (or 0.x minor) line that may change the stored format
CAPPED = {'duckdb': (2, 0), 'anndata': (0, 13)}


def _version(text: str) -> tuple:
    return tuple(int(part) for part in re.findall(r'\d+', text)[:2])


def _scripts() -> list:
    paths = [*REPO.glob('bin/*/*'), *REPO.glob('web/*'), *REPO.glob('docker/*'), *REPO.glob('report/**/*.py'),
             *REPO.glob('frontend/**/*.py')]
    found = []
    for path in sorted(paths):
        if not path.is_file() or path.suffix in ('.md', '.lock', '.txt', '.yaml', '.yml', '.json'):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        match = HEADER.search(text)
        if match:
            deps = DEPS.search(match.group(1))
            if deps:
                found.append((path, [a or b for a, b in ITEM.findall(deps.group(1))]))
    return found


def _upper_bound(spec: str):
    bounds = re.findall(r'<\s*([0-9][0-9.]*)', spec)
    return min((_version(bound) for bound in bounds), default=None)


def test_every_script_header_caps_duckdb_and_anndata():
    scripts = _scripts()
    assert len(scripts) > 30
    problems = []
    for path, deps in scripts:
        for spec in deps:
            name = re.match(r'[A-Za-z0-9_.-]+', spec).group(0).lower()
            if name in CAPPED:
                bound = _upper_bound(spec)
                if bound is None or bound > CAPPED[name]:
                    problems.append(f'{path.relative_to(REPO)}: {spec!r} needs an upper bound at or below '
                                    f'{".".join(map(str, CAPPED[name]))}')
    assert not problems, '\n'.join(problems)


@pytest.mark.parametrize('name', sorted(CAPPED))
def test_the_image_lock_and_the_overrides_stay_on_the_capped_line(name):
    lock = (REPO / 'docker' / 'requirements-app.lock').read_text()
    pinned = re.search(rf'^{name}==([0-9.]+)', lock, re.M)
    assert pinned and _version(pinned.group(1)) < CAPPED[name]
    for line in (REPO / 'docker' / 'requirements-app.in').read_text().splitlines():
        if re.match(rf'{name}\b', line):
            bound = _upper_bound(line.split('#')[0])
            assert bound is not None and bound <= CAPPED[name], line


def test_make_test_pins_duckdb_below_two():
    test_deps = next(line for line in (REPO / 'Makefile').read_text().splitlines() if line.startswith('TEST_DEPS'))
    spec = re.search(r"--with '?(duckdb[^' ]*)", test_deps).group(1)
    assert _upper_bound(spec) is not None and _upper_bound(spec) <= (2, 0)
