"""Tests for lib/sidecar_merge."""
from __future__ import annotations

from sidecar_merge import (
    Conflict,
    merge_sidecars,
    parse_lines,
    restamp_s3_block,
)


# ─────────────────────────────────────────────────────────────────────────
# parse_lines
# ─────────────────────────────────────────────────────────────────────────

def test_parse_lines_separates_pair_comment_blank():
    text = "# top\norganism=mouse\n\nrubbish-line\n"
    rows = parse_lines(text)
    kinds = [row[0] for row in rows]
    assert kinds == ['comment', 'pair', 'blank', 'comment']
    assert rows[1][1] == 'organism'
    assert rows[1][2] == 'mouse'


def test_parse_lines_preserves_value_whitespace():
    # The raw_value is kept verbatim so round-trips don't churn whitespace.
    rows = parse_lines("organism=  mouse \n")
    kind, key, raw = rows[0]
    assert kind == 'pair'
    assert key == 'organism'
    assert raw == '  mouse '


# ─────────────────────────────────────────────────────────────────────────
# merge_sidecars — adopt / no-op / conflict / machine-key overwrite
# ─────────────────────────────────────────────────────────────────────────

def test_merge_adopts_new_incoming_keys():
    local = "organism=mouse\n"
    incoming = "organism=mouse\ntissue=brain\n"
    result = merge_sidecars(local, incoming)
    assert 'tissue' in result.adopted
    assert not result.conflicts
    assert 'tissue=brain' in result.merged_text


def test_merge_silent_when_values_match():
    local = "organism=mouse\ntissue=brain\n"
    incoming = "organism=mouse\ntissue=brain\n"
    result = merge_sidecars(local, incoming)
    assert not result.conflicts
    assert not result.adopted
    assert not result.overwritten


def test_merge_reports_conflict_keeps_local():
    local = "organism=mouse\n"
    incoming = "organism=human\n"
    result = merge_sidecars(local, incoming)
    assert result.conflicts == [
        Conflict(key='organism', local_value='mouse', incoming_value='human')
    ]
    # Local value wins.
    assert 'organism=mouse' in result.merged_text
    assert 'organism=human' not in result.merged_text


def test_merge_s3_keys_overwrite_silently():
    local = "s3_key=__incoming/Foo/bar.h5ad\ns3_etag=old\norganism=mouse\n"
    incoming = "s3_key=Foo/bar.h5ad\ns3_etag=new\norganism=mouse\n"
    result = merge_sidecars(local, incoming)
    assert not result.conflicts
    assert set(result.overwritten) == {'s3_key', 's3_etag'}
    assert 's3_key=Foo/bar.h5ad' in result.merged_text
    assert 's3_etag=new' in result.merged_text


def test_merge_irods_keys_overwrite_silently():
    local = "irods_uploaded_at=2020-01-01T00:00:00Z\n"
    incoming = "irods_uploaded_at=2026-06-13T08:14:22Z\n"
    result = merge_sidecars(local, incoming)
    assert not result.conflicts
    assert 'irods_uploaded_at' in result.overwritten


def test_merge_preserves_local_comments_and_order():
    local = "# header comment\n# second line\norganism=mouse\n\n# block\ntissue=brain\n"
    incoming = "organism=mouse\ntissue=brain\ndisease=Alzheimer disease\n"
    result = merge_sidecars(local, incoming)
    text = result.merged_text
    assert '# header comment' in text
    assert '# second line' in text
    assert '# block' in text
    # Order of local-existing keys preserved.
    assert text.index('organism=') < text.index('tissue=')
    # Adopted key appended at end.
    assert text.index('disease=') > text.index('tissue=')


def test_merge_local_only_keys_kept():
    local = "organism=mouse\nlocal_only_key=foo\n"
    incoming = "organism=mouse\n"
    result = merge_sidecars(local, incoming)
    assert 'local_only_key=foo' in result.merged_text


def test_merge_value_whitespace_ignored_for_compare_but_kept_in_output():
    local = "organism=  mouse  \n"
    incoming = "organism=mouse\n"
    result = merge_sidecars(local, incoming)
    assert not result.conflicts
    # Local raw value preserved verbatim.
    assert 'organism=  mouse  ' in result.merged_text


def test_merge_empty_local():
    # First-ingest case: local sidecar absent.
    result = merge_sidecars('', 'organism=mouse\ntissue=brain\n')
    assert sorted(result.adopted) == ['organism', 'tissue']
    assert 'organism=mouse' in result.merged_text
    assert 'tissue=brain' in result.merged_text


def test_merge_text_terminates_with_newline():
    result = merge_sidecars('organism=mouse', 'organism=mouse\ntissue=brain')
    assert result.merged_text.endswith('\n')


# ─────────────────────────────────────────────────────────────────────────
# restamp_s3_block
# ─────────────────────────────────────────────────────────────────────────

def test_restamp_s3_block_updates_existing_keys():
    text = (
        "organism=mouse\n"
        "s3_bucket=anthive4a\n"
        "s3_key=__incoming/Preman2024/Microglia.h5ad\n"
        "s3_url=https://example.com/__incoming/Preman2024/Microglia.h5ad\n"
        "s3_etag=old\n"
        "s3_uploaded_at=2026-06-13T08:00:00Z\n"
    )
    updated = restamp_s3_block(
        text,
        bucket='anthive4a',
        key='Preman2024/Microglia.h5ad',
        url='https://example.com/Preman2024/Microglia.h5ad',
        etag='new',
        uploaded_at='2026-06-13T09:00:00Z',
    )
    assert 's3_key=Preman2024/Microglia.h5ad' in updated
    assert '__incoming' not in updated
    assert 's3_etag=new' in updated
    assert 's3_uploaded_at=2026-06-13T09:00:00Z' in updated
    # Non-s3 keys untouched.
    assert 'organism=mouse' in updated


def test_restamp_s3_block_inserts_missing_keys():
    text = "organism=mouse\n"
    updated = restamp_s3_block(
        text,
        bucket='anthive4a',
        key='Preman2024/Microglia.h5ad',
        url='https://example.com/x',
        etag='abc',
        uploaded_at='2026-06-13T09:00:00Z',
    )
    for needed in ('s3_bucket', 's3_key', 's3_url', 's3_etag', 's3_uploaded_at'):
        assert f'{needed}=' in updated
    assert 'organism=mouse' in updated
