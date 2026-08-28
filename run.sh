#!/usr/bin/env bash
# Starts the FastAPI backend and Streamlit frontend together.
# Usage: ./run.sh   (Ctrl+C stops both)
set -eu

cd "$(dirname "$0")"

# Free the ports in case a previous run is still lingering.
lsof -ti:8000 | xargs kill -9 2>/dev/null || true
lsof -ti:8501 | xargs kill -9 2>/dev/null || true

cleanup() {
    lsof -ti:8000 | xargs kill -9 2>/dev/null || true
    lsof -ti:8501 | xargs kill -9 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uv run uvicorn src.main:app --reload --reload-exclude 'src/app.py' &
uv run streamlit run src/app.py &

wait
