from desktop_tracker import tracker
from desktop_tracker.sampler.base import (
    ACCESSIBILITY_MESSAGE,
    Sample,
)
from desktop_tracker.store import ActivityStore


class ScriptedSampler:
    """Returns pre-baked samples, or raises when the script item is an Exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def sample(self):
        item = self.script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def s(ts, app="Notes", title="todo", idle=0.0, issue=None) -> Sample:
    return Sample(ts=ts, app=app, window_title=title, idle_seconds=idle, issue=issue)


def run(sampler, store, n, out=None):
    sleeps, lines = [], []
    tracker.run(
        sampler,
        store,
        interval=5,
        sleep=sleeps.append,
        max_samples=n,
        out=out or lines.append,
    )
    return sleeps, lines


def test_n_ticks_save_n_rows_in_order_and_sleep_five_seconds(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    sampler = ScriptedSampler([s(10), s(15), s(20)])

    sleeps, _ = run(sampler, store, 3)

    assert [r.ts for r in store.rows()] == [10, 15, 20]
    assert sleeps == [5, 5, 5]
    store.close()


def test_sampler_exception_does_not_stop_the_loop(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    sampler = ScriptedSampler([s(10), RuntimeError("boom"), s(20)])

    _, lines = run(sampler, store, 3)

    assert [r.ts for r in store.rows()] == [10, 20]  # no row for the failed tick
    assert any("boom" in line for line in lines)
    store.close()


def test_db_write_failure_does_not_stop_the_loop(tmp_path):
    store = FlakyStore(tmp_path / "a.db", {2: OSError("disk hiccup")})
    sampler = ScriptedSampler([s(10), s(15), s(20)])

    _, lines = run(sampler, store, 3)

    assert store.attempts == 3
    assert [r.ts for r in store.rows()] == [10, 20]
    assert any("disk hiccup" in line for line in lines)
    store.close()


def test_permission_message_printed_once_and_app_still_saved(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    denied = [
        s(10 + 5 * i, app="Mail", title=None, issue=ACCESSIBILITY_MESSAGE)
        for i in range(4)
    ]

    _, lines = run(ScriptedSampler(denied), store, 4)

    assert sum(ACCESSIBILITY_MESSAGE in line for line in lines) == 1
    rows = store.rows()
    assert len(rows) == 4
    assert all(r.app == "Mail" and r.window_title is None for r in rows)
    store.close()


class FlakyStore(ActivityStore):
    """Raises the scripted exception on the Nth add() calls (1-based); else saves."""

    def __init__(self, path, fail_on):
        super().__init__(path)
        self.fail_on = dict(fail_on)  # attempt number -> exception
        self.attempts = 0

    def add(self, sample):
        self.attempts += 1
        if self.attempts in self.fail_on:
            raise self.fail_on[self.attempts]
        super().add(sample)


def count(lines, text):
    return sum(text in line for line in lines)


# --- repeated identical errors: print once, quiet until different error or recovery ---


def test_identical_sampler_errors_print_once(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    script = [RuntimeError("no display")] * 4

    _, lines = run(ScriptedSampler(script), store, 4)

    assert count(lines, "no display") == 1
    assert store.rows() == []
    store.close()


def test_a_different_sampler_error_prints_again(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    script = [
        RuntimeError("first"),
        RuntimeError("first"),
        RuntimeError("second"),
        RuntimeError("second"),
    ]

    _, lines = run(ScriptedSampler(script), store, 4)

    assert count(lines, "first") == 1
    assert count(lines, "second") == 1
    store.close()


def test_same_message_but_different_exception_type_is_a_different_error(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    script = [RuntimeError("bad"), ValueError("bad")]

    _, lines = run(ScriptedSampler(script), store, 2)

    assert count(lines, "bad") == 2
    store.close()


def test_sampler_error_prints_again_after_recovery(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    script = [RuntimeError("flaky"), RuntimeError("flaky"), s(20), RuntimeError("flaky")]

    _, lines = run(ScriptedSampler(script), store, 4)

    assert count(lines, "flaky") == 2  # once before recovery, once after
    assert [r.ts for r in store.rows()] == [20]
    store.close()


def test_alternating_errors_each_print_because_they_differ_from_the_last(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    script = [RuntimeError("A"), RuntimeError("B"), RuntimeError("A")]

    _, lines = run(ScriptedSampler(script), store, 3)

    assert count(lines, "A") == 2
    assert count(lines, "B") == 1
    store.close()


def test_identical_db_write_errors_print_once(tmp_path):
    fail = {i: OSError("disk full") for i in (1, 2, 3)}
    store = FlakyStore(tmp_path / "a.db", fail)

    _, lines = run(ScriptedSampler([s(10), s(15), s(20)]), store, 3)

    assert count(lines, "disk full") == 1
    assert store.rows() == []
    store.close()


def test_a_different_db_error_prints_again(tmp_path):
    store = FlakyStore(
        tmp_path / "a.db", {1: OSError("disk full"), 2: OSError("db locked")}
    )

    _, lines = run(ScriptedSampler([s(10), s(15)]), store, 2)

    assert count(lines, "disk full") == 1
    assert count(lines, "db locked") == 1
    store.close()


def test_db_error_prints_again_after_recovery(tmp_path):
    store = FlakyStore(
        tmp_path / "a.db",
        {1: OSError("disk full"), 2: OSError("disk full"), 4: OSError("disk full")},
    )

    _, lines = run(ScriptedSampler([s(10), s(15), s(20), s(25)]), store, 4)

    assert count(lines, "disk full") == 2
    assert [r.ts for r in store.rows()] == [20]
    store.close()


def test_a_sampler_error_then_a_db_error_are_both_reported(tmp_path):
    store = FlakyStore(tmp_path / "a.db", {1: OSError("disk full")})
    script = [RuntimeError("no display"), s(15)]

    _, lines = run(ScriptedSampler(script), store, 2)

    assert count(lines, "no display") == 1
    assert count(lines, "disk full") == 1
    store.close()


def test_a_different_issue_is_still_reported(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    script = [
        s(10, title=None, issue="issue A"),
        s(15, title=None, issue="issue B"),
        s(20, title=None, issue="issue A"),
    ]

    _, lines = run(ScriptedSampler(script), store, 3)

    assert sum("issue A" in line for line in lines) == 1
    assert sum("issue B" in line for line in lines) == 1
    store.close()


def test_issue_is_not_persisted(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    run(ScriptedSampler([s(10, title=None, issue="x")]), store, 1)
    # Round-trips with the four stored fields only; issue is runtime-only.
    assert store.rows()[0].issue is None
    store.close()


def test_permission_message_is_clear_and_actionable():
    assert "Accessibility" in ACCESSIBILITY_MESSAGE
    assert "System Settings" in ACCESSIBILITY_MESSAGE
    assert "Privacy & Security" in ACCESSIBILITY_MESSAGE
