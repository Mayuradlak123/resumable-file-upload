#!/bin/bash
set -e

# Load environment variables if a .env is present.
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "Starting the resumable upload demo on http://${HOST}:${PORT}/"
python -m uv run uvicorn examples.fastapi_demo.main:app --host "$HOST" --port "$PORT" --reload
