"""SHA-256 helpers and checksum enforcement."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from conftest import make_payload, upload_all
from resumable_upload import (
    ChecksumMismatchError,
    UploadStatus,
    sha256_bytes,
    sha256_stream,
    verify_checksum,
)
from resumable_upload.core.checksum import checksums_equal, sha256_chunks


def test_sha256_bytes_matches_hashlib():
    data = make_payload(1024)
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_sha256_stream_and_chunks_match_whole_digest():
    data = make_payload(300_000)
    expected = hashlib.sha256(data).hexdigest()

    assert sha256_stream(io.BytesIO(data)) == expected
    assert sha256_chunks([data[i : i + 1000] for i in range(0, len(data), 1000)]) == expected


def test_checksum_comparison_ignores_case_and_whitespace():
    digest = sha256_bytes(b"hello")
    assert checksums_equal(f"  {digest.upper()}  ", digest)
    assert not checksums_equal(digest, sha256_bytes(b"other"))


def test_verify_checksum_returns_digest_when_none_supplied():
    assert verify_checksum(b"abc", None) == sha256_bytes(b"abc")


def test_verify_checksum_raises_on_mismatch():
    with pytest.raises(ChecksumMismatchError) as excinfo:
        verify_checksum(b"abc", sha256_bytes(b"xyz"), chunk_number=4)

    assert excinfo.value.chunk_number == 4
    assert excinfo.value.actual == sha256_bytes(b"abc")


async def test_matching_chunk_checksum_is_accepted(manager):
    data = make_payload(200)
    upload = await manager.create_upload("ok.bin", len(data), chunk_size=100)

    await manager.upload_chunk(
        upload.upload_id, 1, data[:100], checksum=sha256_bytes(data[:100])
    )

    assert (await manager.get_upload(upload.upload_id)).uploaded_chunks == [1]


async def test_corrupt_chunk_is_rejected_and_not_stored(manager, storage):
    data = make_payload(200)
    upload = await manager.create_upload("bad.bin", len(data), chunk_size=100)
    corrupted = bytes(100)

    with pytest.raises(ChecksumMismatchError):
        await manager.upload_chunk(
            upload.upload_id, 1, corrupted, checksum=sha256_bytes(data[:100])
        )

    assert await storage.chunk_exists(upload.upload_id, 1) is False
    assert (await manager.get_upload(upload.upload_id)).uploaded_chunks == []


async def test_checksum_is_case_insensitive(manager):
    data = make_payload(100)
    upload = await manager.create_upload("case.bin", len(data), chunk_size=100)

    await manager.upload_chunk(
        upload.upload_id, 1, data, checksum=sha256_bytes(data).upper()
    )

    assert (await manager.get_upload(upload.upload_id)).uploaded_chunks == [1]


async def test_whole_file_checksum_is_verified_on_completion(manager):
    data = make_payload(512)
    upload = await manager.create_upload(
        "whole.bin", len(data), chunk_size=128, checksum=sha256_bytes(data)
    )
    await upload_all(manager, upload.upload_id, data, 128)

    completed = await manager.complete_upload(upload.upload_id)

    assert completed.status is UploadStatus.COMPLETED
    assert Path(completed.final_path).read_bytes() == data


async def test_wrong_whole_file_checksum_fails_the_upload(manager, storage):
    data = make_payload(256)
    upload = await manager.create_upload(
        "wrong.bin", len(data), chunk_size=128, checksum=sha256_bytes(b"something else")
    )
    await upload_all(manager, upload.upload_id, data, 128)

    with pytest.raises(ChecksumMismatchError):
        await manager.complete_upload(upload.upload_id)

    failed = await manager.get_upload(upload.upload_id)
    assert failed.status is UploadStatus.FAILED
    assert failed.error == "file checksum mismatch"
    # The bad assembly must not be left behind for anyone to download.
    assert not storage.final_dir(upload.upload_id).exists()


async def test_completion_checksum_can_be_supplied_late(manager):
    data = make_payload(256)
    upload = await manager.create_upload("late.bin", len(data), chunk_size=128)
    await upload_all(manager, upload.upload_id, data, 128)

    completed = await manager.complete_upload(
        upload.upload_id, checksum=sha256_bytes(data)
    )

    assert completed.status is UploadStatus.COMPLETED


async def test_completion_records_the_digest_even_without_one_supplied(manager):
    data = make_payload(256)
    upload = await manager.create_upload("auto.bin", len(data), chunk_size=128)
    await upload_all(manager, upload.upload_id, data, 128)

    completed = await manager.complete_upload(upload.upload_id)

    assert completed.checksum == sha256_bytes(data)
