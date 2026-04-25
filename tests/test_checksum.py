"""Tests for lib/checksum.py — SHA256 file hashing."""

import hashlib

import pytest

from checksum import calculate_sha256, format_checksum, verify_checksum


class TestCalculateSha256:

    def test_known_content(self, tmp_path):
        f = tmp_path / "test.bin"
        content = b"hello world"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert calculate_sha256(f) == expected

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert calculate_sha256(f) == expected

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            calculate_sha256(tmp_path / "nope.bin")


class TestVerifyChecksum:

    def test_match(self, tmp_path):
        f = tmp_path / "test.bin"
        content = b"data"
        f.write_bytes(content)
        checksum = hashlib.sha256(content).hexdigest()
        assert verify_checksum(f, checksum) is True

    def test_mismatch(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"data")
        assert verify_checksum(f, "0" * 64) is False

    def test_case_insensitive(self, tmp_path):
        f = tmp_path / "test.bin"
        content = b"test"
        f.write_bytes(content)
        checksum = hashlib.sha256(content).hexdigest().upper()
        assert verify_checksum(f, checksum) is True


class TestFormatChecksum:

    def test_full(self):
        h = "a" * 64
        assert format_checksum(h) == h
        assert len(format_checksum(h)) == 64

    def test_short(self):
        h = "abcdef0123456789" + "0" * 48
        assert format_checksum(h, short=True) == "abcdef01"
        assert len(format_checksum(h, short=True)) == 8
