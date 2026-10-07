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
"""

_COLUMNS = "ts, app, window_title, idle_seconds"


def _to_sample(row) -> Sample:
    return Sample(ts=row[0], app=row[1], window_title=row[2], idle_seconds=row[3])


class ActivityStore:
    def __init__(self, path: Path | str | None = None):
        # Resolved at call time so tests (and later config) can redirect it.
        path = Path(path) if path is not None else config.DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=5)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def add(self, sample: Sample) -> None:
        self._db.execute(
            f"INSERT INTO samples ({_COLUMNS}) VALUES (?, ?, ?, ?)",
            (sample.ts, sample.app, sample.window_title, sample.idle_seconds),
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

    def close(self) -> None:
        self._db.close()
