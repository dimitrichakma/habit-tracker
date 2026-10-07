"""Paths. Raw activity data lives outside the repo, never inside it."""

import os
from pathlib import Path

DATA_DIR = Path.home() / "Library" / "Application Support" / "FocusTracker"
DB_PATH = DATA_DIR / "activity.db"

SAMPLE_INTERVAL_SECONDS = 5
IDLE_THRESHOLD_SECONDS = 120

# Ollama is localhost only (enforced in categorize/ollama_classifier.py). The
# OLLAMA_HOST environment variable is deliberately NOT read.
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b-mlx")
OLLAMA_TIMEOUT_SECONDS = 30  # the first call loads the model
