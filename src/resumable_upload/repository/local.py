"""Filesystem-backed metadata repository.

Each session is a ``metadata.json`` beside its chunks, which is what makes an
upload survive a server restart: the directory on disk *is* the state.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ..core.exceptions import StorageError, UploadAlreadyExistsError, UploadNotFoundError
from ..core.models import Upload, UploadStatus
from ..storage.local import COMPLETED_DIR, validate_upload_id
from .base import MetadataRepository

__all__ = ["LocalMetadataRepository", "METADATA_FILENAME"]

METADATA_FILENAME = "metadata.json"


class LocalMetadataRepository(MetadataRepository):
    """Stores one ``metadata.json`` per upload under ``root/{upload_id}/``."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def metadata_path(self, upload_id: str) -> Path:
        return self.root / validate_upload_id(upload_id) / METADATA_FILENAME

    # ------------------------------------------------------------------
    # Repository interface
    # ------------------------------------------------------------------
    async def create(self, upload: Upload) -> None:
        path = self.metadata_path(upload.upload_id)
        if await asyncio.to_thread(path.exists):
            raise UploadAlreadyExistsError(upload.upload_id)
        await asyncio.to_thread(self._write, path, upload)

    async def save(self, upload: Upload) -> None:
        await asyncio.to_thread(self._write, self.metadata_path(upload.upload_id), upload)

    async def get(self, upload_id: str) -> Upload:
        upload = await self.find(upload_id)
        if upload is None:
            raise UploadNotFoundError(upload_id)
        return upload

    async def find(self, upload_id: str) -> Upload | None:
        return await asyncio.to_thread(self._read, self.metadata_path(upload_id))

    async def delete(self, upload_id: str) -> None:
        path = self.metadata_path(upload_id)
        await asyncio.to_thread(path.unlink, True)

    async def list(self, status: UploadStatus | None = None) -> list[Upload]:
        uploads = await asyncio.to_thread(self._read_all)
        if status is not None:
            uploads = [u for u in uploads if u.status is status]
        uploads.sort(key=lambda u: u.created_at, reverse=True)
        return uploads

    # ------------------------------------------------------------------
    # Blocking implementations
    # ------------------------------------------------------------------
    @staticmethod
    def _write(path: Path, upload: Upload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(upload.to_dict(), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic replace: readers see either the old or the new metadata,
            # never a truncated file.
            os.replace(tmp, path)
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            tmp.unlink(missing_ok=True)
            raise StorageError(f"Failed to persist upload metadata: {exc}") from exc

    @staticmethod
    def _read(path: Path) -> Upload | None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return Upload.from_dict(json.load(handle))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise StorageError(f"Corrupt upload metadata at {path}: {exc}") from exc

    def _read_all(self) -> list[Upload]:
        uploads: list[Upload] = []
        if not self.root.is_dir():
            return uploads
        for entry in self.root.iterdir():
            if not entry.is_dir() or entry.name == COMPLETED_DIR:
                continue
            upload = self._read(entry / METADATA_FILENAME)
            if upload is not None:
                uploads.append(upload)
        return uploads
