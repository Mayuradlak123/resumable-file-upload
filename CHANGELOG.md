# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-08-08

First release. Phase 1 scope: **upload a large file in chunks and resume it
after failure.**

### Added

- **`UploadManager`** — the public API: create sessions, receive and validate
  chunks, track progress, resume, complete, abort, repair.
- **Resumable by design** — the server records which chunk numbers it holds, so
  a client asks what's missing and re-sends only that. Survives client crashes,
  server restarts, and page reloads alike.
- **Idempotent chunk writes** — keyed on `upload_id + chunk_number`. Replaying a
  chunk that is already stored is a no-op; one whose bytes storage lost is
  rewritten.
- **SHA-256 validation** — optional per chunk, verified before anything is
  written, plus an optional whole-file digest checked during assembly.
- **`LocalStorage`** — filesystem backend. Chunk and metadata writes are atomic
  (temp file + fsync + rename), so a crash cannot leave a truncated chunk that
  looks complete.
- **`LocalMetadataRepository`** — session state as `metadata.json` beside the
  chunks, which is what lets an upload outlive the process.
- **`Storage` and `MetadataRepository` interfaces** — the seam for future S3 /
  Azure / GCS backends; `UploadManager` does not change when one is added.
- **ULID upload ids** — 26 characters, lexicographically sortable, no dependency.
- **FastAPI demo** (`examples/fastapi_demo/`) — a pure adapter that maps HTTP to
  manager calls and exceptions to status codes, with no upload logic of its own.
- **89 tests** covering chunk validation, out-of-order arrival, idempotent
  replay, resume across a manager restart, checksum enforcement, storage
  layout and path-traversal defence, and the HTTP layer.

### Notes

- Published as **`resumable-file-upload`** on PyPI; the import name is
  `resumable_upload`. The shorter distribution name was already claimed by an
  unrelated project.
- The package has **zero runtime dependencies** — standard library only.
  FastAPI and uvicorn are needed for the demo, not the library.
- Requires Python 3.10+.

### Not included

Authentication, quotas, upload expiry / garbage collection, rate limiting, and
cloud storage backends. The per-upload lock is in-process, so it serialises
concurrent chunks within one server process, not across a cluster. Chunks are
buffered in memory rather than streamed, which is what `max_chunk_size` bounds.

[0.1.0]: https://github.com/Mayuradlak123/resumable-upload/releases/tag/v0.1.0
