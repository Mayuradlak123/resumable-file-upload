"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import pytest

from resumable_upload import LocalStorage, UploadManager, sha256_bytes


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "uploads")


@pytest.fixture
def manager(storage):
    return UploadManager(storage=storage)


def make_payload(size: int, seed: int = 0) -> bytes:
    """Deterministic pseudo-random bytes, so digests are reproducible."""
    return bytes((i * 31 + seed * 7 + 13) % 256 for i in range(size))


def split(data: bytes, chunk_size: int) -> list[bytes]:
    """Split ``data`` the way a client would, into 1-based chunks."""
    if not data:
        return [b""]
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


async def upload_all(
    manager: UploadManager,
    upload_id: str,
    data: bytes,
    chunk_size: int,
    *,
    with_checksums: bool = False,
    skip: set[int] | None = None,
) -> None:
    """Send every chunk of ``data``, optionally skipping some chunk numbers."""
    skip = skip or set()
    for number, block in enumerate(split(data, chunk_size), start=1):
        if number in skip:
            continue
        await manager.upload_chunk(
            upload_id,
            number,
            block,
            checksum=sha256_bytes(block) if with_checksums else None,
        )
