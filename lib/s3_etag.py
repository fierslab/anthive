"""File hashes and S3 ETags (standard library only: used by ant-sync, the ingestion routes and the deposits).

``etag_matches_file`` proves that an S3 object holds exactly the bytes of a local file: a single-part ETag
is the MD5 of the content, a multipart ETag is ``md5(concat(part md5s))-<parts>`` for the part size the
uploader used (tried for the common ones).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(2 ** 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - compared with a single-part S3 ETag, not for security
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(2 ** 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


#: Part sizes tried when recomputing a multipart ETag: boto3's default (8 MB), common tool settings,
#: and ant-sync / ant-ingest's 256 MB (MULTIPART_CHUNKSIZE).
MULTIPART_PART_SIZES_MB = (5, 8, 16, 64, 256)
MAX_PARTS = 10000


def candidate_part_sizes(size: int) -> List[int]:
    """Part sizes an uploader may have used for an object of ``size`` bytes.

    The configured sizes (5 MiB: AWS Go SDK, rclone and mus; 8 MiB: boto3;
    16, 64, 256 MiB), each enlarged as boto3 does for more than 10,000 parts
    (s3transfer ``ChunksizeAdjuster``: doubled until the parts fit), plus
    ``ceil(size / 10000)`` rounded up to a whole MiB (the rule of uploaders that
    grow the part size to the minimum that fits).
    """
    mib = 1024 * 1024
    sizes = []
    for size_mb in MULTIPART_PART_SIZES_MB:
        part = size_mb * mib
        while -(-size // part) > MAX_PARTS:
            part *= 2
        sizes.append(part)
    sizes.append(-(-(-(-size // MAX_PARTS)) // mib) * mib)
    return sorted(set(sizes))


def multipart_etag(path: Path, part_size: int) -> str:
    """The S3 ETag of ``path`` uploaded in parts of ``part_size`` bytes: md5(concat(part md5s))-<parts>."""
    digests = []
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(part_size)
            if not chunk:
                break
            digests.append(hashlib.md5(chunk).digest())  # noqa: S324 - S3 ETag, not security
    if not digests:
        digests.append(hashlib.md5(b'').digest())  # noqa: S324
    return hashlib.md5(b''.join(digests)).hexdigest() + f'-{len(digests)}'  # noqa: S324


def etag_matches_file(etag: str, path: Path) -> bool:
    """True when ``etag`` is the single-part MD5 of ``path`` or its multipart ETag for a common part size."""
    etag = etag.strip('"')
    if '-' not in etag:
        return md5_file(path) == etag
    try:
        parts = int(etag.rsplit('-', 1)[1])
    except ValueError:
        return False
    size = path.stat().st_size
    for part_size in candidate_part_sizes(size):
        if -(-size // part_size) == parts and multipart_etag(path, part_size) == etag:
            return True
    return False


