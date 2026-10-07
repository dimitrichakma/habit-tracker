import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


def test_real_sampler_returns_a_sample_and_never_raises():
    pytest.importorskip("Quartz")
    from desktop_tracker.sampler.base import Sample
    from desktop_tracker.sampler.macos import MacSampler

    sample = MacSampler().sample()

    assert isinstance(sample, Sample)
    assert isinstance(sample.app, str) and sample.app
    assert sample.window_title is None or isinstance(sample.window_title, str)
    assert isinstance(sample.idle_seconds, float) and sample.idle_seconds >= 0
    assert isinstance(sample.ts, int)
    # ts is UTC epoch seconds: within a few seconds of "now" whatever the local timezone.
    assert abs(sample.ts - time.time()) < 5


def test_browser_tab_replaces_the_title_and_sets_the_site(monkeypatch):
    pytest.importorskip("Quartz")
    from desktop_tracker.sampler import macos
    from desktop_tracker.sampler.browser import TabInfo

    monkeypatch.setattr(macos, "_frontmost", lambda: ("Google Chrome", 1))
    monkeypatch.setattr(macos, "_title", lambda pid: ("AX title", None))
    monkeypatch.setattr(macos, "_idle_seconds", lambda: 0.0)
    monkeypatch.setattr(macos, "read_tab", lambda app: TabInfo("Tab title", "example.com"))

    sample = macos.MacSampler().sample()
    assert sample.window_title == "Tab title" and sample.site == "example.com"


def test_private_browser_window_stores_no_title_and_no_site(monkeypatch):
    pytest.importorskip("Quartz")
    from desktop_tracker.sampler import macos
    from desktop_tracker.sampler.browser import TabInfo

    monkeypatch.setattr(macos, "_frontmost", lambda: ("Google Chrome", 1))
    monkeypatch.setattr(macos, "_title", lambda pid: ("Secret AX title", None))
    monkeypatch.setattr(macos, "_idle_seconds", lambda: 0.0)
    monkeypatch.setattr(macos, "read_tab", lambda app: TabInfo(None, None, private=True))

    sample = macos.MacSampler().sample()
    assert sample.window_title is None and sample.site is None


def test_failed_tab_read_falls_back_to_the_ax_title_and_reports_the_issue(monkeypatch):
    pytest.importorskip("Quartz")
    from desktop_tracker.sampler import macos
    from desktop_tracker.sampler.browser import TabInfo

    monkeypatch.setattr(macos, "_frontmost", lambda: ("Google Chrome", 1))
    monkeypatch.setattr(macos, "_title", lambda pid: ("AX title", None))
    monkeypatch.setattr(macos, "_idle_seconds", lambda: 0.0)
    monkeypatch.setattr(macos, "read_tab", lambda app: TabInfo(None, None, issue="denied"))

    sample = macos.MacSampler().sample()
    assert sample.window_title == "AX title" and sample.site is None
    assert sample.issue == "denied"


def test_non_browser_apps_are_untouched(monkeypatch):
    pytest.importorskip("Quartz")
    from desktop_tracker.sampler import macos

    monkeypatch.setattr(macos, "_frontmost", lambda: ("Xcode", 1))
    monkeypatch.setattr(macos, "_title", lambda pid: ("Main.swift", None))
    monkeypatch.setattr(macos, "_idle_seconds", lambda: 0.0)
    monkeypatch.setattr(macos, "read_tab", lambda app: None)

    sample = macos.MacSampler().sample()
    assert sample.window_title == "Main.swift" and sample.site is None
