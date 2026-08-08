"""Metadata repository abstraction.

Where the *bytes* live is the storage backend's problem; where the *session
state* lives is this one's. Splitting them means a future deployment can keep
chunks on S3 while keeping metadata in Postgres or Redis, without the
``UploadManager`` changing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.models import Upload, UploadStatus

__all__ = ["MetadataRepository"]


class MetadataRepository(ABC):
    """Persistence for :class:`~resumable_upload.core.models.Upload` records."""

    @abstractmethod
    async def create(self, upload: Upload) -> None:
        """Store a new session. Raises ``UploadAlreadyExistsError`` on conflict."""

    @abstractmethod
    async def save(self, upload: Upload) -> None:
        """Persist the current state of an existing session (upsert)."""

    @abstractmethod
    async def get(self, upload_id: str) -> Upload:
        """Load a session. Raises ``UploadNotFoundError`` if unknown."""

    @abstractmethod
    async def find(self, upload_id: str) -> Upload | None:
        """Load a session, or ``None`` when it does not exist."""

    @abstractmethod
    async def delete(self, upload_id: str) -> None:
        """Forget a session. Deleting an unknown session is not an error."""

    @abstractmethod
    async def list(self, status: UploadStatus | None = None) -> list[Upload]:
        """All known sessions, newest first, optionally filtered by status."""
