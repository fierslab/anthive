"""Exceptions raised by :class:`vark.AnthiveClient`.

Every class here subclasses :class:`requests.HTTPError`, so code that
already catches ``requests.HTTPError`` keeps working. The subclasses add
the decoded JSON body and a few named fields for the paper-upload
answers that a caller is expected to handle (403 forbidden, 503 uploads
switched off, 409 duplicate, 422 invalid, 413 too large, 415 not a PDF).

The artefact store (REST API 2.47.0+) has its own family under
:class:`ArtefactError`, and the deposit answers a caller is expected to
handle have subclasses of :class:`DatasetDepositError`
(:class:`DepositNotOwnerError`, :class:`DepositQuotaError`,
:class:`DepositHeldError`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests


class AnthiveAPIError(requests.HTTPError):
    """A non-2xx answer from ant-serve, with the decoded JSON body.

    Attributes:
        status_code: HTTP status of the answer.
        body: Decoded JSON body (``{}`` when the body was not JSON).
        message: Human-readable message taken from the body
            (``message`` or FastAPI's ``detail``), else the reason phrase.
    """

    def __init__(self, message: str, *, status_code: int,
                 body: Optional[Dict[str, Any]] = None,
                 response: Optional[requests.Response] = None):
        super().__init__(message, response=response)
        self.status_code = status_code
        self.body: Dict[str, Any] = body or {}
        self.message = message


class AnthiveWarmingUpError(AnthiveAPIError):
    """503 ``{"status": "warming_up", "retry_after": N}``: the server was
    restarted and is still building its facet caches (REST API 2.46.1+).

    :class:`vark.AnthiveClient` waits and retries on its own; this is
    raised only when the server is still warming up after
    ``warmup_max_wait`` seconds (or at once when that is 0).

    Attributes:
        retry_after: The server's last suggested wait, in seconds.
        waited: Seconds the client already waited.
    """

    def __init__(self, message: str, *, retry_after: Optional[float],
                 waited: float, body: Optional[Dict[str, Any]] = None,
                 response: Optional[requests.Response] = None):
        super().__init__(message, status_code=503, body=body,
                         response=response)
        self.retry_after = retry_after
        self.waited = waited


class DatasetDepositError(AnthiveAPIError):
    """A refused answer from a ``/datasets/deposit`` route (REST API
    2.46.0+). The server answers JSON ``{"status": "<word>", "message":
    ...}``; for example 403 ``forbidden`` (not a signed-in member of
    ``datasets_upload_groups``), 404 ``not_found`` (also for another
    user's deposit), 404 ``no_report``, 429 ``rate_limited``, 503
    ``disabled``.

    Attributes:
        status: The status word of the body (``''`` when absent).
    """

    def __init__(self, message: str, *, status_code: int,
                 body: Optional[Dict[str, Any]] = None,
                 response: Optional[requests.Response] = None):
        super().__init__(message, status_code=status_code, body=body,
                         response=response)
        self.status = str((body or {}).get('status') or '')


class DepositNotOwnerError(DatasetDepositError):
    """403 ``not_owner`` (REST API 2.47.0+): a new version of an
    artefact is deposited by its owner or an admin only, and a
    ``de_table`` (or ``de_only``) deposit needs the right to update its
    dataset (admin, update policy ``any``, or the dataset's owner)."""


class DepositQuotaError(DatasetDepositError):
    """413 ``quota_exceeded``: the deposit would pass a per-user limit
    (``artefacts_max_stored_gb_per_user``, ``artefacts_max_versions`` or
    the pending bytes of dataset deposits). The message says which."""


class DepositHeldError(DatasetDepositError):
    """A deposit stopped in state ``held``: it passed its checks but
    waits for an admin (for example a public scope, a possible secret,
    donor identifiers, ``made_from_internal``). Raised by
    :meth:`vark.AnthiveClient.wait_for_deposit`, not by a refused HTTP
    answer, so ``status_code`` is the 200 of the status request.

    Attributes:
        deposit: The deposit status dict of the last poll.
        held_reasons: The reasons the server gave (list of str).
    """

    def __init__(self, message: str, *, deposit: Dict[str, Any],
                 status_code: int = 200,
                 response: Optional[requests.Response] = None):
        super().__init__(message, status_code=status_code,
                         body={'status': 'held', 'message': message},
                         response=response)
        self.deposit: Dict[str, Any] = deposit
        self.held_reasons: List[str] = list(deposit.get('held_reasons') or [])


class DepositStoppedError(DatasetDepositError):
    """A deposit ended in a state other than ``live`` or ``held``:
    ``failed``, ``needs_decision``, ``rejected``, ``withdrawn``,
    ``expired`` or ``superseded``. Raised by
    :meth:`vark.AnthiveClient.wait_for_deposit`; ``status`` holds the
    state and ``status_code`` is the 200 of the status request.

    Attributes:
        deposit: The deposit status dict of the last poll.
    """

    def __init__(self, message: str, *, deposit: Dict[str, Any],
                 status_code: int = 200,
                 response: Optional[requests.Response] = None):
        super().__init__(message, status_code=status_code,
                         body={'status': str(deposit.get('state') or ''),
                               'message': message},
                         response=response)
        self.deposit: Dict[str, Any] = deposit


class ArtefactError(AnthiveAPIError):
    """A refused answer from an ``/artefacts`` route (REST API 2.47.0+).

    The server answers JSON ``{"status": "<word>", "message": ...}``,
    often with ``ref`` and ``file``. An artefact the caller may not see
    answers the same 404 ``not_found`` as one that does not exist.

    Attributes:
        status: The status word of the body (``''`` when absent), e.g.
            ``not_found``, ``invalid_kind``, ``invalid_cursor``.
        ref: The artefact reference the server named, if any.
        file: The file name the server named, if any.
        retry_after: Seconds from the ``Retry-After`` header (429, 503),
            else ``None``.
    """

    def __init__(self, message: str, *, status_code: int,
                 body: Optional[Dict[str, Any]] = None,
                 response: Optional[requests.Response] = None,
                 retry_after: Optional[float] = None):
        super().__init__(message, status_code=status_code, body=body,
                         response=response)
        payload = body or {}
        self.status = str(payload.get('status') or '')
        self.ref: Optional[str] = payload.get('ref')
        self.file: Optional[str] = payload.get('file')
        self.retry_after = retry_after


class ArtefactNotFoundError(ArtefactError):
    """404 ``not_found``: no such artefact, version or file, or one the
    caller may not see (the server does not tell them apart)."""


class ArtefactFileNotFoundError(ArtefactNotFoundError):
    """404 ``file_not_found``: the artefact version exists but has no
    file of that name (REST API 2.47.0; ``ref`` names the version and
    ``file`` the name). pyvark's download methods also raise it before
    any request when the manifest has no such file."""


class ArtefactNotOnThisSiteError(ArtefactError):
    """404 ``not_on_this_site``: the file exists but this site has
    neither S3 read access nor a verified local copy; fetch it from
    another site."""


class ArtefactPurgedError(ArtefactError):
    """410 ``purged``: the version was purged. ``tombstone`` holds
    ``{version, state, published, purged, purged_by, purge_reason}``
    when the server sent it (not when every version is purged and
    ``latest`` was asked)."""

    @property
    def tombstone(self) -> Optional[Dict[str, Any]]:
        return self.body.get('tombstone')


class ArtefactPurgingError(ArtefactError):
    """409 ``purging``: a purge of this version is running."""


class ArtefactIntegrityError(ArtefactError):
    """The bytes do not match the manifest.

    Raised for the server's 502 ``integrity_error`` (the stored object
    differs from the manifest; no link was signed) and by the client
    when a downloaded file's size or sha256 differs from the manifest
    (``status`` ``checksum_mismatch``; the partial file is removed).

    Attributes:
        expected_sha256: The manifest's sha256 (client-side checks).
        actual_sha256: The sha256 of the received bytes (client-side).
    """

    def __init__(self, message: str, *, status_code: int,
                 body: Optional[Dict[str, Any]] = None,
                 response: Optional[requests.Response] = None,
                 retry_after: Optional[float] = None,
                 expected_sha256: Optional[str] = None,
                 actual_sha256: Optional[str] = None):
        super().__init__(message, status_code=status_code, body=body,
                         response=response, retry_after=retry_after)
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256


class ArtefactStorageUnavailableError(ArtefactError):
    """503 ``storage_unavailable``: the object store did not answer and
    there is no local copy. Retry after ``retry_after`` seconds."""


class ArtefactRateLimitedError(ArtefactError):
    """429 ``rate_limited``: too many downloads per minute
    (``artefacts_download_per_minute``). Retry after ``retry_after``."""


class ArtefactDownloadError(ArtefactError):
    """The signed object store link of a download failed (for example
    403 after it expired). The signed URL is kept out of the message and
    the exception (it is a bearer credential); ask the API again for a
    fresh link."""


class ServerWarmingUpWarning(UserWarning):
    """Emitted (once per wait) when the client starts waiting for a
    server that answers 503 ``warming_up``."""


class PaperUploadError(AnthiveAPIError):
    """Base class for every refused paper upload (and for the 403 on the
    upload requirements)."""


class PaperUploadForbiddenError(PaperUploadError):
    """403: the caller is not in the site's ``library_upload_groups``.

    The server hides the upload features from everyone else, so this is
    also what a non-uploader gets from
    :meth:`AnthiveClient.get_paper_upload_requirements`.
    """


class PaperUploadDisabledError(PaperUploadError):
    """503 ``{"status": "disabled"}``: paper uploads are switched off on
    this site (the library is mounted read-only). Nothing was written."""


class PaperDuplicateError(PaperUploadError):
    """409: the paper is already in the library.

    Attributes:
        existing_citekey: Citekey of the record that matched.
        matched_on: ``doi``, ``pmid``, ``sha256`` or ``citekey``.
    """

    @property
    def existing_citekey(self) -> Optional[str]:
        return self.body.get('existing_citekey')

    @property
    def matched_on(self) -> Optional[str]:
        return self.body.get('matched_on')


class PaperInvalidError(PaperUploadError):
    """422: the record does not validate.

    Attributes:
        errors: List of ``{"field", "message"}`` dicts.
        missing_required: Required fields that were absent.
    """

    @property
    def errors(self) -> List[Dict[str, Any]]:
        return list(self.body.get('errors') or [])

    @property
    def missing_required(self) -> List[str]:
        return list(self.body.get('missing_required') or [])


class PaperTooLargeError(PaperUploadError):
    """413: the PDF is larger than ``library_upload_max_pdf_mb``."""


class PaperNotPDFError(PaperUploadError):
    """415: the uploaded file is not a PDF (checked by its magic bytes)."""


_UPLOAD_ERRORS_BY_STATUS = {
    403: PaperUploadForbiddenError,
    409: PaperDuplicateError,
    413: PaperTooLargeError,
    415: PaperNotPDFError,
    422: PaperInvalidError,
}


def _decode_body(response: requests.Response) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {'detail': body}


def _message_from(body: Dict[str, Any], response: requests.Response) -> str:
    message = body.get('message') or body.get('detail')
    if isinstance(message, (list, dict)):
        message = str(message)
    if not message:
        message = response.reason or 'request failed'
    return f"{response.status_code} {message} (url: {response.url})"


def raise_for_upload_status(response: requests.Response) -> None:
    """Raise the matching :class:`PaperUploadError` for a refused upload
    answer, or :class:`AnthiveAPIError` for any other non-2xx."""
    if response.ok:
        return
    body = _decode_body(response)
    message = _message_from(body, response)
    status = response.status_code
    if status == 503 and body.get('status') == 'disabled':
        error_class: type = PaperUploadDisabledError
    else:
        error_class = _UPLOAD_ERRORS_BY_STATUS.get(status, AnthiveAPIError)
    raise error_class(message, status_code=status, body=body, response=response)


def raise_for_deposit_status(response: requests.Response) -> None:
    """Raise :class:`DatasetDepositError` for a non-2xx answer of a
    dataset deposit route."""
    if response.ok:
        return
    body = _decode_body(response)
    error_class = _DEPOSIT_ERRORS_BY_WORD.get(str(body.get('status') or ''),
                                              DatasetDepositError)
    raise error_class(_message_from(body, response),
                      status_code=response.status_code, body=body,
                      response=response)


_DEPOSIT_ERRORS_BY_WORD = {
    'not_owner': DepositNotOwnerError,
    'quota_exceeded': DepositQuotaError,
}

_ARTEFACT_ERRORS_BY_WORD = {
    'not_found': ArtefactNotFoundError,
    'file_not_found': ArtefactFileNotFoundError,
    'not_on_this_site': ArtefactNotOnThisSiteError,
    'purged': ArtefactPurgedError,
    'purging': ArtefactPurgingError,
    'integrity_error': ArtefactIntegrityError,
    'storage_unavailable': ArtefactStorageUnavailableError,
    'rate_limited': ArtefactRateLimitedError,
}


def _retry_after(response: requests.Response,
                 body: Dict[str, Any]) -> Optional[float]:
    raw = response.headers.get('Retry-After', body.get('retry_after'))
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def raise_for_artefact_status(response: requests.Response) -> None:
    """Raise the matching :class:`ArtefactError` for a non-2xx answer
    of an ``/artefacts`` route (chosen by the body's status word, so a
    404 ``not_found`` and a 404 ``not_on_this_site`` differ)."""
    if response.ok:
        return
    body = _decode_body(response)
    word = str(body.get('status') or '')
    error_class = _ARTEFACT_ERRORS_BY_WORD.get(word, ArtefactError)
    raise error_class(_message_from(body, response),
                      status_code=response.status_code, body=body,
                      response=response,
                      retry_after=_retry_after(response, body))
