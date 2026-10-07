import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from desktop_tracker import __main__ as cli
from desktop_tracker import config, report
from desktop_tracker.sampler.base import Sample
from desktop_tracker.store import ActivityStore

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 7)


@pytest.fixture(autouse=True)
def new_york(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(day, h, m=0, sec=0) -> int:
    return int(datetime(day.year, day.month, day.day, h, m, sec, tzinfo=NY).timestamp())


def sample(ts, app, title=None, idle=0.0) -> Sample:
    return Sample(ts=ts, app=app, window_title=title, idle_seconds=idle)


def make_store(tmp_path, samples) -> ActivityStore:
    store = ActivityStore(tmp_path / "a.db")
    for x in samples:
        store.add(x)
    return store


def test_sums_samples_per_category(tmp_path):
    store = make_store(tmp_path, [
        sample(at(DAY, 9), "Xcode"),
        sample(at(DAY, 9, 0, 5), "Xcode"),
        sample(at(DAY, 9, 0, 10), "Slack"),
        sample(at(DAY, 9, 0, 15), "Zzz Unknown"),
    ])
    totals = report.time_per_category(store, DAY)
    assert totals == {
        "Work": 10, "Learning": 0, "Communication": 5,
        "Social": 0, "Entertainment": 0, "Other": 5,
    }


def test_title_keywords_are_applied_per_sample(tmp_path):
    store = make_store(tmp_path, [
        sample(at(DAY, 9), "Some Browser", "Python tutorial"),
        sample(at(DAY, 9, 0, 5), "Some Browser", "random page"),
    ])
    totals = report.time_per_category(store, DAY)
    assert totals["Learning"] == 5
    assert totals["Other"] == 5


def test_idle_samples_are_not_counted(tmp_path):
    store = make_store(tmp_path, [
        sample(at(DAY, 9), "Xcode"),
        sample(at(DAY, 9, 0, 5), "Xcode", idle=600.0),
    ])
    assert report.time_per_category(store, DAY)["Work"] == 5


def test_only_the_local_day_counts(tmp_path):
    yesterday, tomorrow = date(2026, 10, 6), date(2026, 10, 8)
    store = make_store(tmp_path, [
        sample(at(yesterday, 23, 59, 55), "Xcode"),  # yesterday, local
        sample(at(DAY, 0, 0, 0), "Xcode"),           # first second of today
        sample(at(DAY, 23, 59, 55), "Xcode"),        # last sample of today
        sample(at(tomorrow, 0, 0, 0), "Xcode"),      # tomorrow, local
    ])
    assert report.time_per_category(store, DAY)["Work"] == 10


def test_empty_day_is_all_zero(tmp_path):
    store = make_store(tmp_path, [])
    assert set(report.time_per_category(store, DAY).values()) == {0}


@pytest.mark.parametrize("seconds,text", [
    (45, "45s"), (60, "1m"), (125, "2m"), (3599, "59m"),
    (3600, "1h 00m"), (7500, "2h 05m"),
])
def test_duration_format(seconds, text):
    assert report.format_duration(seconds) == text


def test_report_lists_non_zero_categories_in_fixed_order_then_total():
    totals = {
        "Work": 7500, "Learning": 0, "Communication": 0,
        "Social": 1800, "Entertainment": 0, "Other": 60,
    }
    lines = report.format_report(totals).splitlines()
    assert [l.split() for l in lines] == [
        ["Work", "2h", "05m"],
        ["Social", "30m"],
        ["Other", "1m"],
        ["Total", "2h", "36m"],
    ]


def test_report_for_an_empty_day():
    totals = dict.fromkeys(
        ["Work", "Learning", "Communication", "Social", "Entertainment", "Other"], 0
    )
    assert report.format_report(totals) == "No activity recorded today."


# --- the command ---


def test_today_subcommand_prints_the_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(report, "today", lambda: DAY)
    make_store(tmp_path, [
        sample(at(DAY, 9), "Xcode"),
        sample(at(DAY, 9, 0, 5), "Xcode"),
    ]).close()
    monkeypatch.setattr(cli, "OllamaClassifier", lambda: None)  # never reach a real Ollama

    cli.main(["today"])

    out = capsys.readouterr().out
    assert "Work" in out and "10s" in out
    assert "Total" in out


def test_no_subcommand_still_starts_tracking(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "a.db")
    calls = []
    monkeypatch.setattr(cli, "MacSampler", lambda: "fake-sampler")
    monkeypatch.setattr(cli.tracker, "run", lambda sampler, store: calls.append(sampler))

    cli.main([])

    assert calls == ["fake-sampler"]


# --- Part B: unknown apps/sites are classified at report time ---


class FakeClassifier:
    def __init__(self, answers=None, raises=None):
        self.answers, self.raises, self.calls = answers or {}, raises, []

    def classify(self, name, kind):
        self.calls.append((kind, name))
        if self.raises:
            raise self.raises
        return self.answers.get(name)


def test_unknown_app_uses_the_classifier_and_the_cache(tmp_path):
    store = make_store(tmp_path, [sample(at(DAY, 9), "Zed"), sample(at(DAY, 9, 0, 5), "Zed")])
    clf = FakeClassifier({"Zed": "Work"})
    assert report.time_per_category(store, DAY, classifier=clf)["Work"] == 10
    assert clf.calls == [("app", "Zed")]
    report.time_per_category(store, DAY, classifier=clf)
    assert len(clf.calls) == 1  # cached: never asked again


def test_site_is_used_for_browser_samples(tmp_path):
    store = make_store(tmp_path, [
        Sample(ts=at(DAY, 9), app="Google Chrome", window_title="Secret", idle_seconds=0.0, site="learn.example.org"),
    ])
    clf = FakeClassifier({"learn.example.org": "Learning"})
    assert report.time_per_category(store, DAY, classifier=clf)["Learning"] == 5
    assert clf.calls == [("site", "learn.example.org")]


def test_ollama_down_gives_other_without_caching(tmp_path):
    store = make_store(tmp_path, [sample(at(DAY, 9), "Zed"), sample(at(DAY, 9, 0, 5), "Quux")])
    clf = FakeClassifier(raises=ConnectionError("off"))
    lines = []
    totals = report.time_per_category(store, DAY, classifier=clf, out=lines.append)
    assert totals["Other"] == 10
    assert len(clf.calls) == 1  # skipped for the rest of the report
    assert store.get_category("app", "zed") is None
    assert len(lines) == 1


def test_no_classifier_keeps_part_a_behaviour(tmp_path):
    store = make_store(tmp_path, [sample(at(DAY, 9), "Zed")])
    assert report.time_per_category(store, DAY)["Other"] == 5


def test_today_command_builds_a_local_classifier(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(report, "today", lambda: DAY)
    make_store(tmp_path, [sample(at(DAY, 9), "Zed")]).close()
    clf = FakeClassifier({"Zed": "Work"})
    monkeypatch.setattr(cli, "OllamaClassifier", lambda: clf)

    cli.main(["today"])

    assert "Work" in capsys.readouterr().out
