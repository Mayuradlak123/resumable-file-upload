"""FastAPI adapter for the resumable-upload package.

This app is a *demo*, not part of the library. It only translates HTTP into
``UploadManager`` calls — there is deliberately no upload logic here.

Run it with::

    uv run uvicorn examples.fastapi_demo.main:app --reload
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Header, Path as PathParam, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from resumable_upload import (
    ChecksumMismatchError,
    IncompleteUploadError,
    InvalidChunkError,
    InvalidUploadStateError,
    LocalStorage,
    ResumableUploadError,
    StorageError,
    UploadAlreadyExistsError,
    UploadManager,
    UploadNotFoundError,
    UploadStatus,
)

try:  # Optional: load a local .env if python-dotenv is installed.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# ----------------------------------------------------------------------
# Configuration (see .env.example)
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))
DEFAULT_CHUNK_SIZE = int(os.getenv("DEFAULT_CHUNK_SIZE", 5 * 1024 * 1024))
MAX_CHUNK_SIZE = int(os.getenv("MAX_CHUNK_SIZE", 100 * 1024 * 1024))
CLEANUP_CHUNKS = os.getenv("CLEANUP_CHUNKS_ON_COMPLETE", "true").lower() == "true"
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", 8000))

manager = UploadManager(
    storage=LocalStorage(UPLOAD_DIR),
    default_chunk_size=DEFAULT_CHUNK_SIZE,
    max_chunk_size=MAX_CHUNK_SIZE,
    cleanup_chunks_on_complete=CLEANUP_CHUNKS,
)

app = FastAPI(
    title="Resumable Upload Demo",
    description="Chunked, resumable uploads over HTTP.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ----------------------------------------------------------------------
# Error translation: package exception -> HTTP status
# ----------------------------------------------------------------------
_STATUS_CODES: list[tuple[type[ResumableUploadError], int]] = [
    (UploadNotFoundError, 404),
    (UploadAlreadyExistsError, 409),
    (InvalidUploadStateError, 409),
    (IncompleteUploadError, 409),
    (ChecksumMismatchError, 422),
    (InvalidChunkError, 400),
    (StorageError, 500),
]


@app.exception_handler(ResumableUploadError)
async def upload_error_handler(request: Request, exc: ResumableUploadError):
    status = next(
        (code for kind, code in _STATUS_CODES if isinstance(exc, kind)), 400
    )
    body: dict = {"error": type(exc).__name__, "detail": str(exc)}
    if isinstance(exc, IncompleteUploadError):
        body["missing_chunks"] = exc.missing_chunks
    if isinstance(exc, ChecksumMismatchError):
        body["expected"] = exc.expected
        body["actual"] = exc.actual
    return JSONResponse(status_code=status, content=body)


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------
class CreateUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1)
    total_size: int = Field(..., ge=0)
    chunk_size: int | None = Field(None, ge=1)
    content_type: str | None = None
    checksum: str | None = Field(None, description="Optional SHA-256 of the whole file")
    metadata: dict[str, str] | None = None


class CompleteUploadRequest(BaseModel):
    checksum: str | None = None


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------
@app.post("/uploads", status_code=201)
async def create_upload(payload: CreateUploadRequest):
    """Open an upload session and hand the client its ``upload_id``."""
    upload = await manager.create_upload(
        filename=payload.filename,
        total_size=payload.total_size,
        chunk_size=payload.chunk_size,
        content_type=payload.content_type,
        checksum=payload.checksum,
        metadata=payload.metadata,
    )
    return upload.status_report()


@app.put("/uploads/{upload_id}/chunks/{chunk_number}")
async def upload_chunk(
    request: Request,
    upload_id: str,
    chunk_number: int = PathParam(..., ge=1),
    x_chunk_checksum: str | None = Header(None),
):
    """Receive one chunk as a raw request body.

    ``X-Chunk-Checksum`` optionally carries the SHA-256 of the chunk.
    Re-sending a chunk is safe: the operation is idempotent.
    """
    data = await request.body()
    upload = await manager.upload_chunk(
        upload_id, chunk_number, data, checksum=x_chunk_checksum
    )
    return {
        "upload_id": upload.upload_id,
        "chunk_number": chunk_number,
        "received": len(data),
        "status": upload.status.value,
        "uploaded_chunks": upload.uploaded_chunks,
        "missing_chunks": upload.missing_chunks,
        "progress": round(upload.progress, 6),
    }


@app.get("/uploads")
async def list_uploads(status: UploadStatus | None = None):
    """All sessions, newest first — the demo's dashboard."""
    uploads = await manager.list_uploads(status)
    return {"uploads": [u.status_report() for u in uploads]}


@app.get("/uploads/{upload_id}")
async def get_upload(upload_id: str):
    """Progress of a session: this is what a client polls to resume."""
    return await manager.get_status(upload_id)


@app.post("/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, payload: CompleteUploadRequest | None = None):
    """Assemble the chunks into the final file."""
    upload = await manager.complete_upload(
        upload_id, checksum=payload.checksum if payload else None
    )
    return upload.status_report()


@app.delete("/uploads/{upload_id}", status_code=200)
async def abort_upload(upload_id: str, purge: bool = False):
    """Abort an upload. ``?purge=true`` erases the record as well."""
    if purge:
        await manager.delete_upload(upload_id)
        return {"upload_id": upload_id, "status": "DELETED"}
    upload = await manager.abort_upload(upload_id)
    return upload.status_report()


@app.post("/uploads/{upload_id}/repair")
async def repair_upload(upload_id: str):
    """Re-sync the session with what is actually on disk (post-crash helper)."""
    upload = await manager.repair(upload_id)
    return upload.status_report()


@app.get("/uploads/{upload_id}/download")
async def download_upload(upload_id: str):
    """Serve the assembled file, so the demo can prove the bytes round-tripped."""
    upload = await manager.get_upload(upload_id)
    if upload.status is not UploadStatus.COMPLETED or not upload.final_path:
        raise InvalidUploadStateError(upload_id, upload.status.value, "COMPLETED")
    return FileResponse(
        upload.final_path,
        filename=upload.filename,
        media_type=upload.content_type or "application/octet-stream",
    )


@app.get("/health")
async def health():
    return {"status": "ok", "upload_dir": str(UPLOAD_DIR)}


@app.get("/", include_in_schema=False)
async def index():
    """The browser client used to exercise the upload flow."""
    return FileResponse(BASE_DIR / "static" / "upload.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("examples.fastapi_demo.main:app", host=HOST, port=PORT, reload=True)
