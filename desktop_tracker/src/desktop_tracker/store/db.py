"""SQLite store: one raw, append-only `samples` table. Idle is derived at query time."""

import sqlite3
from pathlib import Path

from .. import config
from ..sampler.base import Sample

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id           INTEGER PRIMARY KEY,
    ts           INTEGER NOT NULL,  -- UTC unix epoch, whole seconds
    app          TEXT    NOT NULL,
    window_title TEXT,              -- NULL = unavailable
    idle_seconds REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS category_cache (
    kind     TEXT NOT NULL,         -- 'app' or 'site'
    name     TEXT NOT NULL,         -- lowercased app name or URL host
    category TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);
"""

_COLUMNS = "ts, app, window_title, idle_seconds, site"


def _to_sample(row) -> Sample:
    return Sample(
        ts=row[0], app=row[1], window_title=row[2], idle_seconds=row[3], site=row[4]
    )


class ActivityStore:
    def __init__(self, path: Path | str | None = None):
        # Resolved at call time so tests (and later config) can redirect it.
        path = Path(path) if path is not None else config.DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=5)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Additive and idempotent: older databases gain the `site` column, rows kept."""
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(samples)")}
        if "site" not in cols:
            self._db.execute("ALTER TABLE samples ADD COLUMN site TEXT")

    def add(self, sample: Sample) -> None:
        self._db.execute(
            f"INSERT INTO samples ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (sample.ts, sample.app, sample.window_title, sample.idle_seconds, sample.site),
        )
        self._db.commit()

    def rows(self) -> list[Sample]:
        cur = self._db.execute(f"SELECT {_COLUMNS} FROM samples ORDER BY ts, id")
        return [_to_sample(r) for r in cur]

    def between(self, start: int, end: int) -> list[Sample]:
        """Samples with start <= ts < end (UTC epoch seconds)."""
        cur = self._db.execute(
            f"SELECT {_COLUMNS} FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts, id",
            (start, end),
        )
        return [_to_sample(r) for r in cur]

    def get_category(self, kind: str, name: str) -> str | None:
        row = self._db.execute(
            "SELECT category FROM category_cache WHERE kind = ? AND name = ?", (kind, name)
        ).fetchone()
        return row[0] if row else None

    def set_category(self, kind: str, name: str, category: str) -> None:
        self._db.execute(
            "INSERT INTO category_cache (kind, name, category) VALUES (?, ?, ?) "
            "ON CONFLICT(kind, name) DO UPDATE SET category = excluded.category",
            (kind, name, category),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
