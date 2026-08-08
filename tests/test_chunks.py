"""Chunk validation, ordering and idempotency."""

from __future__ import annotations

import pytest

from conftest import make_payload, upload_all
from resumable_upload import InvalidChunkError, sha256_bytes


async def test_chunks_may_arrive_out_of_order(manager):
    data = make_payload(500)
    upload = await manager.create_upload("o.bin", len(data), chunk_size=100)

    for number in (5, 1, 4, 2, 3):
        start = (number - 1) * 100
        await manager.upload_chunk(upload.upload_id, number, data[start : start + 100])

    completed = await manager.complete_upload(upload.upload_id)
    from pathlib import Path

    assert Path(completed.final_path).read_bytes() == data


@pytest.mark.parametrize("chunk_number", [0, -1, 4, 99])
async def test_chunk_number_out_of_range_is_rejected(manager, chunk_number):
    data = make_payload(300)
    upload = await manager.create_upload("n.bin", len(data), chunk_size=100)

    with pytest.raises(InvalidChunkError):
        await manager.upload_chunk(upload.upload_id, chunk_number, data[:100])


async def test_wrong_chunk_size_is_rejected(manager):
    data = make_payload(300)
    upload = await manager.create_upload("w.bin", len(data), chunk_size=100)

    with pytest.raises(InvalidChunkError):
        await manager.upload_chunk(upload.upload_id, 1, data[:50])   # too short
    with pytest.raises(InvalidChunkError):
        await manager.upload_chunk(upload.upload_id, 1, data[:150])  # too long

    assert (await manager.get_upload(upload.upload_id)).uploaded_chunks == []


async def test_last_chunk_may_be_shorter(manager):
    data = make_payload(250)
    upload = await manager.create_upload("l.bin", len(data), chunk_size=100)

    assert upload.expected_chunk_size(3) == 50
    await manager.upload_chunk(upload.upload_id, 3, data[200:])

    # ...but only the last one.
    with pytest.raises(InvalidChunkError):
        await manager.upload_chunk(upload.upload_id, 1, data[:50])


async def test_rejected_chunk_is_not_written_to_storage(manager, storage):
    data = make_payload(300)
    upload = await manager.create_upload("r.bin", len(data), chunk_size=100)

    with pytest.raises(InvalidChunkError):
        await manager.upload_chunk(upload.upload_id, 2, b"short")

    assert await storage.chunk_exists(upload.upload_id, 2) is False


async def test_duplicate_chunk_is_a_no_op(manager, storage):
    """The classic lost-response retry: same chunk twice must not double-write."""
    data = make_payload(300)
    upload = await manager.create_upload("d.bin", len(data), chunk_size=100)

    first = await manager.upload_chunk(upload.upload_id, 1, data[:100])
    mtime_before = storage.chunk_path(upload.upload_id, 1).stat().st_mtime_ns
    second = await manager.upload_chunk(upload.upload_id, 1, data[:100])

    assert first.uploaded_chunks == second.uploaded_chunks == [1]
    assert storage.chunk_path(upload.upload_id, 1).stat().st_mtime_ns == mtime_before
    assert await storage.list_chunks(upload.upload_id) == [1]


async def test_resending_a_chunk_with_different_bytes_overwrites_it(manager, storage):
    data = make_payload(200)
    upload = await manager.create_upload("v.bin", len(data), chunk_size=100)

    await manager.upload_chunk(upload.upload_id, 1, b"a" * 100)
    await manager.upload_chunk(upload.upload_id, 1, data[:100])

    assert await storage.read_chunk(upload.upload_id, 1) == data[:100]
    record = await manager.get_upload(upload.upload_id)
    assert record.chunk_checksums[1] == sha256_bytes(data[:100])


async def test_duplicate_is_rewritten_when_storage_lost_the_chunk(manager, storage):
    """Metadata says 'stored' but the bytes are gone: the retry must repair it."""
    data = make_payload(200)
    upload = await manager.create_upload("g.bin", len(data), chunk_size=100)
    await manager.upload_chunk(upload.upload_id, 1, data[:100])

    await storage.delete_chunk(upload.upload_id, 1)
    await manager.upload_chunk(upload.upload_id, 1, data[:100])

    assert await storage.chunk_exists(upload.upload_id, 1) is True


async def test_concurrent_uploads_of_the_same_chunk_are_serialised(manager, storage):
    import asyncio

    data = make_payload(500)
    upload = await manager.create_upload("cc.bin", len(data), chunk_size=100)

    await asyncio.gather(
        *[
            manager.upload_chunk(upload.upload_id, n, data[(n - 1) * 100 : n * 100])
            for n in (1, 1, 2, 2, 3, 4, 5)
        ]
    )

    record = await manager.get_upload(upload.upload_id)
    assert record.uploaded_chunks == [1, 2, 3, 4, 5]
    assert await storage.list_chunks(upload.upload_id) == [1, 2, 3, 4, 5]


async def test_missing_chunks_tracks_what_is_left(manager):
    data = make_payload(1000)
    upload = await manager.create_upload("m.bin", len(data), chunk_size=100)

    await upload_all(manager, upload.upload_id, data, 100, skip={3, 7})

    assert await manager.missing_chunks(upload.upload_id) == [3, 7]
    assert await manager.has_chunk(upload.upload_id, 3) is False
    assert await manager.has_chunk(upload.upload_id, 4) is True
