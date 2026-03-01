"""
File checksum utilities for anthive
"""

import hashlib
from pathlib import Path


def calculate_sha256(file_path: Path, chunk_size: int = 8192) -> str:
    """
    Calculate SHA256 checksum of a file

    Processes file in chunks to handle large files efficiently.

    Args:
        file_path: Path to file to checksum
        chunk_size: Size of chunks to read (default 8KB)

    Returns:
        Hexadecimal SHA256 checksum string

    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read
    """
    sha256_hash = hashlib.sha256()

    with open(file_path, "rb") as file:
        # Read file in chunks to handle large files
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            sha256_hash.update(chunk)

    return sha256_hash.hexdigest()


def verify_checksum(file_path: Path, expected_checksum: str) -> bool:
    """
    Verify file matches expected checksum

    Args:
        file_path: Path to file to verify
        expected_checksum: Expected SHA256 checksum (hex string)

    Returns:
        True if checksums match, False otherwise
    """
    actual_checksum = calculate_sha256(file_path)
    return actual_checksum.lower() == expected_checksum.lower()


def format_checksum(checksum: str, short: bool = False) -> str:
    """
    Format checksum for display

    Args:
        checksum: SHA256 checksum hex string
        short: If True, return abbreviated version (first 8 chars)

    Returns:
        Formatted checksum string
    """
    if short:
        return checksum[:8]
    return checksum
