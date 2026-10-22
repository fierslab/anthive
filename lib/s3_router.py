"""One S3 client surface over two key pairs: the ingestion bucket's and the catalog's (design section 24.9).

A central watcher may hold separate credentials for the ingestion bucket (``ingest_watch.incoming_s3``: read
the sites' staging, write and delete ``__incoming/``) and for the data bucket (the catalog). The watcher's code
calls one client with an explicit ``Bucket``; :class:`BucketRouter` sends every call to the client of that
bucket. A copy between buckets of different clients cannot be a server-side copy (neither key pair reaches both
buckets), so it is streamed (:func:`streamed_copy`): a GET pinned to the source ETag, a single PUT or a multipart
upload to the target in parts of ``PART_BYTES``, the sha256 computed on the way and compared with the target's
``sha256`` metadata before the upload is completed (aborted otherwise), then a HEAD of the target. With one key
pair (or ``copy_mode: server``) copies stay server-side.

Input: the boto3-like clients. Output: the same calls, routed; copies between key pairs streamed.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterator, Mapping, Optional

PART_BYTES = 64 * 1024 * 1024
MAX_PARTS = 9000                 # S3 allows 10,000 parts; the part size grows to stay below this
READ_BYTES = 8 * 1024 * 1024
#: calls without a Bucket that the router passes to the data bucket's client
NO_BUCKET_CALLS = ('generate_presigned_url', 'can_paginate', 'close')
COPY_MODES = ('auto', 'server', 'stream')


class CopyError(RuntimeError):
    """A streamed copy that did not produce the expected bytes (nothing is left at the target)."""


def _code(exc: BaseException) -> str:
    response = getattr(exc, 'response', None)
    if isinstance(response, dict):
        return str((response.get('Error') or {}).get('Code') or '')
    return str(getattr(exc, 'code', '') or exc)


def _chunks(body: Any, size: int) -> Iterator[bytes]:
    while True:
        chunk = body.read(size)
        if not chunk:
            return
        yield chunk


def streamed_copy(source_client: Any, source_bucket: str, source_key: str, target_client: Any, target_bucket: str,
                  target_key: str, metadata: Optional[Mapping[str, str]] = None, if_match: Optional[str] = None,
                  content_type: Optional[str] = None, content_disposition: Optional[str] = None,
                  part_bytes: int = PART_BYTES) -> Dict[str, Any]:
    """Copy one object between two clients through this host. ``metadata`` None keeps the source's; a
    ``sha256`` in the target metadata must equal the sha256 of the streamed bytes. Returns the target's HEAD."""
    head = source_client.head_object(Bucket=source_bucket, Key=source_key)
    size = int(head.get('ContentLength') or 0)
    get_args: Dict[str, Any] = {'Bucket': source_bucket, 'Key': source_key}
    if if_match:
        get_args['IfMatch'] = if_match
    response = source_client.get_object(**get_args)
    meta = dict(metadata if metadata is not None else (response.get('Metadata') or head.get('Metadata') or {}))
    expected = meta.get('sha256')
    extra: Dict[str, Any] = {'Metadata': meta}
    if content_type:
        extra['ContentType'] = content_type
    if content_disposition:
        extra['ContentDisposition'] = content_disposition
    part = max(part_bytes, -(-size // MAX_PARTS))
    digest = hashlib.sha256()
    body = response['Body']
    if size <= part:
        buffer = bytearray()
        for chunk in _chunks(body, min(READ_BYTES, max(size, 1))):   # bounded by the HEAD size, in chunks
            buffer += chunk
            if len(buffer) > size:
                raise CopyError(f'{source_key}: the source is longer than its HEAD says; not copied')
        if len(buffer) != size:
            raise CopyError(f'{source_key}: the source is shorter than its HEAD says; not copied')
        digest.update(buffer)
        if expected and digest.hexdigest() != expected:
            raise CopyError(f'{source_key}: the bytes read are not the expected sha256; not copied')
        target_client.put_object(Bucket=target_bucket, Key=target_key, Body=bytes(buffer), **extra)
    else:
        upload = target_client.create_multipart_upload(Bucket=target_bucket, Key=target_key, **extra)
        upload_id = upload['UploadId']
        parts = []
        try:
            read = 0
            for number, chunk in enumerate(_chunks(body, part), start=1):
                read += len(chunk)
                if read > size or number > MAX_PARTS + 1:
                    raise CopyError(f'{source_key}: the source is longer than its HEAD says; not copied')
                digest.update(chunk)
                answer = target_client.upload_part(Bucket=target_bucket, Key=target_key, UploadId=upload_id,
                                                   PartNumber=number, Body=chunk)
                parts.append({'PartNumber': number, 'ETag': answer['ETag']})
            if read != size:
                raise CopyError(f'{source_key}: the source is shorter than its HEAD says; not copied')
            if expected and digest.hexdigest() != expected:
                raise CopyError(f'{source_key}: the bytes read are not the expected sha256; not copied')
            target_client.complete_multipart_upload(Bucket=target_bucket, Key=target_key, UploadId=upload_id,
                                                    MultipartUpload={'Parts': parts})
        except BaseException:
            try:
                target_client.abort_multipart_upload(Bucket=target_bucket, Key=target_key, UploadId=upload_id)
            except Exception:  # noqa: BLE001 - the bucket's lifecycle rule removes it otherwise
                pass
            raise
    target = target_client.head_object(Bucket=target_bucket, Key=target_key)
    if int(target.get('ContentLength') or -1) != size:
        raise CopyError(f'{target_key}: the copy has another size than {source_key}')
    return target


class _RoutedPaginator:
    def __init__(self, router: 'BucketRouter', name: str):
        self.router, self.name = router, name

    def paginate(self, **kwargs: Any) -> Any:
        return self.router.client_for(kwargs.get('Bucket')).get_paginator(self.name).paginate(**kwargs)


class BucketRouter:
    """Send each call to the client of its bucket (``clients``: bucket -> client; ``default`` for the others).

    ``copy_mode``: ``auto`` streams a copy between two clients and keeps a copy within one client server-side;
    ``server`` always asks the target's client for a server-side copy (its keys must read the source);
    ``stream`` streams every copy between different buckets."""

    def __init__(self, default: Any, clients: Optional[Mapping[str, Any]] = None, copy_mode: str = 'auto'):
        if copy_mode not in COPY_MODES:
            raise ValueError(f'copy_mode is one of {", ".join(COPY_MODES)}, not {copy_mode!r}')
        self.default = default
        self.clients = dict(clients or {})
        self.copy_mode = copy_mode

    def client_for(self, bucket: Optional[str]) -> Any:
        return self.clients.get(str(bucket), self.default) if bucket is not None else self.default

    def _stream(self, source_bucket: str, target_bucket: str) -> bool:
        if source_bucket == target_bucket or self.copy_mode == 'server':
            return False
        return self.copy_mode == 'stream' or self.client_for(source_bucket) is not self.client_for(target_bucket)

    # routed calls -------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.default, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            bucket = kwargs.get('Bucket')
            if bucket is None and name not in NO_BUCKET_CALLS:
                # never guess the key pair: a call without an explicit Bucket would go to the data keys
                raise TypeError(f'BucketRouter.{name} needs an explicit Bucket= keyword')
            return getattr(self.client_for(bucket), name)(*args, **kwargs)
        return call

    def get_paginator(self, name: str) -> _RoutedPaginator:
        return _RoutedPaginator(self, name)

    def upload_file(self, Filename: str, Bucket: str, Key: str, **kwargs: Any) -> Any:  # noqa: N803
        return self.client_for(Bucket).upload_file(Filename, Bucket, Key, **kwargs)

    def download_file(self, Bucket: str, Key: str, Filename: str, **kwargs: Any) -> Any:  # noqa: N803
        return self.client_for(Bucket).download_file(Bucket, Key, Filename, **kwargs)

    def copy_object(self, Bucket: str, Key: str, CopySource: Mapping[str, str], Metadata: Optional[dict] = None,  # noqa: N803
                    MetadataDirective: str = 'COPY', CopySourceIfMatch: Optional[str] = None,  # noqa: N803
                    ContentType: Optional[str] = None, ContentDisposition: Optional[str] = None,  # noqa: N803
                    **kwargs: Any) -> Dict[str, Any]:
        source_bucket = CopySource['Bucket']
        if not self._stream(source_bucket, Bucket):
            args: Dict[str, Any] = dict(Bucket=Bucket, Key=Key, CopySource=dict(CopySource),
                                        MetadataDirective=MetadataDirective, **kwargs)
            for name, value in (('Metadata', Metadata), ('CopySourceIfMatch', CopySourceIfMatch),
                                ('ContentType', ContentType), ('ContentDisposition', ContentDisposition)):
                if value is not None:
                    args[name] = value
            return self.client_for(Bucket).copy_object(**args)
        target = streamed_copy(self.client_for(source_bucket), source_bucket, CopySource['Key'],
                               self.client_for(Bucket), Bucket, Key,
                               metadata=Metadata if MetadataDirective == 'REPLACE' else None,
                               if_match=CopySourceIfMatch, content_type=ContentType,
                               content_disposition=ContentDisposition)
        return {'CopyObjectResult': {'ETag': target.get('ETag')}}

    def copy(self, CopySource: Mapping[str, str], Bucket: str, Key: str, ExtraArgs: Optional[dict] = None,  # noqa: N803
             **kwargs: Any) -> Any:
        extra = dict(ExtraArgs or {})
        source_bucket = CopySource['Bucket']
        if not self._stream(source_bucket, Bucket):
            return self.client_for(Bucket).copy(dict(CopySource), Bucket, Key, ExtraArgs=extra or None, **kwargs)
        streamed_copy(self.client_for(source_bucket), source_bucket, CopySource['Key'], self.client_for(Bucket),
                      Bucket, Key, metadata=extra.get('Metadata') if extra.get('MetadataDirective') == 'REPLACE'
                      or 'Metadata' in extra else None, if_match=extra.get('CopySourceIfMatch'),
                      content_type=extra.get('ContentType'), content_disposition=extra.get('ContentDisposition'))
        return None
