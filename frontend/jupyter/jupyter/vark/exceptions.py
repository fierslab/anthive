"""Exceptions raised by :class:`vark.AnthiveClient`.

Every class here subclasses :class:`requests.HTTPError`, so code that
already catches ``requests.HTTPError`` keeps working. The subclasses add
the decoded JSON body and a few named fields for the paper-upload
answers that a caller is expected to handle (403 forbidden, 503 uploads
switched off, 409 duplicate, 422 invalid, 413 too large, 415 not a PDF).
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
