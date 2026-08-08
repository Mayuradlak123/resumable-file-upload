"""Resumable, chunked file uploads for Python backends.

    from resumable_upload import UploadManager
    from resumable_upload.storage import LocalStorage

    manager = UploadManager(storage=LocalStorage("./uploads"))

The package is framework-agnostic: nothing in it knows about HTTP.
"""

from .core.checksum import sha256_bytes, sha256_stream, verify_checksum
from .core.exceptions import (
    ChecksumMismatchError,
    IncompleteUploadError,
    InvalidChunkError,
    InvalidUploadStateError,
    ResumableUploadError,
    StorageError,
    UploadAlreadyExistsError,
    UploadNotFoundError,
)
from .core.manager import DEFAULT_CHUNK_SIZE, MAX_CHUNK_SIZE, UploadManager
from .core.models import Upload, UploadStatus
from .repository.base import MetadataRepository
from .repository.local import LocalMetadataRepository
from .storage.base import FinalizeResult, Storage
from .storage.local import LocalStorage

__version__ = "0.1.0"

__all__ = [
    "UploadManager",
    "Upload",
    "UploadStatus",
    "Storage",
    "LocalStorage",
    "FinalizeResult",
    "MetadataRepository",
    "LocalMetadataRepository",
    "DEFAULT_CHUNK_SIZE",
    "MAX_CHUNK_SIZE",
    "sha256_bytes",
    "sha256_stream",
    "verify_checksum",
    "ResumableUploadError",
    "UploadNotFoundError",
    "UploadAlreadyExistsError",
    "InvalidUploadStateError",
    "InvalidChunkError",
    "ChecksumMismatchError",
    "IncompleteUploadError",
    "StorageError",
    "__version__",
]
