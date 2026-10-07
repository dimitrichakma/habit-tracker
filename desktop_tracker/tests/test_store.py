import sqlite3
import time
from datetime import datetime, timezone

from desktop_tracker import config
from desktop_tracker.sampler.base import Sample
from desktop_tracker.store import ActivityStore


def fake(ts=1_700_000_000, app="Safari", title="Docs", idle=1.5) -> Sample:
    return Sample(ts=ts, app=app, window_title=title, idle_seconds=idle)


def test_creates_db_file_and_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "FocusTracker" / "activity.db"
    ActivityStore(path).close()
    assert path.exists()


def test_add_then_read_back_is_identical(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    original = fake()
    store.add(original)
    assert store.rows() == [original]
    store.close()


def test_rows_come_back_in_time_order(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    for ts in (300, 100, 200):
        store.add(fake(ts=ts))
    assert [r.ts for r in store.rows()] == [100, 200, 300]
    store.close()


def test_none_title_round_trips_as_null_not_empty_string(tmp_path):
    path = tmp_path / "a.db"
    store = ActivityStore(path)
    store.add(fake(title=None))
    store.add(fake(ts=1_700_000_005, title=""))
    store.close()

    raw = sqlite3.connect(path).execute(
        "SELECT window_title FROM samples ORDER BY ts"
    ).fetchall()
    assert raw == [(None,), ("",)]


def test_between_is_start_inclusive_end_exclusive(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    for ts in (100, 200, 300):
        store.add(fake(ts=ts))
    assert [r.ts for r in store.between(100, 300)] == [100, 200]
    store.close()


def test_ts_is_stored_as_utc_epoch_integer_seconds(tmp_path, monkeypatch):
    # Pins the format: UTC unix epoch, INTEGER seconds. Not a float, not ISO text,
    # and independent of the machine's local timezone.
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        path = tmp_path / "a.db"
        store = ActivityStore(path)
        store.add(fake(ts=1_700_000_000))
        store.close()

        value, kind = sqlite3.connect(path).execute(
            "SELECT ts, typeof(ts) FROM samples"
        ).fetchone()
        assert (value, kind) == (1_700_000_000, "integer")
        # 1_700_000_000 is 2023-11-14T22:13:20Z; reading it as UTC gives exactly that.
        as_utc = datetime.fromtimestamp(value, tz=timezone.utc)
        assert as_utc.isoformat() == "2023-11-14T22:13:20+00:00"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_schema_has_required_columns_and_no_is_idle(tmp_path):
    path = tmp_path / "a.db"
    ActivityStore(path).close()
    db = sqlite3.connect(path)
    cols = {row[1] for row in db.execute("PRAGMA table_info(samples)")}
    # Extra columns may be added later; these must always exist.
    assert {"id", "ts", "app", "window_title", "idle_seconds"} <= cols
    # Idle is derived from idle_seconds at query time, never stored.
    assert "is_idle" not in cols
    indexed = {
        col
        for (_, name, *_rest) in db.execute("PRAGMA index_list(samples)")
        for col in [r[2] for r in db.execute(f"PRAGMA index_info({name})")]
    }
    assert "ts" in indexed


def test_reopening_keeps_rows_and_init_is_idempotent(tmp_path):
    path = tmp_path / "a.db"
    first = ActivityStore(path)
    first.add(fake(ts=1))
    first.close()

    second = ActivityStore(path)
    second.add(fake(ts=2))
    assert [r.ts for r in second.rows()] == [1, 2]
    second.close()


def test_default_path_resolves_to_config_db_path(tmp_path, monkeypatch):
    # Point config at a temp file so the real activity.db is never touched.
    target = tmp_path / "FocusTracker" / "activity.db"
    monkeypatch.setattr(config, "DB_PATH", target)
    ActivityStore().close()
    assert target.exists()


# --- Part B: site column + category cache ---


def test_site_round_trips_and_none_stays_null(tmp_path):
    path = tmp_path / "a.db"
    store = ActivityStore(path)
    with_site = Sample(ts=1, app="Google Chrome", window_title="T", idle_seconds=0.0, site="example.com")
    store.add(with_site)
    store.add(fake(ts=2))
    assert store.rows()[0] == with_site
    assert store.rows()[1].site is None
    store.close()
    assert sqlite3.connect(path).execute("SELECT site FROM samples ORDER BY ts").fetchall() == [
        ("example.com",), (None,),
    ]


OLD_SCHEMA = """
CREATE TABLE samples (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL,
    window_title TEXT, idle_seconds REAL NOT NULL
);
CREATE INDEX idx_samples_ts ON samples(ts);
"""


def test_old_database_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.execute("INSERT INTO samples (ts, app, window_title, idle_seconds) VALUES (10, 'Xcode', 'a', 0.5)")
    old.execute("INSERT INTO samples (ts, app, window_title, idle_seconds) VALUES (15, 'Slack', NULL, 1.0)")
    old.commit()
    old.close()

    store = ActivityStore(path)
    assert [(r.ts, r.app, r.window_title, r.idle_seconds, r.site) for r in store.rows()] == [
        (10, "Xcode", "a", 0.5, None), (15, "Slack", None, 1.0, None),
    ]
    store.add(Sample(ts=20, app="Safari", window_title="x", idle_seconds=0.0, site="a.com"))
    store.close()

    again = ActivityStore(path)  # second open: migration is a no-op
    assert len(again.rows()) == 3
    again.close()


def test_category_cache_get_set_overwrite_and_persist(tmp_path):
    path = tmp_path / "a.db"
    store = ActivityStore(path)
    assert store.get_category("app", "zed") is None
    store.set_category("app", "zed", "Work")
    store.set_category("app", "zed", "Learning")
    assert store.get_category("app", "zed") == "Learning"
    store.close()
    again = ActivityStore(path)
    assert again.get_category("app", "zed") == "Learning"
    again.close()


def test_category_cache_kinds_do_not_collide(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    store.set_category("app", "reddit", "Other")
    store.set_category("site", "reddit", "Social")
    assert store.get_category("app", "reddit") == "Other"
    assert store.get_category("site", "reddit") == "Social"
    store.close()
