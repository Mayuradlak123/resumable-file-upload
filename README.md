# resumable-upload

A lightweight Python package for receiving large files in chunks and resuming
interrupted uploads without starting over.

Phase 1 solves exactly one problem well:

> **Upload a large file in chunks and resume it after failure.**

No Redis, no Celery, no cloud storage — just the standard library, the local
filesystem, and a clean abstraction so S3/Azure/GCS can be added later without
rewriting the upload engine.

---

## Install

```bash
uv sync
```

## Quick start

```python
import asyncio
from resumable_upload import UploadManager
from resumable_upload.storage import LocalStorage

async def main():
    manager = UploadManager(storage=LocalStorage("./uploads"))

    data = b"x" * 12_000
    upload = await manager.create_upload(
        filename="video.mp4",
        total_size=len(data),
        chunk_size=5_000,          # -> total_chunks == 3
        content_type="video/mp4",
    )

    for number in upload.missing_chunks:
        start = (number - 1) * upload.chunk_size
        await manager.upload_chunk(
            upload.upload_id,
            number,
            data[start : start + upload.expected_chunk_size(number)],
        )

    done = await manager.complete_upload(upload.upload_id)
    print(done.status, done.final_path, done.checksum)

asyncio.run(main())
```

## How resuming works

The server is the source of truth for what it has received. After a failure the
client asks for the session state and re-sends only what is missing:

```python
status = await manager.get_status(upload_id)
# {'status': 'UPLOADING', 'total_chunks': 10,
#  'uploaded_chunks': [1, 2, 3, 4], 'missing_chunks': [5, 6, 7, 8, 9, 10], ...}
```

Because every chunk is addressed by `upload_id + chunk_number`, re-sending a
chunk is **idempotent** — a chunk whose bytes are already stored is accepted and
changes nothing, which is what makes retries after a lost response safe.

## Checksums

Every chunk may carry an optional SHA-256. It is verified *before* the bytes are
written, so a corrupt chunk never lands on disk:

```python
from resumable_upload import sha256_bytes

await manager.upload_chunk(upload_id, 1, block, checksum=sha256_bytes(block))
```

A whole-file SHA-256 can be passed to `create_upload(..., checksum=...)` or to
`complete_upload(upload_id, checksum=...)`. It is checked against the digest
computed while the final file is assembled; on mismatch the assembled file is
discarded and the upload is marked `FAILED`.

## API

| Method | Purpose |
| --- | --- |
| `create_upload(filename, total_size, *, chunk_size, content_type, checksum, upload_id, metadata)` | Open a session, returns `Upload` with a ULID `upload_id` |
| `upload_chunk(upload_id, chunk_number, data, checksum=None)` | Validate + store one 1-based chunk (idempotent) |
| `get_upload(upload_id)` / `get_status(upload_id)` | Session object / progress payload for resuming |
| `missing_chunks(upload_id)` | Chunk numbers still needed |
| `complete_upload(upload_id, checksum=None)` | Assemble chunks into the final file (idempotent) |
| `abort_upload(upload_id)` | Discard data, keep the record as `ABORTED` |
| `delete_upload(upload_id)` | Erase everything, record included |
| `repair(upload_id)` | Re-sync the record with what storage actually holds |
| `list_uploads(status=None)` | All sessions, newest first |

### Status lifecycle

```text
PENDING -> UPLOADING -> COMPLETING -> COMPLETED
              |  ^
              v  |
            FAILED          (resumable)

  any -> ABORTED            (terminal)
```

## Storage layout

```text
uploads/
├── {upload_id}/
│   ├── chunks/
│   │   ├── 000001.part
│   │   └── 000002.part
│   └── metadata.json
└── completed/
    └── {upload_id}/
        └── original-file.mp4
```

Metadata lives next to the chunks, so an upload survives a server restart: the
directory on disk *is* the state.

## Architecture

```text
        FastAPI demo (examples/)      <- adapter only, no upload logic
                  |
             UploadManager            <- the engine
             /          \
       Storage        MetadataRepository
          |                  |
     LocalStorage    LocalMetadataRepository
```

`UploadManager` talks only to the `Storage` and `MetadataRepository`
interfaces, so adding `S3Storage` later means implementing four methods — the
engine itself does not change.

## Development

```bash
./setup.sh              # installs uv if needed, syncs deps, copies .env, runs tests
```

or step by step:

```bash
uv sync                 # create the environment
uv run pytest           # run the tests
./run.sh                # or: uv run uvicorn examples.fastapi_demo.main:app --reload
```

Then open <http://127.0.0.1:8000/> for the browser demo, which lets you pause,
kill, and resume an upload — including across a page reload — to see the flow
end to end. API docs are at `/docs`.

### Configuration

The package takes **no** configuration from the environment; everything is an
explicit `UploadManager` argument. Only the demo reads `.env`:

```bash
cp .env.example .env
```

See [`.env.example`](.env.example) for every variable, and
[`examples/fastapi_demo/requirements.md`](examples/fastapi_demo/requirements.md)
for the endpoint reference.

### Layout

```text
src/resumable_upload/
├── core/          manager.py, models.py, checksum.py, exceptions.py
├── storage/       base.py (interface), local.py (filesystem)
└── repository/    base.py (interface), local.py (metadata.json)
tests/             89 tests: upload, chunks, resume, checksum, storage, api
examples/          FastAPI adapter + browser client
```

## License

MIT — see [LICENSE](LICENSE).
