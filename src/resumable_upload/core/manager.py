"""The public API of the package: :class:`UploadManager`.

It owns the upload lifecycle — create, receive chunks, resume, complete, abort —
and knows nothing about HTTP or about how bytes are actually persisted.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..repository.base import MetadataRepository
from ..storage.base import Storage
from .checksum import checksums_equal, verify_checksum
from .exceptions import (
    ChecksumMismatchError,
    IncompleteUploadError,
    InvalidChunkError,
    InvalidUploadStateError,
    StorageError,
    UploadNotFoundError,
)
from .models import Upload, UploadStatus, new_upload_id, safe_filename, utcnow

__all__ = ["UploadManager", "DEFAULT_CHUNK_SIZE", "MAX_CHUNK_SIZE"]

DEFAULT_CHUNK_SIZE = 5 * 1024 * 1024
MAX_CHUNK_SIZE = 100 * 1024 * 1024
MIN_CHUNK_SIZE = 1


@dataclass
class _LockEntry:
    """A per-upload lock plus the number of tasks currently interested in it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiters: int = 0


class UploadManager:
    """Coordinates an upload session across a storage and a metadata backend.

    Example::

        storage = LocalStorage("./uploads")
        manager = UploadManager(storage=storage)

        upload = await manager.create_upload("video.mp4", total_size=1_048_576)
        await manager.upload_chunk(upload.upload_id, 1, data)
        await manager.complete_upload(upload.upload_id)

    Operations on a single ``upload_id`` are serialised with an in-process lock,
    so a client retrying a chunk while the first attempt is still in flight
    cannot corrupt the session.
    """

    def __init__(
        self,
        storage: Storage,
        repository: MetadataRepository | None = None,
        *,
        default_chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_chunk_size: int = MAX_CHUNK_SIZE,
        cleanup_chunks_on_complete: bool = True,
    ) -> None:
        self.storage = storage
        self.repository = repository or self._default_repository(storage)
        self.default_chunk_size = default_chunk_size
        self.max_chunk_size = max_chunk_size
        self.cleanup_chunks_on_complete = cleanup_chunks_on_complete
        self._locks: dict[str, _LockEntry] = {}
        self._locks_guard = asyncio.Lock()

    @staticmethod
    def _default_repository(storage: Storage) -> MetadataRepository:
        """Local storage brings its own metadata repository for free."""
        from ..repository.local import LocalMetadataRepository
        from ..storage.local import LocalStorage

        if isinstance(storage, LocalStorage):
            return LocalMetadataRepository(storage.root)
        raise ValueError(
            "A MetadataRepository must be supplied for non-local storage backends"
        )

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def _session_lock(self, upload_id: str) -> AsyncIterator[None]:
        """Serialise operations on one upload without leaking a lock per id.

        The entry is refcounted so it is only discarded once nobody is holding
        or waiting on it — dropping it earlier would let a second caller create
        a *different* lock for the same upload.
        """
        async with self._locks_guard:
            entry = self._locks.get(upload_id)
            if entry is None:
                entry = self._locks[upload_id] = _LockEntry()
            entry.waiters += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._locks_guard:
                entry.waiters -= 1
                if entry.waiters == 0 and self._locks.get(upload_id) is entry:
                    del self._locks[upload_id]

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    async def create_upload(
        self,
        filename: str,
        total_size: int,
        *,
        chunk_size: int | None = None,
        content_type: str | None = None,
        checksum: str | None = None,
        upload_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Upload:
        """Open a new upload session and return it, including its ``upload_id``.

        ``checksum`` is the optional SHA-256 of the *whole* file, verified when
        the upload is completed.
        """
        name = safe_filename(filename)
        if not filename.strip():
            raise InvalidChunkError("filename must not be empty")
        if total_size < 0:
            raise InvalidChunkError(f"total_size must be >= 0, got {total_size}")

        size = chunk_size if chunk_size is not None else self.default_chunk_size
        if not MIN_CHUNK_SIZE <= size <= self.max_chunk_size:
            raise InvalidChunkError(
                f"chunk_size must be between {MIN_CHUNK_SIZE} and "
                f"{self.max_chunk_size} bytes, got {size}"
            )

        upload = Upload(
            upload_id=upload_id or new_upload_id(),
            filename=name,
            content_type=content_type,
            total_size=total_size,
            chunk_size=size,
            total_chunks=Upload.total_chunks_for(total_size, size),
            checksum=checksum.strip().lower() if checksum else None,
            status=UploadStatus.PENDING,
            metadata=dict(metadata or {}),
        )
        await self.repository.create(upload)
        return upload

    async def get_upload(self, upload_id: str) -> Upload:
        """Return the session, raising ``UploadNotFoundError`` if unknown."""
        return await self.repository.get(upload_id)

    async def get_status(self, upload_id: str) -> dict:
        """Progress payload for resuming: uploaded and missing chunk numbers."""
        upload = await self.repository.get(upload_id)
        return upload.status_report()

    async def list_uploads(self, status: UploadStatus | None = None) -> list[Upload]:
        """All known sessions, newest first."""
        return await self.repository.list(status)

    # ------------------------------------------------------------------
    # Chunks
    # ------------------------------------------------------------------
    async def upload_chunk(
        self,
        upload_id: str,
        chunk_number: int,
        data: bytes,
        checksum: str | None = None,
    ) -> Upload:
        """Validate and store one chunk (1-based), returning the updated session.

        The operation is idempotent: re-sending a chunk that is already stored
        with the same content is accepted and changes nothing.
        """
        async with self._session_lock(upload_id):
            upload = await self.repository.get(upload_id)

            if not upload.status.accepts_chunks:
                raise InvalidUploadStateError(
                    upload_id,
                    upload.status.value,
                    "PENDING, UPLOADING, FAILED",
                )

            self._validate_chunk(upload, chunk_number, data)

            # Calculate and compare before touching storage, so a corrupt chunk
            # never lands on disk.
            actual = verify_checksum(data, checksum, chunk_number=chunk_number)

            if await self._is_duplicate(upload, chunk_number, actual):
                return upload

            await self.storage.write_chunk(upload_id, chunk_number, data)

            upload.chunk_checksums[chunk_number] = actual
            if upload.status is not UploadStatus.UPLOADING:
                upload.status = UploadStatus.UPLOADING
            upload.error = None
            upload.touch()
            await self.repository.save(upload)
            return upload

    def _validate_chunk(self, upload: Upload, chunk_number: int, data: bytes) -> None:
        if not 1 <= chunk_number <= upload.total_chunks:
            raise InvalidChunkError(
                f"chunk_number {chunk_number} out of range 1..{upload.total_chunks}"
            )
        expected_size = upload.expected_chunk_size(chunk_number)
        if len(data) != expected_size:
            raise InvalidChunkError(
                f"chunk {chunk_number} must be exactly {expected_size} bytes, "
                f"got {len(data)}"
            )

    async def _is_duplicate(self, upload: Upload, chunk_number: int, actual: str) -> bool:
        """Whether this chunk is a replay we can safely ignore.

        A chunk counts as already stored only when the recorded checksum matches
        *and* the bytes are still in storage — otherwise we fall through and
        write it again, which is what repairs a partially lost upload directory.
        """
        recorded = upload.chunk_checksums.get(chunk_number)
        if recorded is None or not checksums_equal(recorded, actual):
            return False
        return await self.storage.chunk_exists(upload.upload_id, chunk_number)

    async def has_chunk(self, upload_id: str, chunk_number: int) -> bool:
        upload = await self.repository.get(upload_id)
        return upload.has_chunk(chunk_number)

    async def missing_chunks(self, upload_id: str) -> list[int]:
        """Chunk numbers the client still needs to send."""
        upload = await self.repository.get(upload_id)
        return upload.missing_chunks

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------
    async def complete_upload(
        self, upload_id: str, checksum: str | None = None
    ) -> Upload:
        """Assemble the chunks into the final file and mark the upload COMPLETED.

        Idempotent: completing an already-completed upload returns it unchanged.
        """
        async with self._session_lock(upload_id):
            upload = await self.repository.get(upload_id)

            if upload.status is UploadStatus.COMPLETED:
                return upload
            if upload.status is UploadStatus.ABORTED:
                raise InvalidUploadStateError(
                    upload_id, upload.status.value, "PENDING, UPLOADING, FAILED"
                )
            if not upload.is_complete:
                raise IncompleteUploadError(upload_id, upload.missing_chunks)

            expected = (checksum or upload.checksum or "").strip().lower() or None

            upload.status = UploadStatus.COMPLETING
            upload.touch()
            await self.repository.save(upload)

            try:
                result = await self.storage.finalize(upload)
            except Exception as exc:
                await self._mark_failed(upload, str(exc))
                raise

            if result.size != upload.total_size:
                await self._mark_failed(
                    upload,
                    f"assembled size {result.size} != declared {upload.total_size}",
                )
                raise StorageError(
                    f"Assembled file is {result.size} bytes but the upload "
                    f"declared {upload.total_size}"
                )

            if expected and not checksums_equal(expected, result.checksum):
                # The assembled file is wrong; drop it rather than serve corrupt
                # data, but keep the session so the client can resend chunks.
                await self.storage.delete_upload(upload_id)
                upload.chunk_checksums.clear()
                await self._mark_failed(upload, "file checksum mismatch")
                raise ChecksumMismatchError(expected, result.checksum)

            upload.status = UploadStatus.COMPLETED
            upload.checksum = result.checksum
            upload.final_path = result.location
            upload.completed_at = utcnow()
            upload.error = None
            upload.touch()
            await self.repository.save(upload)

            if self.cleanup_chunks_on_complete:
                await self.storage.cleanup_chunks(upload_id)
            return upload

    async def _mark_failed(self, upload: Upload, error: str) -> None:
        upload.status = UploadStatus.FAILED
        upload.error = error
        upload.touch()
        await self.repository.save(upload)

    async def mark_failed(self, upload_id: str, error: str) -> Upload:
        """Record that an upload failed. It can still be resumed afterwards."""
        upload = await self.repository.get(upload_id)
        await self._mark_failed(upload, error)
        return upload

    # ------------------------------------------------------------------
    # Teardown and repair
    # ------------------------------------------------------------------
    async def abort_upload(self, upload_id: str) -> Upload:
        """Mark the session ABORTED and discard its data.

        The metadata record is kept so a client polling the id learns *why* its
        upload stopped rather than getting a bare 404.
        """
        async with self._session_lock(upload_id):
            upload = await self.repository.get(upload_id)
            await self.storage.delete_upload(upload_id)
            upload.status = UploadStatus.ABORTED
            upload.chunk_checksums.clear()
            upload.final_path = None
            upload.touch()
            await self.repository.save(upload)
            return upload

    async def delete_upload(self, upload_id: str) -> None:
        """Erase the session entirely: chunks, final file and metadata."""
        async with self._session_lock(upload_id):
            await self.storage.delete_upload(upload_id)
            await self.repository.delete(upload_id)

    async def repair(self, upload_id: str) -> Upload:
        """Re-sync the session with what is actually in storage.

        Useful after a crash: chunks the metadata claims but storage lost are
        dropped from the record, so the client is told to resend them.
        """
        async with self._session_lock(upload_id):
            upload = await self.repository.get(upload_id)
            if upload.status is UploadStatus.COMPLETED:
                return upload
            present = set(await self.storage.list_chunks(upload_id))
            stale = [n for n in upload.chunk_checksums if n not in present]
            if stale:
                for number in stale:
                    del upload.chunk_checksums[number]
                upload.touch()
                await self.repository.save(upload)
            return upload

    async def exists(self, upload_id: str) -> bool:
        try:
            await self.repository.get(upload_id)
        except UploadNotFoundError:
            return False
        return True
