# FastAPI Demo — Requirements

This demo is an **adapter**, not part of the library. It exists to exercise the
upload flow from a browser; it contains no upload business logic of its own.

## Dependencies

| Package | Why |
| --- | --- |
| `fastapi` | HTTP routing and request/response validation |
| `uvicorn` | ASGI server |
| `python-dotenv` | Loads `.env` (optional — the app runs fine without it) |

The `resumable_upload` package itself needs **none** of these.

```bash
uv sync                      # installs the dev group, which includes them
# or, with pip:
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` at the repo root to `.env`. Every value has a working
default, so the demo runs with no configuration at all.

| Variable | Default | Meaning |
| --- | --- | --- |
| `UPLOAD_DIR` | `examples/fastapi_demo/uploads` | Where chunks and completed files live |
| `DEFAULT_CHUNK_SIZE` | `5242880` | Chunk size when the client does not send one |
| `MAX_CHUNK_SIZE` | `104857600` | Largest chunk a client may request |
| `CLEANUP_CHUNKS_ON_COMPLETE` | `true` | Delete chunk files after assembly |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |

## Run

```bash
uv run uvicorn examples.fastapi_demo.main:app --reload
```

Open <http://127.0.0.1:8000/> for the browser client.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/uploads` | Create a session; returns `upload_id` and `total_chunks` |
| `PUT` | `/uploads/{upload_id}/chunks/{chunk_number}` | Send one chunk as the raw body |
| `GET` | `/uploads/{upload_id}` | Progress: `uploaded_chunks` + `missing_chunks` |
| `GET` | `/uploads` | List sessions (optional `?status=UPLOADING`) |
| `POST` | `/uploads/{upload_id}/complete` | Assemble the final file |
| `POST` | `/uploads/{upload_id}/repair` | Re-sync the record with what is on disk |
| `DELETE` | `/uploads/{upload_id}` | Abort (`?purge=true` erases the record too) |
| `GET` | `/uploads/{upload_id}/download` | Download the assembled file |
| `GET` | `/health` | Liveness probe |

Interactive API docs: <http://127.0.0.1:8000/docs>.

### Chunk requests

Chunks are sent as a **raw body**, not multipart — there is no reason to
base64-inflate binary data:

```http
PUT /uploads/01JXYZ.../chunks/5
Content-Type: application/octet-stream
X-Chunk-Checksum: <sha256 hex of this chunk>   # optional

<raw bytes>
```

`X-Chunk-Checksum` is verified *before* anything is written, so a corrupt chunk
never reaches disk. Re-sending a chunk that is already stored is a no-op.

## Error mapping

Package exceptions are translated to HTTP by a single exception handler:

| Exception | Status |
| --- | --- |
| `UploadNotFoundError` | 404 |
| `UploadAlreadyExistsError`, `InvalidUploadStateError`, `IncompleteUploadError` | 409 |
| `ChecksumMismatchError` | 422 |
| `InvalidChunkError` | 400 |
| `StorageError` | 500 |

## Not production-ready

Deliberately out of scope for Phase 1: authentication, per-user quotas, upload
expiry/garbage collection, rate limiting, and streaming chunk bodies to disk
instead of buffering them in memory (a chunk is held whole, which is why
`MAX_CHUNK_SIZE` matters).
