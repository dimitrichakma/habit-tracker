from desktop_tracker.categorize.resolver import CategoryResolver
from desktop_tracker.store import ActivityStore


class FakeClassifier:
    def __init__(self, answers=None, raises=None):
        self.answers, self.raises, self.calls = answers or {}, raises, []

    def classify(self, name, kind):
        self.calls.append((kind, name))
        if self.raises:
            raise self.raises
        return self.answers.get(name)


def make(tmp_path, classifier, lines=None):
    store = ActivityStore(tmp_path / "a.db")
    return store, CategoryResolver(store, classifier, out=(lines if lines is not None else []).append)


def test_rule_match_never_asks_the_model(tmp_path):
    clf = FakeClassifier()
    _, r = make(tmp_path, clf)
    assert r.resolve("Slack", None, None) == "Communication"
    assert clf.calls == []


def test_unknown_app_is_asked_once_then_cached(tmp_path):
    clf = FakeClassifier({"Zed": "Work"})
    store, r = make(tmp_path, clf)
    assert r.resolve("Zed", None, "x") == "Work"
    assert r.resolve("Zed", None, "y") == "Work"
    assert clf.calls == [("app", "Zed")]
    assert store.get_category("app", "zed") == "Work"


def test_cache_survives_a_new_resolver_and_works_with_ollama_off(tmp_path):
    clf = FakeClassifier({"Zed": "Work"})
    store, r = make(tmp_path, clf)
    r.resolve("Zed", None, None)
    off = FakeClassifier(raises=ConnectionError("off"))
    assert CategoryResolver(store, off, out=lambda _: None).resolve("Zed", None, None) == "Work"
    assert off.calls == []


def test_browser_sample_is_classified_by_site_host_only(tmp_path):
    clf = FakeClassifier({"learn.example.org": "Learning"})
    store, r = make(tmp_path, clf)
    assert r.resolve("Google Chrome", "learn.example.org", "Secret title") == "Learning"
    assert clf.calls == [("site", "learn.example.org")]
    assert store.get_category("site", "learn.example.org") == "Learning"


def test_title_keyword_is_resolved_by_rules_before_the_model(tmp_path):
    clf = FakeClassifier()
    _, r = make(tmp_path, clf)
    assert r.resolve("Zed", None, "Python tutorial") == "Learning"
    assert clf.calls == []


def test_failure_gives_other_caches_nothing_and_stops_asking_this_report(tmp_path):
    clf = FakeClassifier(raises=ConnectionError("off"))
    lines = []
    store, r = make(tmp_path, clf, lines)
    assert r.resolve("Zed", None, None) == "Other"
    assert r.resolve("Other App", None, None) == "Other"
    assert len(clf.calls) == 1  # skipped for the rest of the report
    assert store.get_category("app", "zed") is None
    assert len(lines) == 1  # one short notice


def test_next_report_tries_ollama_again(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    down = FakeClassifier(raises=ConnectionError("off"))
    CategoryResolver(store, down, out=lambda _: None).resolve("Zed", None, None)
    up = FakeClassifier({"Zed": "Work"})
    assert CategoryResolver(store, up, out=lambda _: None).resolve("Zed", None, None) == "Work"


def test_invalid_answer_gives_other_and_is_not_cached(tmp_path):
    clf = FakeClassifier({"Zed": None})
    store, r = make(tmp_path, clf)
    assert r.resolve("Zed", None, None) == "Other"
    assert r.resolve("Zed", None, None) == "Other"
    assert len(clf.calls) == 1  # asked once per report
    assert store.get_category("app", "zed") is None


def test_no_classifier_means_other(tmp_path):
    store = ActivityStore(tmp_path / "a.db")
    assert CategoryResolver(store, None).resolve("Zed", None, None) == "Other"
