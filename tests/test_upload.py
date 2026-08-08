"""Session creation, the happy-path flow, completion and teardown."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_payload, upload_all
from resumable_upload import (
    IncompleteUploadError,
    InvalidChunkError,
    InvalidUploadStateError,
    UploadNotFoundError,
    UploadStatus,
    sha256_bytes,
)


async def test_create_upload_assigns_id_and_chunk_count(manager):
    upload = await manager.create_upload(
        "video.mp4", total_size=250, chunk_size=100, content_type="video/mp4"
    )

    assert upload.upload_id
    assert len(upload.upload_id) == 26  # ULID
    assert upload.status is UploadStatus.PENDING
    assert upload.total_chunks == 3  # 100 + 100 + 50
    assert upload.content_type == "video/mp4"
    assert upload.uploaded_chunks == []
    assert upload.missing_chunks == [1, 2, 3]


async def test_create_upload_uses_default_chunk_size(manager):
    upload = await manager.create_upload("a.bin", total_size=10)
    assert upload.chunk_size == manager.default_chunk_size
    assert upload.total_chunks == 1


async def test_create_upload_strips_directories_from_filename(manager):
    upload = await manager.create_upload("../../etc/passwd", total_size=1)
    assert upload.filename == "passwd"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_size": -1},
        {"total_size": 10, "chunk_size": 0},
        {"total_size": 10, "chunk_size": -5},
    ],
)
async def test_create_upload_rejects_invalid_sizes(manager, kwargs):
    with pytest.raises(InvalidChunkError):
        await manager.create_upload("a.bin", **kwargs)


async def test_create_upload_rejects_oversized_chunk_size(manager):
    with pytest.raises(InvalidChunkError):
        await manager.create_upload(
            "a.bin", total_size=10, chunk_size=manager.max_chunk_size + 1
        )


async def test_full_upload_produces_identical_file(manager):
    data = make_payload(4096)
    upload = await manager.create_upload("payload.bin", len(data), chunk_size=1000)

    await upload_all(manager, upload.upload_id, data, 1000)
    completed = await manager.complete_upload(upload.upload_id)

    assert completed.status is UploadStatus.COMPLETED
    assert completed.completed_at is not None
    assert completed.final_path is not None
    final = Path(completed.final_path)
    assert final.name == "payload.bin"
    assert final.read_bytes() == data
    assert completed.checksum == sha256_bytes(data)


async def test_empty_file_uploads_as_one_empty_chunk(manager):
    upload = await manager.create_upload("empty.txt", total_size=0, chunk_size=100)
    assert upload.total_chunks == 1

    await manager.upload_chunk(upload.upload_id, 1, b"")
    completed = await manager.complete_upload(upload.upload_id)

    assert completed.status is UploadStatus.COMPLETED
    assert Path(completed.final_path).read_bytes() == b""


async def test_progress_advances_with_each_chunk(manager):
    data = make_payload(300)
    upload = await manager.create_upload("p.bin", len(data), chunk_size=100)

    assert upload.progress == 0.0
    await manager.upload_chunk(upload.upload_id, 1, data[:100])
    assert (await manager.get_upload(upload.upload_id)).progress == pytest.approx(1 / 3)

    await upload_all(manager, upload.upload_id, data, 100, skip={1})
    assert (await manager.get_upload(upload.upload_id)).progress == 1.0


async def test_status_moves_to_uploading_on_first_chunk(manager):
    data = make_payload(200)
    upload = await manager.create_upload("s.bin", len(data), chunk_size=100)

    await manager.upload_chunk(upload.upload_id, 1, data[:100])

    assert (await manager.get_upload(upload.upload_id)).status is UploadStatus.UPLOADING


async def test_complete_rejects_incomplete_upload(manager):
    data = make_payload(300)
    upload = await manager.create_upload("i.bin", len(data), chunk_size=100)
    await manager.upload_chunk(upload.upload_id, 1, data[:100])

    with pytest.raises(IncompleteUploadError) as excinfo:
        await manager.complete_upload(upload.upload_id)

    assert excinfo.value.missing_chunks == [2, 3]
    # A failed completion must not lose what was already received.
    assert (await manager.get_upload(upload.upload_id)).uploaded_chunks == [1]


async def test_complete_is_idempotent(manager):
    data = make_payload(200)
    upload = await manager.create_upload("c.bin", len(data), chunk_size=100)
    await upload_all(manager, upload.upload_id, data, 100)

    first = await manager.complete_upload(upload.upload_id)
    second = await manager.complete_upload(upload.upload_id)

    assert second.status is UploadStatus.COMPLETED
    assert second.completed_at == first.completed_at
    assert Path(second.final_path).read_bytes() == data


async def test_chunks_rejected_after_completion(manager):
    data = make_payload(100)
    upload = await manager.create_upload("done.bin", len(data), chunk_size=100)
    await upload_all(manager, upload.upload_id, data, 100)
    await manager.complete_upload(upload.upload_id)

    with pytest.raises(InvalidUploadStateError):
        await manager.upload_chunk(upload.upload_id, 1, data)


async def test_unknown_upload_raises(manager):
    with pytest.raises(UploadNotFoundError):
        await manager.get_upload("MISSING")
    with pytest.raises(UploadNotFoundError):
        await manager.upload_chunk("MISSING", 1, b"x")
    assert await manager.exists("MISSING") is False


async def test_abort_discards_data_but_keeps_the_record(manager):
    data = make_payload(200)
    upload = await manager.create_upload("a.bin", len(data), chunk_size=100)
    await manager.upload_chunk(upload.upload_id, 1, data[:100])

    aborted = await manager.abort_upload(upload.upload_id)

    assert aborted.status is UploadStatus.ABORTED
    assert aborted.uploaded_chunks == []
    # Still queryable, so a client learns why its upload stopped.
    assert (await manager.get_upload(upload.upload_id)).status is UploadStatus.ABORTED
    with pytest.raises(InvalidUploadStateError):
        await manager.upload_chunk(upload.upload_id, 1, data[:100])
    with pytest.raises(InvalidUploadStateError):
        await manager.complete_upload(upload.upload_id)


async def test_delete_removes_everything(manager, storage):
    data = make_payload(100)
    upload = await manager.create_upload("d.bin", len(data), chunk_size=100)
    await upload_all(manager, upload.upload_id, data, 100)
    await manager.complete_upload(upload.upload_id)

    await manager.delete_upload(upload.upload_id)

    with pytest.raises(UploadNotFoundError):
        await manager.get_upload(upload.upload_id)
    assert not storage.upload_dir(upload.upload_id).exists()
    assert not storage.final_dir(upload.upload_id).exists()


async def test_list_uploads_filters_by_status(manager):
    first = await manager.create_upload("one.bin", 100, chunk_size=100)
    second = await manager.create_upload("two.bin", 100, chunk_size=100)
    await upload_all(manager, second.upload_id, make_payload(100), 100)
    await manager.complete_upload(second.upload_id)

    everything = await manager.list_uploads()
    completed = await manager.list_uploads(UploadStatus.COMPLETED)

    assert {u.upload_id for u in everything} == {first.upload_id, second.upload_id}
    assert [u.upload_id for u in completed] == [second.upload_id]


async def test_status_report_shape(manager):
    data = make_payload(1000)
    upload = await manager.create_upload("r.bin", len(data), chunk_size=100)
    await upload_all(manager, upload.upload_id, data, 100, skip={5, 6, 7, 8, 9, 10})

    report = await manager.get_status(upload.upload_id)

    assert report["upload_id"] == upload.upload_id
    assert report["status"] == "UPLOADING"
    assert report["total_chunks"] == 10
    assert report["uploaded_chunks"] == [1, 2, 3, 4]
    assert report["missing_chunks"] == [5, 6, 7, 8, 9, 10]
