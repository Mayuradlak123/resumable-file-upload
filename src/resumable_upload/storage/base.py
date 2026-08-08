"""Storage abstraction.

``UploadManager`` only ever talks to this interface, so a future S3 / Azure /
GCS backend can be dropped in without touching the upload engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core.models import Upload

__all__ = ["Storage", "FinalizeResult"]


@dataclass(frozen=True)
class FinalizeResult:
    """Outcome of assembling the chunks into the final object."""

    location: str
    """Backend-specific locator: a filesystem path, an S3 key, a URI..."""

    size: int
    """Byte length of the assembled object."""

    checksum: str
    """SHA-256 of the assembled object, computed while it is written."""


class Storage(ABC):
    """Where chunk bytes and completed files live.

    Implementations must be safe to call concurrently for *different* uploads.
    Serialisation of concurrent operations on the *same* upload is handled by
    ``UploadManager``.
    """

    @abstractmethod
    async def write_chunk(self, upload_id: str, chunk_number: int, data: bytes) -> int:
        """Persist ``data`` as chunk ``chunk_number``, overwriting any previous
        copy atomically. Returns the number of bytes written."""

    @abstractmethod
    async def read_chunk(self, upload_id: str, chunk_number: int) -> bytes:
        """Return the bytes previously stored for ``chunk_number``."""

    @abstractmethod
    async def delete_chunk(self, upload_id: str, chunk_number: int) -> None:
        """Remove a single chunk. Deleting a missing chunk is not an error."""

    @abstractmethod
    async def chunk_exists(self, upload_id: str, chunk_number: int) -> bool:
        """Whether chunk ``chunk_number`` is present in storage."""

    @abstractmethod
    async def list_chunks(self, upload_id: str) -> list[int]:
        """Sorted chunk numbers currently present for this upload."""

    @abstractmethod
    async def finalize(self, upload: Upload) -> FinalizeResult:
        """Assemble chunks 1..``total_chunks`` in order into the final object."""

    @abstractmethod
    async def cleanup_chunks(self, upload_id: str) -> None:
        """Drop the chunk working area, keeping any finalized object."""

    @abstractmethod
    async def delete_upload(self, upload_id: str) -> None:
        """Drop everything belonging to this upload, finalized object included."""
