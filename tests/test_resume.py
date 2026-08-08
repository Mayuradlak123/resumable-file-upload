"""Resuming after failures, including across a process restart."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_payload, split, upload_all
from resumable_upload import (
    LocalStorage,
    UploadManager,
    UploadStatus,
    sha256_bytes,
)


async def test_client_resumes_only_the_missing_chunks(manager):
    data = make_payload(1000)
    upload = await manager.create_upload("resume.bin", len(data), chunk_size=100)

    # Chunks 1-4 land, then the network dies.
    await upload_all(
        manager, upload.upload_id, data, 100, skip=set(range(5, 11))
    )
    report = await manager.get_status(upload.upload_id)
    assert report["uploaded_chunks"] == [1, 2, 3, 4]
    assert report["missing_chunks"] == [5, 6, 7, 8, 9, 10]

    # The client comes back and sends only what the server says it lacks.
    blocks = split(data, 100)
    for number in report["missing_chunks"]:
        await manager.upload_chunk(upload.upload_id, number, blocks[number - 1])

    completed = await manager.complete_upload(upload.upload_id)
    assert Path(completed.final_path).read_bytes() == data


async def test_upload_survives_a_manager_restart(tmp_path):
    """A new process pointed at the same directory must see the same session."""
    data = make_payload(500)
    root = tmp_path / "uploads"

    first = UploadManager(storage=LocalStorage(root))
    upload = await first.create_upload("restart.bin", len(data), chunk_size=100)
    await upload_all(first, upload.upload_id, data, 100, skip={4, 5})

    del first
    second = UploadManager(storage=LocalStorage(root))

    reloaded = await second.get_upload(upload.upload_id)
    assert reloaded.filename == "restart.bin"
    assert reloaded.status is UploadStatus.UPLOADING
    assert reloaded.missing_chunks == [4, 5]

    blocks = split(data, 100)
    for number in (4, 5):
        await second.upload_chunk(upload.upload_id, number, blocks[number - 1])
    completed = await second.complete_upload(upload.upload_id)

    assert Path(completed.final_path).read_bytes() == data


async def test_failed_upload_can_be_resumed(manager):
    data = make_payload(300)
    upload = await manager.create_upload("f.bin", len(data), chunk_size=100)
    await manager.upload_chunk(upload.upload_id, 1, data[:100])

    failed = await manager.mark_failed(upload.upload_id, "connection reset")
    assert failed.status is UploadStatus.FAILED
    assert failed.error == "connection reset"

    # FAILED is recoverable, not terminal: chunks are still accepted.
    await upload_all(manager, upload.upload_id, data, 100, skip={1})
    resumed = await manager.get_upload(upload.upload_id)
    assert resumed.status is UploadStatus.UPLOADING
    assert resumed.error is None

    completed = await manager.complete_upload(upload.upload_id)
    assert Path(completed.final_path).read_bytes() == data


async def test_repair_drops_chunks_storage_no_longer_has(manager, storage):
    data = make_payload(500)
    upload = await manager.create_upload("rep.bin", len(data), chunk_size=100)
    await upload_all(manager, upload.upload_id, data, 100)

    # Simulate a crash that lost part of the working directory.
    await storage.delete_chunk(upload.upload_id, 2)
    await storage.delete_chunk(upload.upload_id, 3)

    repaired = await manager.repair(upload.upload_id)

    assert repaired.missing_chunks == [2, 3]
    blocks = split(data, 100)
    for number in (2, 3):
        await manager.upload_chunk(upload.upload_id, number, blocks[number - 1])
    completed = await manager.complete_upload(upload.upload_id)
    assert Path(completed.final_path).read_bytes() == data


async def test_repair_is_a_no_op_for_a_healthy_upload(manager):
    data = make_payload(300)
    upload = await manager.create_upload("h.bin", len(data), chunk_size=100)
    await upload_all(manager, upload.upload_id, data, 100)

    repaired = await manager.repair(upload.upload_id)

    assert repaired.uploaded_chunks == [1, 2, 3]


async def test_resume_with_checksums_end_to_end(manager):
    data = make_payload(2048, seed=3)
    upload = await manager.create_upload(
        "ck.bin", len(data), chunk_size=512, checksum=sha256_bytes(data)
    )

    await upload_all(manager, upload.upload_id, data, 512, with_checksums=True, skip={2})
    assert await manager.missing_chunks(upload.upload_id) == [2]

    await manager.upload_chunk(
        upload.upload_id, 2, data[512:1024], checksum=sha256_bytes(data[512:1024])
    )
    completed = await manager.complete_upload(upload.upload_id)

    assert completed.status is UploadStatus.COMPLETED
    assert Path(completed.final_path).read_bytes() == data
