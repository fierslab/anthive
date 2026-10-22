"""Script headers, the image lock and `make test` agree on the packages that matter (2026-10-22).

Every ``bin/<domain>/ant-*`` tool and ``web/ant-browse`` runs either standalone through its PEP-723 header (its own
uv environment) or in the image's one pinned venv (``docker/requirements-app.lock``, built by ``docker/gen-lock``).
Both must run the same versions, or a script environment refreshed on one host writes files the others cannot read:

* caps that must be present wherever the package is declared (also in ``report/`` scripts): duckdb <1.6 (duckdb 2.0
  writes database files 1.5 cannot read), anndata <0.13 (h5ad encodings change between 0.x minors), pandas <3
  (another default string dtype; anndata 0.12 untested with it), h5py <4 (HDF5 2.x; see
  ``tests/test_h5ad_round_trip.py``);
* every header range of a package the lock pins contains the locked version (the scripts gen-lock leaves out of the
  image, the starCAT tools, run in their own environment and are exempt);
* the lock overrides and the lock input carry the same caps, and ``make test`` pins duckdb.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO = Path(__file__).resolve().parent.parent
HEADER = re.compile(r'^# /// script\s*$(.*?)^# ///\s*$', re.M | re.S)
DEPS = re.compile(r'dependencies\s*=\s*\[(.*?)\]', re.S)
ITEM = re.compile(r'"([^"]+)"|\'([^\']+)\'')
# package -> the version every declaration must stay below
CAPS = {'duckdb': Version('1.6'), 'anndata': Version('0.13'), 'pandas': Version('3.0'), 'h5py': Version('4')}
# packages whose header ranges must contain the locked version (all others in the lock are checked as well)
KEY_PACKAGES = ('duckdb', 'anndata', 'pandas', 'numpy', 'h5py', 'pyarrow', 'scipy')


def _gen_lock():
    loader = importlib.machinery.SourceFileLoader('gen_lock_for_caps', str(REPO / 'docker' / 'gen-lock'))
    spec = importlib.util.spec_from_loader('gen_lock_for_caps', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _headers(patterns) -> list:
    found = []
    for pattern in patterns:
        for path in sorted(REPO.glob(pattern)):
            if not path.is_file() or path.suffix in ('.md', '.lock', '.txt', '.yaml', '.yml', '.json'):
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            match = HEADER.search(text)
            deps = DEPS.search(match.group(1)) if match else None
            if deps:
                found.append((path, [Requirement(a or b) for a, b in ITEM.findall(deps.group(1))]))
    return found


def _locked() -> dict:
    pins = {}
    for line in (REPO / 'docker' / 'requirements-app.lock').read_text().splitlines():
        match = re.match(r'([A-Za-z0-9_.-]+)==([^\s;]+)', line)
        if match:
            pins[canonicalize_name(match.group(1))] = Version(match.group(2))
    return pins


def _below(requirement: Requirement, cap: Version) -> bool:
    """Every version the requirement allows is below ``cap`` (it has an upper bound at or below the cap)."""
    uppers = [Version(spec.version) for spec in requirement.specifier if spec.operator == '<']
    uppers += [Version(spec.version) for spec in requirement.specifier if spec.operator in ('==', '<=')]
    return bool(uppers) and min(uppers) <= cap and not any(
        spec.operator == '<=' and Version(spec.version) >= cap for spec in requirement.specifier)


def test_every_declaration_carries_the_caps():
    headers = _headers(['bin/*/*', 'web/*', 'report/**/*.py', 'frontend/**/*.py', 'docker/*'])
    assert len(headers) > 30
    problems = [f'{path.relative_to(REPO)}: {requirement} must stay below {CAPS[canonicalize_name(requirement.name)]}'
                for path, requirements in headers for requirement in requirements
                if canonicalize_name(requirement.name) in CAPS
                and not _below(requirement, CAPS[canonicalize_name(requirement.name)])]
    assert not problems, '\n'.join(problems)


def test_every_header_range_contains_the_locked_version():
    excluded = set(_gen_lock().EXCLUDED_SCRIPTS)
    locked = _locked()
    for name in KEY_PACKAGES:
        assert name in locked, name
    problems = []
    for path, requirements in _headers(['bin/*/*', 'web/*']):
        if path.name in excluded:
            continue
        for requirement in requirements:
            name = canonicalize_name(requirement.name)
            if name in locked and not requirement.specifier.contains(locked[name], prereleases=True):
                problems.append(f'{path.relative_to(REPO)}: {requirement} excludes the locked {name}=={locked[name]}')
    assert not problems, '\n'.join(problems)


def test_the_lock_stays_below_the_caps_and_the_overrides_carry_them():
    locked = _locked()
    overrides = {}
    for line in (REPO / 'docker' / 'lock-overrides.txt').read_text().splitlines():
        line = line.split('#')[0].strip()
        if line and not line.startswith('-'):
            requirement = Requirement(line)
            overrides[canonicalize_name(requirement.name)] = requirement
    for name, cap in CAPS.items():
        assert locked[name] < cap, (name, locked[name])
        if name != 'anndata':                      # anndata: the headers' own cap is the only spec
            assert name in overrides and _below(overrides[name], cap), name
    assert overrides['numpy'].specifier.contains(locked['numpy'])
    for line in (REPO / 'docker' / 'requirements-app.in').read_text().splitlines():
        requirement = Requirement(line.split('#')[0].strip()) if line.split('#')[0].strip() else None
        if requirement is not None and canonicalize_name(requirement.name) in CAPS:
            assert _below(requirement, CAPS[canonicalize_name(requirement.name)]), line


def test_make_test_pins_duckdb_below_the_cap():
    test_deps = next(line for line in (REPO / 'Makefile').read_text().splitlines() if line.startswith('TEST_DEPS'))
    spec = re.search(r"--with '?(duckdb[^' ]*)", test_deps).group(1)
    assert _below(Requirement(spec), CAPS['duckdb'])


@pytest.mark.parametrize('spec, cap, expected', [
    ('duckdb>=1.3,<1.6', '1.6', True), ('duckdb>=0.10', '1.6', False), ('duckdb', '1.6', False),
    ('pandas>=2,<3.0', '3.0', True), ('h5py<5', '4', False), ('anndata==0.12.19', '0.13', True),
])
def test_the_cap_rule_itself(spec, cap, expected):
    assert _below(Requirement(spec), Version(cap)) is expected
