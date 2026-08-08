"""Data model for upload sessions.

The package deliberately depends on nothing but the standard library, so the
model is built from dataclasses rather than pydantic. ``Upload.to_dict`` /
``Upload.from_dict`` provide the JSON round-trip used by the repository.
"""

from __future__ import annotations

import math
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

__all__ = ["UploadStatus", "Upload", "new_upload_id", "utcnow"]

# Crockford base32, the alphabet used by ULID.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def _encode_base32(value: int, length: int) -> str:
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_upload_id() -> str:
    """Generate a lexicographically sortable, URL-safe 26 character ULID."""
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = secrets.randbits(80)
    return _encode_base32(timestamp, 10) + _encode_base32(randomness, 16)


class UploadStatus(str, Enum):
    """Lifecycle of an upload session."""

    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    COMPLETING = "COMPLETING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"

    @property
    def is_terminal(self) -> bool:
        return self in (UploadStatus.COMPLETED, UploadStatus.ABORTED)

    @property
    def accepts_chunks(self) -> bool:
        return self in (UploadStatus.PENDING, UploadStatus.UPLOADING, UploadStatus.FAILED)


@dataclass
class Upload:
    """Metadata describing a single resumable upload session."""

    upload_id: str
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    content_type: str | None = None
    checksum: str | None = None
    """Optional SHA-256 of the whole file, verified on completion."""
    status: UploadStatus = UploadStatus.PENDING
    chunk_checksums: dict[int, str] = field(default_factory=dict)
    """Maps chunk number -> SHA-256 of the stored chunk. Doubles as the
    record of which chunks have been received."""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None
    final_path: str | None = None
    error: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    """Free-form key/value data supplied by the caller."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @staticmethod
    def total_chunks_for(total_size: int, chunk_size: int) -> int:
        """Number of chunks a file of ``total_size`` splits into.

        A zero-byte file still consists of exactly one (empty) chunk, which
        keeps the client and server chunk loops identical for every file.
        """
        if total_size == 0:
            return 1
        return math.ceil(total_size / chunk_size)

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------
    @property
    def uploaded_chunks(self) -> list[int]:
        """Sorted chunk numbers that have been stored (1-based)."""
        return sorted(self.chunk_checksums)

    @property
    def missing_chunks(self) -> list[int]:
        """Sorted chunk numbers that have not been stored yet (1-based)."""
        received = self.chunk_checksums
        return [n for n in range(1, self.total_chunks + 1) if n not in received]

    @property
    def is_complete(self) -> bool:
        """True once every chunk has been received."""
        return len(self.chunk_checksums) == self.total_chunks

    @property
    def uploaded_size(self) -> int:
        """Bytes stored so far, derived from which chunks have arrived."""
        return sum(self.expected_chunk_size(n) for n in self.chunk_checksums)

    @property
    def progress(self) -> float:
        """Fraction of the file received, in ``[0.0, 1.0]``."""
        if self.total_size == 0:
            return 1.0 if self.is_complete else 0.0
        return self.uploaded_size / self.total_size

    def has_chunk(self, chunk_number: int) -> bool:
        return chunk_number in self.chunk_checksums

    def expected_chunk_size(self, chunk_number: int) -> int:
        """Exact byte length chunk ``chunk_number`` must have."""
        if not 1 <= chunk_number <= self.total_chunks:
            raise ValueError(
                f"chunk_number {chunk_number} out of range 1..{self.total_chunks}"
            )
        if chunk_number < self.total_chunks:
            return self.chunk_size
        return self.total_size - self.chunk_size * (self.total_chunks - 1)

    def touch(self) -> None:
        self.updated_at = utcnow()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """JSON-serialisable representation, used for ``metadata.json``."""
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "total_size": self.total_size,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
            "checksum": self.checksum,
            "status": self.status.value,
            # JSON object keys must be strings.
            "chunk_checksums": {str(k): v for k, v in sorted(self.chunk_checksums.items())},
            "uploaded_chunks": self.uploaded_chunks,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "final_path": self.final_path,
            "error": self.error,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Upload":
        def _dt(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        created_at = _dt(data.get("created_at")) or utcnow()
        return cls(
            upload_id=data["upload_id"],
            filename=data["filename"],
            total_size=data["total_size"],
            chunk_size=data["chunk_size"],
            total_chunks=data["total_chunks"],
            content_type=data.get("content_type"),
            checksum=data.get("checksum"),
            status=UploadStatus(data.get("status", UploadStatus.PENDING.value)),
            chunk_checksums={
                int(k): v for k, v in (data.get("chunk_checksums") or {}).items()
            },
            created_at=created_at,
            updated_at=_dt(data.get("updated_at")) or created_at,
            completed_at=_dt(data.get("completed_at")),
            final_path=data.get("final_path"),
            error=data.get("error"),
            metadata=dict(data.get("metadata") or {}),
        )

    def status_report(self) -> dict:
        """Compact progress payload, shaped for ``GET /uploads/{upload_id}``."""
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "status": self.status.value,
            "total_size": self.total_size,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
            "uploaded_chunks": self.uploaded_chunks,
            "missing_chunks": self.missing_chunks,
            "uploaded_size": self.uploaded_size,
            "progress": round(self.progress, 6),
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "final_path": self.final_path,
            "error": self.error,
        }


def safe_filename(filename: str, fallback: str = "upload.bin") -> str:
    """Strip directory components so a client-supplied name cannot escape a
    storage directory. Returns ``fallback`` if nothing usable remains."""
    name = os.path.basename(filename.replace("\\", "/")).strip()
    if name in ("", ".", ".."):
        return fallback
    return name
