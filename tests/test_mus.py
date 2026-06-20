"""Tests for lib/mus.py — the .h5ad.mus + .env reader.

Mix of synthetic-fixture tests (covering edge cases) and real-data
sanity checks against the migrated h5adstore.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))

from mus import (  # noqa: E402
    _split_csv_quoted,
    _normalize,
    parse_mus_file,
    merge_cascade,
    read_cascade,
    sidecar_path,
    resolve_annotation,
    validate_value,
    validate_annotation,
    required_fields,
    soft_fields,
    is_controlled,
    allowed_values,
    load_vocabulary,
)


# ─── csv-quoted splitter ──────────────────────────────────────────────────

def test_csv_split_simple():
    assert _split_csv_quoted("a,b,c") == ["a", "b", "c"]


def test_csv_split_strips_whitespace():
    assert _split_csv_quoted(" a , b , c ") == ["a", "b", "c"]


def test_csv_split_quoted_with_comma():
    raw = 'Alzheimer disease,"leukoencephalopathy, diffuse hereditary, with spheroids 1"'
    assert _split_csv_quoted(raw) == [
        "Alzheimer disease",
        "leukoencephalopathy, diffuse hereditary, with spheroids 1",
    ]


def test_csv_split_empty_and_blank_items_dropped():
    assert _split_csv_quoted("a,,b, ,c") == ["a", "b", "c"]


def test_csv_split_single():
    assert _split_csv_quoted("only") == ["only"]


# ─── parse_mus_file ───────────────────────────────────────────────────────

def test_parse_basic_env(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "access=public\n"
        "organism=human\n"
        "year=2026\n"
        "# this is a comment\n"
        "\n"
        "contributor=Jane Doe\n"
    )
    out = parse_mus_file(p)
    assert out == {
        'access': 'public', 'organism': 'human',
        'year': '2026', 'contributor': 'Jane Doe',
    }


def test_parse_list_field_with_quotes(tmp_path):
    p = tmp_path / "x.h5ad.mus"
    p.write_text('disease=Alzheimer disease,"leukoencephalopathy, diffuse hereditary, with spheroids 1"\n')
    out = parse_mus_file(p)
    assert out['disease'] == [
        "Alzheimer disease",
        "leukoencephalopathy, diffuse hereditary, with spheroids 1",
    ]


def test_parse_tissue_multiple_no_quotes(tmp_path):
    p = tmp_path / "x.h5ad.mus"
    p.write_text("tissue=brain,cortex,white matter\n")
    out = parse_mus_file(p)
    assert out['tissue'] == ['brain', 'cortex', 'white matter']


def test_parse_python_list_quirk(tmp_path):
    """Migration tool sometimes wrote age=['6m', '12m']; accept gracefully."""
    p = tmp_path / "x.h5ad.mus"
    p.write_text("age=['6m', '12m']\n")
    out = parse_mus_file(p)
    assert out['age'] == ['6m', '12m']


def test_parse_missing_file(tmp_path):
    assert parse_mus_file(tmp_path / "nope") == {}


def test_parse_skips_lines_without_equals(tmp_path):
    p = tmp_path / ".env"
    p.write_text("garbage line\norganism=human\n")
    out = parse_mus_file(p)
    assert out == {'organism': 'human'}


def test_parse_single_value_quoted(tmp_path):
    p = tmp_path / ".env"
    p.write_text('title="multi word title"\n')
    out = parse_mus_file(p)
    assert out['title'] == "multi word title"


# ─── cascade merge ────────────────────────────────────────────────────────

def test_merge_cascade_scalar_replaces():
    layers = [
        {'organism': 'human', 'year': '2025'},
        {'year': '2026'},  # deeper layer overrides year
    ]
    assert merge_cascade(layers) == {'organism': 'human', 'year': '2026'}


def test_merge_cascade_list_accumulates():
    layers = [
        {'tissue': ['brain']},
        {'tissue': ['cortex', 'hippocampus']},
    ]
    out = merge_cascade(layers)
    assert out['tissue'] == ['brain', 'cortex', 'hippocampus']


def test_merge_cascade_list_dedupes():
    layers = [{'tissue': ['brain']}, {'tissue': ['brain', 'cortex']}]
    assert merge_cascade(layers)['tissue'] == ['brain', 'cortex']


def test_merge_cascade_dash_prefix_removes():
    layers = [
        {'disease': ['Alzheimer disease', 'normal']},
        {'disease': ['-normal']},  # remove 'normal' from cascade
    ]
    assert merge_cascade(layers)['disease'] == 'Alzheimer disease'


def test_merge_cascade_scalar_to_single_list():
    """A single-element list reduces to a scalar after merge."""
    out = merge_cascade([{'tissue': ['brain']}])
    assert out['tissue'] == 'brain'


# ─── read_cascade (walks .env files) ──────────────────────────────────────

def test_read_cascade_two_levels(tmp_path):
    (tmp_path / ".env").write_text("organism=human\naccess=internal\n")
    sub = tmp_path / "study"
    sub.mkdir()
    (sub / ".env").write_text("access=public\nyear=2026\n")  # deeper overrides
    out = read_cascade(sub, root=tmp_path)
    assert out['organism'] == 'human'
    assert out['access'] == 'public'  # deeper layer won
    assert out['year'] == '2026'


def test_read_cascade_no_env_returns_empty(tmp_path):
    assert read_cascade(tmp_path, root=tmp_path) == {}


def test_read_cascade_stops_at_root(tmp_path):
    (tmp_path.parent / ".env").write_text("organism=should_not_appear\n")
    (tmp_path / ".env").write_text("organism=human\n")
    sub = tmp_path / "study"
    sub.mkdir()
    out = read_cascade(sub, root=tmp_path)
    assert out['organism'] == 'human'  # parent's .env not read


# ─── resolve_annotation (cascade + sidecar) ───────────────────────────────

def test_sidecar_path():
    assert sidecar_path(Path('/foo/bar/x.h5ad')) == Path('/foo/bar/x.h5ad.mus')


def test_resolve_sidecar_replaces_cascade_per_key(tmp_path):
    (tmp_path / ".env").write_text("organism=mouse\ntissue=brain\n")
    h5 = tmp_path / "data.h5ad"
    h5.touch()
    (tmp_path / "data.h5ad.mus").write_text(
        "organism=human\n"     # sidecar replaces
        "cell_type=microglial cell\n"  # sidecar adds
    )
    out = resolve_annotation(h5, root=tmp_path)
    assert out['organism'] == 'human'
    assert out['tissue'] == 'brain'  # from cascade, untouched
    assert out['cell_type'] == 'microglial cell'


def test_resolve_no_sidecar_returns_cascade(tmp_path):
    (tmp_path / ".env").write_text("organism=human\n")
    h5 = tmp_path / "data.h5ad"
    h5.touch()
    out = resolve_annotation(h5, root=tmp_path)
    assert out == {'organism': 'human'}


# ─── vocabulary ───────────────────────────────────────────────────────────

def test_vocabulary_loads():
    vocab = load_vocabulary()
    assert vocab['schema_version'] == 2


def test_required_fields_are_what_conventions_says():
    expected = {'organism', 'tissue', 'cell_type', 'technology',
                'suspension_type', 'access', 'source'}
    assert set(required_fields()) == expected


def test_soft_fields_include_disease_sex_year():
    soft = set(soft_fields())
    assert {'disease', 'sex', 'year'}.issubset(soft)


def test_normalize_matches_underscore_space_dash():
    assert _normalize("Prefrontal_Cortex") == _normalize("prefrontal cortex")
    assert _normalize("ALZHEIMER-DISEASE") == _normalize("alzheimer disease")


def test_validate_value_controlled_field_matches_case_insensitive():
    assert validate_value('organism', 'human')
    assert validate_value('organism', 'Human')
    assert validate_value('organism', 'HUMAN')
    assert not validate_value('organism', 'kangaroo')


def test_validate_value_free_field_always_passes():
    assert validate_value('accession', 'GSE12345')
    assert validate_value('doi', 'whatever')


def test_validate_value_underscore_dash_space_equivalence():
    # cell_type has "microglial cell" (space)
    assert validate_value('cell_type', 'microglial cell')
    assert validate_value('cell_type', 'microglial-cell')
    assert validate_value('cell_type', 'microglial_cell')
    assert validate_value('cell_type', 'Microglial_Cell')


def test_is_controlled():
    assert is_controlled('organism')
    assert is_controlled('cell_type')
    assert not is_controlled('accession')  # freeform
    assert not is_controlled('doi')


def test_allowed_values_for_controlled():
    av = allowed_values('access')
    assert 'internal' in av and 'public' in av


def test_validate_annotation_clean():
    ann = {
        'organism': 'human', 'tissue': 'brain', 'cell_type': 'microglial cell',
        'technology': '10x_chromium', 'suspension_type': 'cell',
        'access': 'public', 'source': 'internal',
        'disease': 'Alzheimer disease', 'sex': 'mixed', 'year': '2026',
    }
    out = validate_annotation(ann)
    assert out['missing_required'] == []
    assert out['invalid_controlled'] == []


def test_validate_annotation_flags_missing_required():
    ann = {'organism': 'human'}
    out = validate_annotation(ann)
    assert 'tissue' in out['missing_required']
    assert 'cell_type' in out['missing_required']
    assert 'technology' in out['missing_required']


def test_validate_annotation_flags_bad_controlled():
    ann = {
        'organism': 'human', 'tissue': 'brain', 'cell_type': 'unicorn cell',
        'technology': '10x_chromium', 'suspension_type': 'cell',
        'access': 'public', 'source': 'internal',
    }
    out = validate_annotation(ann)
    assert any("'unicorn cell'" in s for s in out['invalid_controlled'])


# ─── against real migrated data ───────────────────────────────────────────

H5ADSTORE = REPO_ROOT / 'data/h5adstore'


@pytest.mark.skipif(not H5ADSTORE.exists(), reason='no local h5adstore')
def test_real_mancuso_2024():
    h5 = H5ADSTORE / 'Mancuso2024' / 'Mancuso_2023.h5ad'
    ann = resolve_annotation(h5, root=H5ADSTORE)
    assert ann.get('organism') == 'human'
    assert ann.get('cell_type') == 'microglial cell'
    assert ann.get('technology') == '10x_chromium'
    assert ann.get('suspension_type') == 'cell'
    assert ann.get('access') == 'public'  # sidecar overrides cascade default
    assert 'brain' in (ann.get('tissue') if isinstance(ann.get('tissue'), list)
                       else [ann.get('tissue')])


@pytest.mark.skipif(not H5ADSTORE.exists(), reason='no local h5adstore')
def test_real_pan2024_microglia_multi_disease():
    """Pan2024 Microglia has a quoted multi-comma MONDO label in disease."""
    h5 = H5ADSTORE / 'Pan2024' / 'Microglia.h5ad'
    ann = resolve_annotation(h5, root=H5ADSTORE)
    diseases = ann.get('disease', [])
    if isinstance(diseases, str):
        diseases = [diseases]
    assert 'Alzheimer disease' in diseases
    # The quoted MONDO label must come through intact (with its commas)
    assert any('leukoencephalopathy' in d and ',' in d for d in diseases), diseases


@pytest.mark.skipif(not H5ADSTORE.exists(), reason='no local h5adstore')
def test_real_required_fields_present():
    """Migrated datasets should have all required vocabulary fields filled."""
    for study, stem in [
        ('Mancuso2024', 'Mancuso_2023'),
        ('Pan2024', 'Microglia'),
        ('Olah2020', 'olah2020_microglia'),
        ('Simpson2025', 'seurat_obj_sorl1_final'),
    ]:
        h5 = H5ADSTORE / study / f'{stem}.h5ad'
        if not h5.exists():
            continue
        ann = resolve_annotation(h5, root=H5ADSTORE)
        result = validate_annotation(ann)
        assert result['missing_required'] == [], (
            f'{study}/{stem}: missing required {result["missing_required"]}'
        )


@pytest.mark.skipif(not H5ADSTORE.exists(), reason='no local h5adstore')
def test_real_vocabulary_mismatches_surface_cleanly():
    """Surface (not assert) any vocabulary mismatches in real data — those
    are vocab-gap or data-cleanup tickets, not parser bugs."""
    seen_invalid: list[str] = []
    for md in sorted(H5ADSTORE.rglob('*.md')):
        parts = md.relative_to(H5ADSTORE).parts
        if any(p.startswith('_') for p in parts) or md.name == 'index.md':
            continue
        h5 = md.with_suffix('.h5ad')
        if not h5.exists():
            continue
        ann = resolve_annotation(h5, root=H5ADSTORE)
        result = validate_annotation(ann)
        for msg in result['invalid_controlled']:
            seen_invalid.append(f'{md.relative_to(H5ADSTORE)}: {msg}')
    # Just print; don't fail the suite — vocab gaps belong upstream.
    if seen_invalid:
        print('\n--- vocabulary mismatches across h5adstore ---')
        for line in seen_invalid:
            print(f'  {line}')
