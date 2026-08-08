"""LocalStorage, the metadata repository, and the storage abstraction itself."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_payload, upload_all
from resumable_upload import (
    LocalMetadataRepository,
    LocalStorage,
    Storage,
    StorageError,
    Upload,
    UploadAlreadyExistsError,
    UploadManager,
    UploadNotFoundError,
    UploadStatus,
    sha256_bytes,
)
from resumable_upload.repository.local import METADATA_FILENAME


async def test_write_read_and_delete_a_chunk(storage):
    written = await storage.write_chunk("ABC123", 1, b"hello")

    assert written == 5
    assert await storage.chunk_exists("ABC123", 1) is True
    assert await storage.read_chunk("ABC123", 1) == b"hello"

    await storage.delete_chunk("ABC123", 1)
    assert await storage.chunk_exists("ABC123", 1) is False
    # Deleting twice is not an error.
    await storage.delete_chunk("ABC123", 1)


async def test_read_missing_chunk_raises(storage):
    with pytest.raises(StorageError):
        await storage.read_chunk("ABC123", 7)


async def test_chunk_layout_on_disk(storage):
    await storage.write_chunk("ABC123", 1, b"a")
    await storage.write_chunk("ABC123", 42, b"b")

    chunks_dir = storage.root / "ABC123" / "chunks"
    assert {p.name for p in chunks_dir.iterdir()} == {"000001.part", "000042.part"}
    assert await storage.list_chunks("ABC123") == [1, 42]


async def test_list_chunks_of_unknown_upload_is_empty(storage):
    assert await storage.list_chunks("NOPE") == []


async def test_writing_a_chunk_leaves_no_temporary_files(storage):
    await storage.write_chunk("ABC123", 1, b"hello")

    names = [p.name for p in (storage.root / "ABC123" / "chunks").iterdir()]
    assert names == ["000001.part"]


@pytest.mark.parametrize("upload_id", ["../escape", "a/b", "a\\b", "", ".", "completed"])
async def test_unsafe_upload_ids_are_rejected(storage, upload_id):
    with pytest.raises(StorageError):
        await storage.write_chunk(upload_id, 1, b"x")


async def test_finalize_concatenates_in_order_and_hashes(storage):
    data = make_payload(250)
    upload = Upload(
        upload_id="FINAL1",
        filename="movie.mp4",
        total_size=len(data),
        chunk_size=100,
        total_chunks=3,
    )
    # Written out of order on purpose: assembly order comes from chunk numbers.
    for number in (3, 1, 2):
        start = (number - 1) * 100
        await storage.write_chunk("FINAL1", number, data[start : start + 100])

    result = await storage.finalize(upload)

    assert result.size == len(data)
    assert result.checksum == sha256_bytes(data)
    assert Path(result.location) == storage.root / "completed" / "FINAL1" / "movie.mp4"
    assert Path(result.location).read_bytes() == data


async def test_finalize_fails_loudly_when_a_chunk_is_missing(storage):
    upload = Upload(
        upload_id="FINAL2",
        filename="x.bin",
        total_size=200,
        chunk_size=100,
        total_chunks=2,
    )
    await storage.write_chunk("FINAL2", 1, b"a" * 100)

    with pytest.raises(StorageError):
        await storage.finalize(upload)

    # No partial file is left behind.
    assert not (storage.root / "completed" / "FINAL2" / "x.bin").exists()


async def test_cleanup_chunks_keeps_the_final_file(manager, storage):
    data = make_payload(300)
    upload = await manager.create_upload("keep.bin", len(data), chunk_size=100)
    await upload_all(manager, upload.upload_id, data, 100)

    completed = await manager.complete_upload(upload.upload_id)

    assert await storage.list_chunks(upload.upload_id) == []
    assert Path(completed.final_path).read_bytes() == data


async def test_chunks_are_kept_when_cleanup_is_disabled(storage):
    manager = UploadManager(storage=storage, cleanup_chunks_on_complete=False)
    data = make_payload(300)
    upload = await manager.create_upload("keep2.bin", len(data), chunk_size=100)
    await upload_all(manager, upload.upload_id, data, 100)

    await manager.complete_upload(upload.upload_id)

    assert await storage.list_chunks(upload.upload_id) == [1, 2, 3]


async def test_final_filename_cannot_escape_the_storage_root(storage):
    upload = Upload(
        upload_id="ESCAPE",
        filename="../../evil.sh",
        total_size=1,
        chunk_size=1,
        total_chunks=1,
    )
    await storage.write_chunk("ESCAPE", 1, b"x")

    result = await storage.finalize(upload)

    assert Path(result.location).parent == storage.root / "completed" / "ESCAPE"


async def test_local_storage_implements_the_abstraction(storage):
    assert isinstance(storage, Storage)


# ----------------------------------------------------------------------
# Metadata repository
# ----------------------------------------------------------------------
def _sample(upload_id: str = "META01") -> Upload:
    return Upload(
        upload_id=upload_id,
        filename="a.bin",
        content_type="application/octet-stream",
        total_size=300,
        chunk_size=100,
        total_chunks=3,
        chunk_checksums={1: sha256_bytes(b"one")},
        metadata={"owner": "alice"},
    )


async def test_repository_round_trips_every_field(tmp_path):
    repo = LocalMetadataRepository(tmp_path)
    upload = _sample()

    await repo.create(upload)
    loaded = await repo.get("META01")

    assert loaded.to_dict() == upload.to_dict()
    assert loaded.chunk_checksums == {1: sha256_bytes(b"one")}
    assert loaded.metadata == {"owner": "alice"}
    assert loaded.created_at == upload.created_at


async def test_metadata_lives_beside_the_chunks(tmp_path):
    repo = LocalMetadataRepository(tmp_path)
    await repo.create(_sample())

    path = tmp_path / "META01" / METADATA_FILENAME
    assert json.loads(path.read_text())["filename"] == "a.bin"


async def test_repository_rejects_duplicate_ids(tmp_path):
    repo = LocalMetadataRepository(tmp_path)
    await repo.create(_sample())

    with pytest.raises(UploadAlreadyExistsError):
        await repo.create(_sample())


async def test_repository_get_missing_raises_find_returns_none(tmp_path):
    repo = LocalMetadataRepository(tmp_path)

    assert await repo.find("NOPE") is None
    with pytest.raises(UploadNotFoundError):
        await repo.get("NOPE")


async def test_repository_delete_is_forgiving(tmp_path):
    repo = LocalMetadataRepository(tmp_path)
    await repo.create(_sample())

    await repo.delete("META01")
    await repo.delete("META01")

    assert await repo.find("META01") is None


async def test_repository_list_skips_the_completed_directory(tmp_path):
    repo = LocalMetadataRepository(tmp_path)
    LocalStorage(tmp_path)  # creates the "completed" sibling directory
    await repo.create(_sample("META01"))
    await repo.create(_sample("META02"))

    uploads = await repo.list()

    assert {u.upload_id for u in uploads} == {"META01", "META02"}


async def test_repository_reports_corrupt_metadata(tmp_path):
    repo = LocalMetadataRepository(tmp_path)
    await repo.create(_sample())
    (tmp_path / "META01" / METADATA_FILENAME).write_text("{not json")

    with pytest.raises(StorageError):
        await repo.get("META01")


async def test_upload_status_serialises_as_its_name(tmp_path):
    repo = LocalMetadataRepository(tmp_path)
    upload = _sample()
    upload.status = UploadStatus.COMPLETING
    await repo.save(upload)

    raw = json.loads((tmp_path / "META01" / METADATA_FILENAME).read_text())
    assert raw["status"] == "COMPLETING"
    assert (await repo.get("META01")).status is UploadStatus.COMPLETING


async def test_manager_requires_a_repository_for_custom_storage():
    class DummyStorage(Storage):
        async def write_chunk(self, upload_id, chunk_number, data): ...
        async def read_chunk(self, upload_id, chunk_number): ...
        async def delete_chunk(self, upload_id, chunk_number): ...
        async def chunk_exists(self, upload_id, chunk_number): ...
        async def list_chunks(self, upload_id): ...
        async def finalize(self, upload): ...
        async def cleanup_chunks(self, upload_id): ...
        async def delete_upload(self, upload_id): ...

    with pytest.raises(ValueError):
        UploadManager(storage=DummyStorage())
