# Resumable Upload Library — Phase 1

## Goal

Build a lightweight Python package that allows a backend to receive large files in chunks and resume interrupted uploads without restarting from the beginning.

Phase 1 will use:

- Python
- `uv` for package/dependency management
- FastAPI for the demo/integration layer
- Local filesystem for storage
- No Redis
- No Celery/ARQ
- No S3/Azure/GCS
- No frontend inside the package

The frontend is only required for testing the upload flow.

---

## Architecture

```text
                Test Client / Frontend
                         |
                         | HTTP
                         v
                  FastAPI Demo App
                         |
                         v
              Resumable Upload Package
                         |
              +----------+----------+
              |                     |
              v                     v
        Upload Manager          Local Storage
              |                     |
              v                     v
        Upload Metadata        Local Filesystem
```

The package must remain independent of FastAPI.

FastAPI should only act as an adapter/example application.

---

# Project Structure

```text
resumable-upload/
│
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
├── .gitignore
│
├── src/
│   └── resumable_upload/
│       │
│       ├── __init__.py
│       │
│       ├── core/
│       │   ├── __init__.py
│       │   ├── manager.py
│       │   ├── models.py
│       │   ├── exceptions.py
│       │   └── checksum.py
│       │
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   └── local.py
│       │
│       └── repository/
│           ├── __init__.py
│           └── base.py
│
├── tests/
│   ├── test_upload.py
│   ├── test_chunks.py
│   ├── test_resume.py
│   ├── test_checksum.py
│   └── test_storage.py
│
└── examples/
    └── fastapi_demo/
        ├── main.py
        ├── requirements.md
        └── static/
            └── upload.html
```

---

# Core Components

## 1. UploadManager

The main public API of the package.

Responsibilities:

- Create upload sessions
- Receive chunks
- Validate chunks
- Track upload progress
- Resume uploads
- Detect duplicate chunks
- Complete uploads
- Abort uploads

Example:

```python
from resumable_upload import UploadManager
from resumable_upload.storage import LocalStorage

storage = LocalStorage("./uploads")

manager = UploadManager(storage=storage)
```

---

## 2. Upload Session

Every upload receives a unique `upload_id`.

Example:

```text
upload_id = "01JXYZ..."
```

Metadata:

```text
Upload
├── upload_id
├── filename
├── content_type
├── total_size
├── chunk_size
├── total_chunks
├── uploaded_chunks
├── status
├── created_at
└── completed_at
```

Possible statuses:

```text
PENDING
UPLOADING
COMPLETING
COMPLETED
FAILED
ABORTED
```

---

# 3. Chunk Upload

The client sends chunks independently.

Example API:

```http
PUT /uploads/{upload_id}/chunks/{chunk_number}
```

The package receives:

```text
upload_id
chunk_number
chunk_data
checksum
```

The package:

```text
Receive Chunk
      ↓
Validate Upload
      ↓
Validate Chunk Number
      ↓
Calculate Checksum
      ↓
Compare Checksum
      ↓
Check Duplicate
      ↓
Write Chunk
      ↓
Mark Chunk Uploaded
```

---

# 4. Resume

If an upload fails:

```text
Chunk 1 ✓
Chunk 2 ✓
Chunk 3 ✓
Chunk 4 ✓
Chunk 5 ✗
Chunk 6 ✗
```

The client can request:

```http
GET /uploads/{upload_id}
```

Response:

```json
{
  "upload_id": "abc123",
  "status": "UPLOADING",
  "total_chunks": 10,
  "uploaded_chunks": [1, 2, 3, 4],
  "missing_chunks": [5, 6, 7, 8, 9, 10]
}
```

The client uploads only the missing chunks.

---

# 5. Idempotency

The same chunk may arrive more than once.

Example:

```text
Client
  |
  | Chunk 5
  v
Server
  |
  X response lost
  |
Client thinks upload failed
  |
  | Chunk 5 again
  v
Server
```

