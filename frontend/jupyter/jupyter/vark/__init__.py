"""pyvark — Python client for the Anthive single-cell REST API.

This is the **Python** sibling of the Go `vark` CLI (codeberg.org/mfiers/vark).
Distributed on Codeberg as the `pyvark` repo to disambiguate from the Go
binary; the importable package name inside is just `vark`.

    pip install pyvark         # PyPI / Codeberg distribution name
    from vark import AnthiveClient   # importable name

The Go CLI and this Python client both talk to the same anthive REST API
(`bin/api/ant-serve` in anthive4). When the API version changes, both move
forward together — `vark.AnthiveClient.API_TARGET` records the API version
this release was last verified against.

Quick start::

    from vark import AnthiveClient
    # credentials from auth=, ANTHIVE_USER/ANTHIVE_PASSWORD, the vark CLI
    # store or ~/.netrc
    c = AnthiveClient("https://my.anthive/api")
    print(c.get_version()["version"])

For the legacy `find_database` / `find_metadata` helpers from antclient,
use ``from vark.helpers import find_database, find_metadata``.
"""

from .client import AnthiveClient
from ._credentials import VarkConfigError
from .exceptions import (
    AnthiveAPIError,
    AnthiveWarmingUpError,
    ArtefactDownloadError,
    ArtefactError,
    ArtefactFileNotFoundError,
    ArtefactIntegrityError,
    ArtefactNotFoundError,
    ArtefactNotOnThisSiteError,
    ArtefactPurgedError,
    ArtefactPurgingError,
    ArtefactRateLimitedError,
    ArtefactStorageUnavailableError,
    DatasetDepositError,
    DepositHeldError,
    DepositNotOwnerError,
    DepositQuotaError,
    DepositStoppedError,
    PaperDuplicateError,
    PaperInvalidError,
    PaperNotPDFError,
    PaperTooLargeError,
    PaperUploadDisabledError,
    PaperUploadError,
    PaperUploadForbiddenError,
    ServerWarmingUpWarning,
)

# Backward-compatibility alias (matches antclient 1.x).
AntClient = AnthiveClient

__version__ = "0.12.0"
__all__ = [
    "AnthiveClient", "AntClient", "VarkConfigError", "__version__",
    "AnthiveAPIError", "AnthiveWarmingUpError", "ServerWarmingUpWarning",
    "DatasetDepositError", "DepositNotOwnerError", "DepositQuotaError",
    "DepositHeldError", "DepositStoppedError",
    "ArtefactError", "ArtefactNotFoundError", "ArtefactFileNotFoundError", "ArtefactNotOnThisSiteError",
    "ArtefactPurgedError", "ArtefactPurgingError", "ArtefactIntegrityError",
    "ArtefactStorageUnavailableError", "ArtefactRateLimitedError",
    "ArtefactDownloadError",
    "PaperUploadError", "PaperUploadForbiddenError",
    "PaperUploadDisabledError", "PaperDuplicateError", "PaperInvalidError",
    "PaperTooLargeError", "PaperNotPDFError",
]
