"""The FastAPI demo: it must be a thin adapter that maps errors to status codes."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from conftest import make_payload, split
from resumable_upload import sha256_bytes


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A freshly imported demo app pointed at an isolated upload directory."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    module = importlib.import_module("examples.fastapi_demo.main")
    return importlib.reload(module)


@pytest.fixture
async def client(demo):
    transport = ASGITransport(app=demo.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _create(client, filename, total_size, chunk_size, **extra):
    response = await client.post(
        "/uploads",
        json={
            "filename": filename,
            "total_size": total_size,
            "chunk_size": chunk_size,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _put(client, upload_id, number, block, checksum=None):
    headers = {"Content-Type": "application/octet-stream"}
    if checksum:
        headers["X-Chunk-Checksum"] = checksum
    return await client.put(
        f"/uploads/{upload_id}/chunks/{number}", content=block, headers=headers
    )


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_full_upload_over_http(client):
    data = make_payload(1000)
    session = await _create(client, "http.bin", len(data), 250)
    assert session["total_chunks"] == 4

    for number, block in enumerate(split(data, 250), start=1):
        response = await _put(client, session["upload_id"], number, block,
                              sha256_bytes(block))
        assert response.status_code == 200, response.text

    response = await client.post(f"/uploads/{session['upload_id']}/complete", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert Path(body["final_path"]).read_bytes() == data

    download = await client.get(f"/uploads/{session['upload_id']}/download")
    assert download.status_code == 200
    assert download.content == data


async def test_status_endpoint_drives_resume(client):
    data = make_payload(1000)
    session = await _create(client, "resume.bin", len(data), 100)
    blocks = split(data, 100)
    for number in (1, 2, 3, 4):
        await _put(client, session["upload_id"], number, blocks[number - 1])

    status = (await client.get(f"/uploads/{session['upload_id']}")).json()
    assert status["uploaded_chunks"] == [1, 2, 3, 4]
    assert status["missing_chunks"] == [5, 6, 7, 8, 9, 10]

    for number in status["missing_chunks"]:
        await _put(client, session["upload_id"], number, blocks[number - 1])

    completed = await client.post(f"/uploads/{session['upload_id']}/complete", json={})
    assert completed.json()["status"] == "COMPLETED"


async def test_duplicate_chunk_returns_200(client):
    data = make_payload(200)
    session = await _create(client, "dup.bin", len(data), 100)

    first = await _put(client, session["upload_id"], 1, data[:100])
    second = await _put(client, session["upload_id"], 1, data[:100])

    assert first.status_code == second.status_code == 200
    assert second.json()["uploaded_chunks"] == [1]


async def test_unknown_upload_is_404(client):
    response = await client.get("/uploads/DOESNOTEXIST")
    assert response.status_code == 404
    assert response.json()["error"] == "UploadNotFoundError"


async def test_bad_chunk_size_is_400(client):
    session = await _create(client, "bad.bin", 300, 100)
    response = await _put(client, session["upload_id"], 1, b"too short")
    assert response.status_code == 400
    assert response.json()["error"] == "InvalidChunkError"


async def test_checksum_mismatch_is_422(client):
    data = make_payload(100)
    session = await _create(client, "ck.bin", len(data), 100)

    response = await _put(client, session["upload_id"], 1, data, sha256_bytes(b"other"))

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "ChecksumMismatchError"
    assert body["actual"] == sha256_bytes(data)


async def test_incomplete_completion_is_409_and_lists_missing(client):
    data = make_payload(300)
    session = await _create(client, "inc.bin", len(data), 100)
    await _put(client, session["upload_id"], 1, data[:100])

    response = await client.post(f"/uploads/{session['upload_id']}/complete", json={})

    assert response.status_code == 409
    assert response.json()["missing_chunks"] == [2, 3]


async def test_abort_then_upload_is_409(client):
    data = make_payload(200)
    session = await _create(client, "ab.bin", len(data), 100)
    await _put(client, session["upload_id"], 1, data[:100])

    aborted = await client.delete(f"/uploads/{session['upload_id']}")
    assert aborted.status_code == 200
    assert aborted.json()["status"] == "ABORTED"

    retry = await _put(client, session["upload_id"], 1, data[:100])
    assert retry.status_code == 409


async def test_purge_removes_the_record(client):
    session = await _create(client, "pg.bin", 100, 100)

    await client.delete(f"/uploads/{session['upload_id']}?purge=true")

    assert (await client.get(f"/uploads/{session['upload_id']}")).status_code == 404


async def test_list_uploads(client):
    await _create(client, "one.bin", 100, 100)
    await _create(client, "two.bin", 100, 100)

    response = await client.get("/uploads")

    assert {u["filename"] for u in response.json()["uploads"]} == {"one.bin", "two.bin"}


async def test_download_before_completion_is_409(client):
    session = await _create(client, "dl.bin", 100, 100)
    response = await client.get(f"/uploads/{session['upload_id']}/download")
    assert response.status_code == 409


async def test_index_page_is_served(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "Resumable Upload Demo" in response.text