The package must recognize:

```text
upload_id + chunk_number
```

and avoid creating duplicate data.

The operation should be idempotent.

---

# 6. Checksum Validation

Every chunk can optionally contain a checksum.

Example:

```text
SHA-256(chunk)
```

Flow:

```text
Client checksum
       |
       v
Server receives chunk
       |
       v
Server calculates checksum
       |
       v
Compare
   /       \
 MATCH    DIFFERENT
   |          |
   v          v
 Store       Reject
```

Phase 1 should support SHA-256.

---

# 7. Local Storage

Phase 1 only supports local filesystem storage.

Example:

```text
uploads/
│
├── {upload_id}/
│   ├── chunks/
│   │   ├── 000001.part
│   │   ├── 000002.part
│   │   └── 000003.part
│   │
│   └── metadata.json
│
└── completed/
    └── {upload_id}/
        └── original-file.mp4
```

Storage must be accessed through an abstraction.

```python
class Storage:
    async def write_chunk(...):
        ...

    async def read_chunk(...):
        ...

    async def delete_chunk(...):
        ...

    async def finalize(...):
        ...
```

Implementation:

```python
class LocalStorage(Storage):
    ...
```

---

# Future Storage Architecture

Do not implement cloud storage in Phase 1.

The architecture should allow:

```text
              Storage Interface
                     |
        +------------+------------+
        |            |            |
        v            v            v
   LocalStorage   S3Storage   AzureStorage
```

Future versions may support:

- AWS S3
- Azure Blob Storage
- Google Cloud Storage

The core `UploadManager` should not need to change.

---

# FastAPI Integration

FastAPI is only an adapter/demo.

Example:

```text
examples/
└── fastapi_demo/
    ├── main.py
    └── static/
        └── upload.html
```

Possible endpoints:

```http
POST   /uploads
PUT    /uploads/{upload_id}/chunks/{chunk_number}
GET    /uploads/{upload_id}
POST   /uploads/{upload_id}/complete
DELETE /uploads/{upload_id}
```

FastAPI should translate HTTP requests into calls to `UploadManager`.

It should not contain upload business logic.

---

# Example Flow

```text
1. Client creates upload
          ↓
2. Server generates upload_id
          ↓
3. Client splits file into chunks
          ↓
4. Client uploads chunks
          ↓
5. Server validates and stores chunks
          ↓
6. Network failure
          ↓
7. Client requests upload status
          ↓
8. Server returns uploaded chunks
          ↓
9. Client uploads missing chunks
          ↓
10. Client requests completion
          ↓
11. Server validates all chunks
          ↓
12. Server combines chunks
          ↓
13. Final file created
          ↓
14. Upload marked COMPLETED
```

---

# Development Setup

Initialize with `uv`:

```bash
uv init
```

Create the package environment:

```bash
uv sync
```

Add runtime dependencies only when required:

```bash
uv add fastapi uvicorn
```

Add development dependencies:

```bash
uv add --dev pytest pytest-asyncio
```

Run tests:

```bash
uv run pytest
```

Run FastAPI demo:

```bash
uv run uvicorn examples.fastapi_demo.main:app --reload
```

---

# Phase 1 Scope

### Must Have

- Upload session creation
- Unique upload ID
- Chunk upload
- Local filesystem storage
- Chunk tracking
- Resume support
- Duplicate chunk handling
- SHA-256 checksum
- Upload completion
- Failed upload recovery
- Unit tests
- FastAPI demo

---

# Design Principle

The first version should solve one problem well:

> **Upload a large file in chunks and resume it after failure.**

Do not introduce distributed infrastructure until the requirements actually demand it.

The package should have a clean core abstraction so that future versions can evolve from:

```text
Local Filesystem
      ↓
S3 / Azure
      ↓
Distributed Storage
```

without rewriting the upload engine.

for frotnend just create page html and tailwidn for UI not will be the part of library
