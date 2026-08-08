"""Exception hierarchy for the resumable upload package."""

from __future__ import annotations

__all__ = [
    "ResumableUploadError",
    "UploadNotFoundError",
    "UploadAlreadyExistsError",
    "InvalidUploadStateError",
    "InvalidChunkError",
    "ChecksumMismatchError",
    "IncompleteUploadError",
    "StorageError",
]


class ResumableUploadError(Exception):
    """Base class for every error raised by this package."""


class UploadNotFoundError(ResumableUploadError):
    """No upload session exists for the given id."""

    def __init__(self, upload_id: str) -> None:
        self.upload_id = upload_id
        super().__init__(f"Upload {upload_id!r} was not found")


class UploadAlreadyExistsError(ResumableUploadError):
    """An upload session with the requested id already exists."""

    def __init__(self, upload_id: str) -> None:
        self.upload_id = upload_id
        super().__init__(f"Upload {upload_id!r} already exists")


class InvalidUploadStateError(ResumableUploadError):
    """The operation is not allowed while the upload is in its current status."""

    def __init__(self, upload_id: str, status: str, expected: str) -> None:
        self.upload_id = upload_id
        self.status = status
        self.expected = expected
        super().__init__(
            f"Upload {upload_id!r} is {status}; expected one of: {expected}"
        )


class InvalidChunkError(ResumableUploadError):
    """The chunk number or chunk size does not match the upload session."""


class ChecksumMismatchError(ResumableUploadError):
    """The received data does not match the checksum supplied by the client."""

    def __init__(self, expected: str, actual: str, chunk_number: int | None = None) -> None:
        self.expected = expected
        self.actual = actual
        self.chunk_number = chunk_number
        where = f" for chunk {chunk_number}" if chunk_number is not None else ""
        super().__init__(
            f"Checksum mismatch{where}: expected {expected}, calculated {actual}"
        )


class IncompleteUploadError(ResumableUploadError):
    """Completion was requested while chunks are still missing."""

    def __init__(self, upload_id: str, missing_chunks: list[int]) -> None:
        self.upload_id = upload_id
        self.missing_chunks = missing_chunks
        preview = missing_chunks[:10]
        suffix = "..." if len(missing_chunks) > len(preview) else ""
        super().__init__(
            f"Upload {upload_id!r} is missing {len(missing_chunks)} chunk(s): "
            f"{preview}{suffix}"
        )


class StorageError(ResumableUploadError):
    """The storage backend failed to complete an operation."""
