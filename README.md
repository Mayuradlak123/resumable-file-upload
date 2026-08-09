# resumable-upload

Upload a large file in pieces, and pick up where you left off when the
connection dies.

That's the whole point. No Redis, no Celery, no cloud SDKs — the package itself
imports nothing outside the Python standard library.

```python
manager = UploadManager(storage=LocalStorage("./uploads"))

upload = await manager.create_upload("video.mp4", total_size=file_size)
await manager.upload_chunk(upload.upload_id, 1, first_block)
# ...connection dies, client restarts, hours pass...
await manager.upload_chunk(upload.upload_id, 2, second_block)
final = await manager.complete_upload(upload.upload_id)
```

---

## Table of contents

- [The problem](#the-problem)
- [The mental model](#the-mental-model)
- [Install](#install)
- [The full flow, step by step](#the-full-flow-step-by-step)
- [How resuming actually works](#how-resuming-actually-works)
- [Why sending the same chunk twice is safe](#why-sending-the-same-chunk-twice-is-safe)
- [Checksums](#checksums)
- [What lives on disk](#what-lives-on-disk)
- [Status lifecycle](#status-lifecycle)
- [Over HTTP](#over-http)
- [API reference](#api-reference)
- [Architecture](#architecture)
- [Errors](#errors)
- [Development](#development)
- [Scope and limits](#scope-and-limits)

---

## The problem

Uploading a 4 GB file as one HTTP request is a bet that nothing goes wrong for
several minutes. Lose the connection at 97% and you start over from zero.

The fix is to stop treating the upload as one operation. Cut the file into
numbered chunks, send them independently, and have the server remember which
numbers it already has. A dropped connection then costs you one chunk, not the
whole file.

Everything else in this package exists to make that idea safe: validating that
chunks are the size they claim, verifying they weren't corrupted in transit,
and making retries harmless.

## The mental model

Three ideas carry the entire design.

**1. A chunk is identified by `upload_id + chunk_number`, not by arrival order.**
Chunk 7 is chunk 7 whether it shows up first, last, or three times. The server
writes it to a slot named after its number, so order never matters and a retry
just overwrites its own slot.

**2. The server is the source of truth about progress.**
The client never has to remember what it sent. It asks the server "what do you
still need?" and gets back a list of numbers. This is what makes resuming work
after a crash, a page reload, or a switch to a different device.

**3. The state lives on disk, next to the data.**
Each upload is a directory containing its chunks and a `metadata.json`. There is
no in-memory session table to lose, so restarting the server doesn't invalidate
uploads in flight.

## Install

```bash
pip install resumable-file-upload
```

> The distribution is `resumable-file-upload`; the import is `resumable_upload`.
> They differ because the shorter name was already taken on PyPI by an
> unrelated project.

Working on the source instead:

```bash
uv sync                          # or: pip install -r requirements.txt
```

The library has zero runtime dependencies. FastAPI and uvicorn are only needed
for the demo app in `examples/`.

## The full flow, step by step

Here is a complete upload, from nothing to a finished file, with an interruption
in the middle. This is a runnable script.

```python
import asyncio
from pathlib import Path

from resumable_upload import UploadManager, LocalStorage, sha256_bytes

CHUNK_SIZE = 1024 * 1024  # 1 MiB


async def main():
    manager = UploadManager(storage=LocalStorage("./uploads"))
    source = Path("big-video.mp4")
    data = source.read_bytes()
```

### Step 1 — Open a session

Tell the server what's coming. Nothing is transferred yet; this just reserves an
id and records the plan.

```python
    upload = await manager.create_upload(
        filename=source.name,
        total_size=len(data),
        chunk_size=CHUNK_SIZE,
        content_type="video/mp4",
    )

    print(upload.upload_id)     # '01JXYZ8QW3K5M7N9P0R2T4V6X8' — a sortable ULID
    print(upload.total_chunks)  # e.g. 42
    print(upload.status)        # UploadStatus.PENDING
```

The server computes `total_chunks` from `total_size / chunk_size`, so client and
server agree on the numbering before a single byte moves. Chunks are numbered
**from 1**.

### Step 2 — Send chunks

Each chunk is an independent call. They can go in any order, and in parallel.

```python
    for number in range(1, upload.total_chunks + 1):
        start = (number - 1) * CHUNK_SIZE
        block = data[start : start + CHUNK_SIZE]

        await manager.upload_chunk(
            upload.upload_id,
            number,
            block,
            checksum=sha256_bytes(block),   # optional, but cheap and worth it
        )

        if number == 20:
            raise ConnectionError("network dies here")
```

### Step 3 — Something goes wrong

The process crashes at chunk 20. Chunks 1–20 are already on disk and stay there.
Nothing is rolled back — half an upload is a perfectly valid state.

### Step 4 — Ask what's missing

Later, in a brand-new process, with no memory of what happened:

```python
    manager = UploadManager(storage=LocalStorage("./uploads"))

    status = await manager.get_status(upload_id)
    # {'status': 'UPLOADING',
    #  'total_chunks': 42,
    #  'uploaded_chunks': [1, 2, ..., 20],
    #  'missing_chunks': [21, 22, ..., 42],
    #  'progress': 0.476, ...}
```

### Step 5 — Send only what's missing

```python
    for number in status["missing_chunks"]:
        start = (number - 1) * status["chunk_size"]
        block = data[start : start + status["chunk_size"]]
        await manager.upload_chunk(upload_id, number, block)
```

### Step 6 — Complete

When every chunk has arrived, ask the server to assemble them.

```python
    final = await manager.complete_upload(upload_id)

    print(final.status)      # UploadStatus.COMPLETED
    print(final.final_path)  # './uploads/completed/01JXYZ.../big-video.mp4'
    print(final.checksum)    # SHA-256 of the assembled file

asyncio.run(main())
```

The chunks are concatenated in numeric order into the final file, hashed as they
are written, and the working chunks are deleted. Done.

## How resuming actually works

The interesting part isn't the storage, it's that the client is stateless. The
recovery loop is the same whether you lost one chunk or the entire client
process:

```text
              ┌──────────────────────────────────┐
              │  GET status → missing_chunks     │
              └────────────────┬─────────────────┘
                               ▼
              ┌──────────────────────────────────┐
              │  send those chunk numbers        │
              └────────────────┬─────────────────┘
                               ▼
                     any failures? ──yes──┐
                               │          │
                               no         └──► (loop back up)
                               ▼
              ┌──────────────────────────────────┐
              │  complete → final file           │
              └──────────────────────────────────┘
```

Because progress is stored beside the data rather than in memory, all of these
resume identically:

| What broke | What you do |
| --- | --- |
| One chunk request timed out | Re-send that chunk |
| Client process crashed | Ask for status, send the missing ones |
| **Server** process restarted | Nothing special — state was on disk |
| Browser tab reloaded | Ask for status, send the missing ones |
| Chunks lost from disk after a crash | `repair()`, then send what it reports missing |

That last one is worth explaining. If the metadata says chunk 12 is stored but
the file is gone, `repair()` reconciles the record against what storage actually
holds and drops the phantom entry, so the client is correctly told to resend it:

```python
upload = await manager.repair(upload_id)
```

## Why sending the same chunk twice is safe

This scenario is normal, not exceptional:

```text
client ──── chunk 5 ────► server     server stores chunk 5 ✓
client ◄──✗ response lost ── server
client: "chunk 5 failed, retry"
client ──── chunk 5 ────► server     ...now what?
```

The client cannot tell "the request never arrived" from "the response was lost".
So it retries, and the server must handle a chunk it already has.

`upload_chunk` is **idempotent**. On a repeat it compares the incoming bytes'
digest against the digest recorded for that slot:

- **Same bytes, and the chunk is still in storage** → accepted, nothing is
  written, nothing changes. The retry is free.
- **Different bytes** → the slot is overwritten. This is a corrected chunk, not
  a duplicate.
- **Same bytes, but storage lost the file** → written again. This is precisely
  what repairs a partially lost upload directory.

The upshot: a client can retry any chunk, any number of times, without
coordination and without corrupting anything.

## Checksums

Two independent, optional layers. Both use SHA-256.

**Per chunk** — catches corruption in transit, and catches it *before* anything
touches the disk. A bad chunk is rejected and never stored, so it can't silently
poison the final file:

```python
await manager.upload_chunk(upload_id, 5, block, checksum=sha256_bytes(block))
# raises ChecksumMismatchError if the bytes don't match
```

**Whole file** — catches assembly-level problems. Supply it up front or at the
end:

```python
upload = await manager.create_upload(name, size, checksum=sha256_bytes(data))
# or:
await manager.complete_upload(upload_id, checksum=sha256_bytes(data))
```

The digest is computed while the final file is written, so verification costs no
extra pass over the data. On mismatch the assembled file is **deleted** — no
corrupt file is left where something might serve it — and the upload is marked
`FAILED`.

If you supply no checksum at all, the upload still works; you just lose the
integrity guarantee. The digest of the finished file is recorded either way.

## What lives on disk

```text
uploads/
├── 01JXYZ8QW3K5M7N9P0R2T4V6X8/
│   ├── chunks/
│   │   ├── 000001.part          ← one file per chunk, named by number
│   │   ├── 000002.part
│   │   └── 000003.part
│   └── metadata.json            ← the session state
└── completed/
    └── 01JXYZ8QW3K5M7N9P0R2T4V6X8/
        └── big-video.mp4        ← the assembled file
```

`metadata.json` holds the filename, sizes, status, timestamps, and the digest of
every stored chunk. **This is what makes an upload survive a restart** — the
directory on disk *is* the state, so a fresh process reading the same root picks
up exactly where the old one stopped.

Two details that matter more than they look:

- **Chunk writes are atomic.** Each chunk is written to a temp file, flushed,
  fsynced, then renamed into place. A crash mid-write can't leave a truncated
  chunk that looks complete on resume.
- **Client-supplied names can't escape the directory.** A `filename` of
  `../../etc/passwd` is reduced to `passwd`, and upload ids are validated
  against a strict character set before ever being used as a path.

## Status lifecycle

```text
   create_upload
        │
        ▼
    PENDING ──first chunk──► UPLOADING ──complete──► COMPLETING ──► COMPLETED
                                 │ ▲                      │
                          error  │ │ more chunks          │ error
                                 ▼ │                      │
                              FAILED ◄─────────────────────
                                 │
                                 └── still resumable

   any state ──abort_upload──► ABORTED   (terminal, data discarded)
```

The one thing to internalise: **`FAILED` is not terminal.** It records that
something went wrong, and the upload still accepts chunks. Only `COMPLETED` and
`ABORTED` are final.

| Status | Meaning |
| --- | --- |
| `PENDING` | Session created, no chunk received yet |
| `UPLOADING` | At least one chunk stored, more expected |
| `COMPLETING` | Assembly in progress |
| `COMPLETED` | Final file written and verified |
| `FAILED` | Something went wrong — resumable |
| `ABORTED` | Cancelled, data discarded — terminal |

## Over HTTP

The package knows nothing about HTTP. `examples/fastapi_demo/` is a thin adapter
that translates requests into `UploadManager` calls and exceptions into status
codes — it contains no upload logic of its own, which is the point.

```bash
./run.sh     # or: uv run uvicorn examples.fastapi_demo.main:app --reload
```

The same flow as above, over the wire:

```http
POST /uploads
{"filename": "video.mp4", "total_size": 44040192, "chunk_size": 1048576}

201 → {"upload_id": "01JXYZ...", "total_chunks": 42, "missing_chunks": [1...42]}
```

```http
PUT /uploads/01JXYZ.../chunks/5
Content-Type: application/octet-stream
X-Chunk-Checksum: 3bb46d4fc22df884...

<raw bytes>
```

Chunks go as a raw body rather than multipart — there's no reason to
base64-inflate binary data by a third.

```http
GET /uploads/01JXYZ...
→ {"status": "UPLOADING", "uploaded_chunks": [1,2,3,4],
   "missing_chunks": [5,6,7,8,9,10], "progress": 0.4}

POST /uploads/01JXYZ.../complete
→ {"status": "COMPLETED", "final_path": "...", "checksum": "..."}
```

Full endpoint table, configuration, and error mapping:
[`examples/fastapi_demo/requirements.md`](examples/fastapi_demo/requirements.md).
Interactive docs at `/docs` once running.

> The browser client (`static/upload.html`) is a local testing aid and is not
> tracked in this repo. The API runs fine without it — `GET /` just reports that
> it isn't installed.

## API reference

Everything on `UploadManager` is async.

### Creating and inspecting

| Method | Returns | Notes |
| --- | --- | --- |
| `create_upload(filename, total_size, *, chunk_size=None, content_type=None, checksum=None, upload_id=None, metadata=None)` | `Upload` | `chunk_size` defaults to 5 MiB. `checksum` is the whole-file digest |
| `get_upload(upload_id)` | `Upload` | Raises `UploadNotFoundError` |
| `get_status(upload_id)` | `dict` | Progress payload — what a client polls to resume |
| `list_uploads(status=None)` | `list[Upload]` | Newest first |
| `exists(upload_id)` | `bool` | |

### Transferring

| Method | Returns | Notes |
| --- | --- | --- |
| `upload_chunk(upload_id, chunk_number, data, checksum=None)` | `Upload` | 1-based. Idempotent |
| `missing_chunks(upload_id)` | `list[int]` | What still needs sending |
| `has_chunk(upload_id, chunk_number)` | `bool` | |

### Finishing

| Method | Returns | Notes |
| --- | --- | --- |
| `complete_upload(upload_id, checksum=None)` | `Upload` | Assembles the file. Idempotent |
| `abort_upload(upload_id)` | `Upload` | Discards data, keeps the record as `ABORTED` |
| `delete_upload(upload_id)` | `None` | Erases everything, record included |
| `mark_failed(upload_id, error)` | `Upload` | Records a failure; still resumable |
| `repair(upload_id)` | `Upload` | Re-syncs the record with what storage holds |

### The `Upload` object

```python
upload.upload_id          # '01JXYZ...' — 26-char sortable ULID
upload.filename           # sanitised: no directory components survive
upload.total_size         # bytes
upload.chunk_size         # bytes per chunk
upload.total_chunks       # ceil(total_size / chunk_size); 0 bytes → 1 chunk
upload.status             # UploadStatus enum
upload.uploaded_chunks    # [1, 2, 3] — sorted
upload.missing_chunks     # [4, 5, 6] — sorted
upload.is_complete        # every chunk received?
upload.progress           # 0.0 – 1.0
upload.uploaded_size      # bytes received so far
upload.checksum           # whole-file SHA-256 (set once completed)
upload.final_path         # where the assembled file landed
upload.created_at / .completed_at / .updated_at
upload.metadata           # your own dict[str, str], carried along untouched

upload.expected_chunk_size(n)   # exact size chunk n must be
upload.status_report()          # the dict returned by get_status()
```

### Constructing the manager

```python
UploadManager(
    storage,                            # a Storage implementation
    repository=None,                    # inferred for LocalStorage
    default_chunk_size=5 * 1024**2,
    max_chunk_size=100 * 1024**2,
    cleanup_chunks_on_complete=True,    # delete .part files after assembly
)
```

With `LocalStorage`, the metadata repository is created for you. Any other
backend must be given one explicitly.

## Architecture

```text
              FastAPI demo  ← adapter only; no upload logic
                    │
                    ▼
              UploadManager  ← the engine: validation, idempotency, lifecycle
                 ╱       ╲
                ▼         ▼
          Storage      MetadataRepository      ← interfaces
             │                 │
        LocalStorage   LocalMetadataRepository ← Phase 1 implementations
```

`UploadManager` only ever calls the two interfaces. That's the seam that makes
this extensible: adding S3 means implementing eight methods, and the engine —
all the chunk validation, idempotency, and lifecycle logic — is untouched.

```text
                  Storage (interface)
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   LocalStorage       S3Storage        AzureStorage
     (Phase 1)         (future)          (future)
```

To write a backend, implement:

```python
class MyStorage(Storage):
    async def write_chunk(self, upload_id, chunk_number, data) -> int: ...
    async def read_chunk(self, upload_id, chunk_number) -> bytes: ...
    async def delete_chunk(self, upload_id, chunk_number) -> None: ...
    async def chunk_exists(self, upload_id, chunk_number) -> bool: ...
    async def list_chunks(self, upload_id) -> list[int]: ...
    async def finalize(self, upload) -> FinalizeResult: ...
    async def cleanup_chunks(self, upload_id) -> None: ...
    async def delete_upload(self, upload_id) -> None: ...
```

`finalize` returns the location, size, and SHA-256 of the assembled object —
computing the digest during assembly, so no backend needs a second pass.

Metadata is a separate interface on purpose: a real deployment might keep chunks
on S3 while keeping session state in Postgres. Implement `MetadataRepository`
(`create`, `save`, `get`, `find`, `delete`, `list`) and pass it in.

## Errors

Every exception derives from `ResumableUploadError`, so one `except` clause
catches the lot.

| Exception | When | HTTP (demo) |
| --- | --- | --- |
| `UploadNotFoundError` | Unknown `upload_id` | 404 |
| `UploadAlreadyExistsError` | Explicit `upload_id` collides | 409 |
| `InvalidUploadStateError` | e.g. chunk sent to a `COMPLETED` upload | 409 |
| `IncompleteUploadError` | `complete_upload` while chunks are missing — carries `.missing_chunks` | 409 |
| `ChecksumMismatchError` | Digest mismatch — carries `.expected`, `.actual`, `.chunk_number` | 422 |
| `InvalidChunkError` | Bad chunk number, wrong chunk size, bad parameters | 400 |
| `StorageError` | The backend failed | 500 |

Note that `IncompleteUploadError` tells you exactly which chunks are missing, so
a failed completion is directly actionable rather than something to investigate.

## Development

```bash
./setup.sh        # installs uv if needed, syncs deps, copies .env, runs tests
```

Or step by step:

```bash
uv sync
uv run pytest     # 89 tests
./run.sh          # start the demo on http://127.0.0.1:8000/
```

### Test layout

| File | Covers |
| --- | --- |
| `test_upload.py` | Session creation, happy path, completion, abort, delete |
| `test_chunks.py` | Size/number validation, out-of-order arrival, idempotent replay, concurrency |
| `test_resume.py` | Resuming after failure, **across a manager restart**, `repair()` |
| `test_checksum.py` | SHA-256 helpers, chunk and whole-file enforcement |
| `test_storage.py` | Storage layout, atomicity, path-traversal defence, the repository |
| `test_api.py` | The HTTP layer and its error mapping |

### Configuration

The package takes **no** configuration from the environment — everything is an
explicit `UploadManager` argument. Only the demo reads `.env`:

```bash
cp .env.example .env
```

See [`.env.example`](.env.example) for the full list.

## Scope and limits

Phase 1 does one thing well. Deliberately **not** included:

- Authentication, authorisation, per-user quotas
- Upload expiry or garbage collection of abandoned sessions
- Rate limiting
- Cloud storage backends (the interface is ready; the implementations aren't)
- Distributed coordination — the per-upload lock is in-process, so it serialises
  concurrent chunks within **one** server process, not across a cluster

Two practical notes:

- **A chunk is held in memory whole.** Chunks aren't streamed to disk, so
  `max_chunk_size` (100 MiB by default) is a real memory bound. A few MiB per
  chunk is the sweet spot.
- **Multiple processes on one directory** aren't safe against interleaved writes
  to the *same* upload. Different uploads are fine.

The design principle behind all of this:

> Do not introduce distributed infrastructure until the requirements actually
> demand it.

The abstractions are placed so the path from local filesystem → S3/Azure →
distributed storage doesn't require rewriting the upload engine.

## License

MIT — see [LICENSE](LICENSE).
