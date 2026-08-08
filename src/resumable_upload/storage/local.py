"""Local filesystem implementation of :class:`~resumable_upload.storage.base.Storage`.

Layout::

    root/
    ├── {upload_id}/
    │   ├── chunks/
    │   │   ├── 000001.part
    │   │   └── 000002.part
    │   └── metadata.json          (written by LocalMetadataRepository)
    └── completed/
        └── {upload_id}/
            └── original-file.mp4
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from pathlib import Path

from ..core.exceptions import StorageError
from ..core.models import Upload, safe_filename
from .base import FinalizeResult, Storage

__all__ = ["LocalStorage", "COMPLETED_DIR", "validate_upload_id"]

COMPLETED_DIR = "completed"
CHUNKS_DIR = "chunks"
_CHUNK_SUFFIX = ".part"
_CHUNK_NAME = re.compile(r"^(\d+)\.part$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_COPY_BUFFER = 1024 * 1024


def validate_upload_id(upload_id: str) -> str:
    """Reject ids that could escape the storage root when used as a path."""
    if not _SAFE_ID.match(upload_id) or upload_id in (".", "..", COMPLETED_DIR):
        raise StorageError(f"Unsafe upload id: {upload_id!r}")
    return upload_id


class LocalStorage(Storage):
    """Stores chunks and completed files under a single root directory.

    Blocking filesystem calls are pushed onto a worker thread so the async
    interface never stalls the event loop.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / COMPLETED_DIR).mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def upload_dir(self, upload_id: str) -> Path:
        return self.root / validate_upload_id(upload_id)

    def chunks_dir(self, upload_id: str) -> Path:
        return self.upload_dir(upload_id) / CHUNKS_DIR

    def chunk_path(self, upload_id: str, chunk_number: int) -> Path:
        if chunk_number < 1:
            raise StorageError(f"chunk_number must be >= 1, got {chunk_number}")
        return self.chunks_dir(upload_id) / f"{chunk_number:06d}{_CHUNK_SUFFIX}"

    def final_dir(self, upload_id: str) -> Path:
        return self.root / COMPLETED_DIR / validate_upload_id(upload_id)

    def final_path(self, upload: Upload) -> Path:
        return self.final_dir(upload.upload_id) / safe_filename(upload.filename)

    # ------------------------------------------------------------------
    # Storage interface
    # ------------------------------------------------------------------
    async def write_chunk(self, upload_id: str, chunk_number: int, data: bytes) -> int:
        return await asyncio.to_thread(self._write_chunk, upload_id, chunk_number, data)

    async def read_chunk(self, upload_id: str, chunk_number: int) -> bytes:
        return await asyncio.to_thread(self._read_chunk, upload_id, chunk_number)

    async def delete_chunk(self, upload_id: str, chunk_number: int) -> None:
        path = self.chunk_path(upload_id, chunk_number)
        await asyncio.to_thread(path.unlink, True)

    async def chunk_exists(self, upload_id: str, chunk_number: int) -> bool:
        path = self.chunk_path(upload_id, chunk_number)
        return await asyncio.to_thread(path.is_file)

    async def list_chunks(self, upload_id: str) -> list[int]:
        return await asyncio.to_thread(self._list_chunks, upload_id)

    async def finalize(self, upload: Upload) -> FinalizeResult:
        return await asyncio.to_thread(self._finalize, upload)

    async def cleanup_chunks(self, upload_id: str) -> None:
        directory = self.chunks_dir(upload_id)
        await asyncio.to_thread(shutil.rmtree, directory, True)

    async def delete_upload(self, upload_id: str) -> None:
        await asyncio.to_thread(self._delete_upload, upload_id)

    # ------------------------------------------------------------------
    # Blocking implementations
    # ------------------------------------------------------------------
    def _write_chunk(self, upload_id: str, chunk_number: int, data: bytes) -> int:
        path = self.chunk_path(upload_id, chunk_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename, so a crash mid-write can never leave
        # a half-written chunk that looks complete on resume.
        tmp = path.with_suffix(f"{_CHUNK_SUFFIX}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            tmp.unlink(missing_ok=True)
            raise StorageError(f"Failed to write chunk {chunk_number}: {exc}") from exc
        return len(data)

    def _read_chunk(self, upload_id: str, chunk_number: int) -> bytes:
        path = self.chunk_path(upload_id, chunk_number)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise StorageError(
                f"Chunk {chunk_number} of upload {upload_id!r} is not stored"
            ) from exc

    def _list_chunks(self, upload_id: str) -> list[int]:
        directory = self.chunks_dir(upload_id)
        if not directory.is_dir():
            return []
        numbers = []
        for entry in directory.iterdir():
            match = _CHUNK_NAME.match(entry.name)
            if match and entry.is_file():
                numbers.append(int(match.group(1)))
        return sorted(numbers)

    def _finalize(self, upload: Upload) -> FinalizeResult:
        destination = self.final_path(upload)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + ".assembling")
        digest = hashlib.sha256()
        size = 0
        try:
            with open(tmp, "wb") as out:
                for number in range(1, upload.total_chunks + 1):
                    source = self.chunk_path(upload.upload_id, number)
                    try:
                        with open(source, "rb") as part:
                            while True:
                                block = part.read(_COPY_BUFFER)
                                if not block:
                                    break
                                digest.update(block)
                                size += len(block)
                                out.write(block)
                    except FileNotFoundError as exc:
                        raise StorageError(
                            f"Chunk {number} of upload {upload.upload_id!r} is missing "
                            "from storage"
                        ) from exc
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, destination)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise StorageError(f"Failed to assemble upload: {exc}") from exc
        except StorageError:
            tmp.unlink(missing_ok=True)
            raise
        return FinalizeResult(
            location=str(destination),
            size=size,
            checksum=digest.hexdigest(),
        )

    def _delete_upload(self, upload_id: str) -> None:
        shutil.rmtree(self.upload_dir(upload_id), ignore_errors=True)
        shutil.rmtree(self.final_dir(upload_id), ignore_errors=True)
