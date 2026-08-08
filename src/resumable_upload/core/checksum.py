"""SHA-256 helpers used to validate chunks and completed files."""

from __future__ import annotations

import hashlib
from typing import IO, Iterable

from .exceptions import ChecksumMismatchError

__all__ = [
    "ALGORITHM",
    "sha256_bytes",
    "sha256_stream",
    "sha256_chunks",
    "checksums_equal",
    "verify_checksum",
]

ALGORITHM = "sha256"

_READ_SIZE = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: IO[bytes], read_size: int = _READ_SIZE) -> str:
    """Return the hex SHA-256 digest of a binary stream, read incrementally."""
    digest = hashlib.sha256()
    while True:
        block = stream.read(read_size)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def sha256_chunks(blocks: Iterable[bytes]) -> str:
    """Return the hex SHA-256 digest of an iterable of byte blocks."""
    digest = hashlib.sha256()
    for block in blocks:
        digest.update(block)
    return digest.hexdigest()


def checksums_equal(expected: str, actual: str) -> bool:
    """Compare two hex digests case-insensitively, ignoring surrounding space."""
    return expected.strip().lower() == actual.strip().lower()


def verify_checksum(
    data: bytes,
    expected: str | None,
    *,
    chunk_number: int | None = None,
) -> str:
    """Calculate the digest of ``data`` and raise if it differs from ``expected``.

    ``expected`` may be ``None``, in which case the digest is simply returned:
    checksums are optional per the Phase 1 spec.
    """
    actual = sha256_bytes(data)
    if expected is not None and not checksums_equal(expected, actual):
        raise ChecksumMismatchError(expected.strip().lower(), actual, chunk_number)
    return actual
