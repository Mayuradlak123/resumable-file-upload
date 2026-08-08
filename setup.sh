#!/bin/bash
set -e

echo "Setting up resumable-upload..."

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found; installing it with pip..."
    python -m pip install --upgrade pip
    python -m pip install uv
fi

echo "Creating the environment and installing dependencies..."
uv sync

if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
fi

echo "Running the test suite..."
uv run pytest

echo ""
echo "Setup complete."

read -p "Do you want to start the demo now? (y/n): " choice
if [[ $choice == "y" || $choice == "Y" ]]; then
    ./run.sh
else
    echo "You can start it later with ./run.sh"
fi
