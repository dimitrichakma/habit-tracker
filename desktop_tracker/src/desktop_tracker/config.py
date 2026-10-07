"""Paths. Raw activity data lives outside the repo, never inside it."""

from pathlib import Path

DATA_DIR = Path.home() / "Library" / "Application Support" / "FocusTracker"
DB_PATH = DATA_DIR / "activity.db"
