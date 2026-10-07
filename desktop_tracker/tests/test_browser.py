import subprocess

from desktop_tracker.sampler import browser
from desktop_tracker.sampler.browser import read_tab


class FakeRun:
    def __init__(self, stdout="", stderr="", returncode=0, raises=None):
        self.result = (stdout, stderr, returncode)
        self.raises = raises
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises:
            raise self.raises
        out, err, code = self.result
        return subprocess.CompletedProcess(args, code, out, err)


def chrome(mode, title, url):
    return FakeRun(stdout=f"{mode}\n{title}\n{url}\n")


def test_chrome_normal_returns_title_and_host_only():
    tab = read_tab("Google Chrome", run=chrome("normal", "Python tutorial", "https://www.YouTube.com/watch?v=abc#t=5"))
    assert tab.title == "Python tutorial"
    assert tab.site == "www.youtube.com"
    assert tab.private is False and tab.issue is None


def test_url_path_query_port_and_credentials_never_survive():
    tab = read_tab("Google Chrome", run=chrome("normal", "t", "https://user:secret@example.com:8443/a/b?token=xyz"))
    assert tab.site == "example.com"
    for leaked in ("secret", "user", "8443", "token", "/a/b", "https"):
        assert leaked not in repr(tab)


def test_chrome_incognito_stores_nothing():
    tab = read_tab("Google Chrome", run=chrome("incognito", "Secret page", "https://private.example/x"))
    assert tab.private is True
    assert tab.title is None and tab.site is None


def test_non_web_urls_have_no_site_but_keep_the_title():
    for url in ("chrome://settings", "file:///Users/me/notes.txt", "about:blank", ""):
        tab = read_tab("Google Chrome", run=chrome("normal", "A page", url))
        assert tab.site is None, url
        assert tab.title == "A page", url


def test_title_with_newlines_is_kept_and_url_is_the_last_line():
    tab = read_tab("Google Chrome", run=FakeRun(stdout="normal\nline one\nline two\nhttps://example.com/p\n"))
    assert tab.site == "example.com"
    assert tab.title == "line one\nline two"


def test_never_raises_on_bad_output_or_failures():
    for run in (
        FakeRun(stdout=""),
        FakeRun(stdout="garbage"),
        FakeRun(stdout="x", returncode=1, stderr="boom"),
        FakeRun(raises=subprocess.TimeoutExpired("osascript", 2)),
        FakeRun(raises=FileNotFoundError("osascript")),
        FakeRun(raises=RuntimeError("anything")),
    ):
        tab = read_tab("Google Chrome", run=run)
        assert tab.title is None and tab.site is None and tab.private is False


def test_timeout_is_short_and_quiet():
    run = FakeRun(raises=subprocess.TimeoutExpired("osascript", 2))
    tab = read_tab("Google Chrome", run=run)
    assert run.calls[0][0][0] == "osascript"
    assert run.calls[0][1]["timeout"] <= 3
    assert tab.issue is None  # a slow browser must not spam messages


def test_automation_denied_gives_a_clear_issue_naming_the_browser():
    run = FakeRun(returncode=1, stderr="execution error: Not authorized to send Apple events to Google Chrome. (-1743)")
    tab = read_tab("Google Chrome", run=run)
    assert tab.title is None and tab.site is None
    assert "Automation" in tab.issue and "Google Chrome" in tab.issue


def test_other_apps_never_call_osascript():
    run = FakeRun()
    assert read_tab("Xcode", run=run) is None
    assert read_tab("Firefox", run=run) is None
    assert run.calls == []


def test_safari_fails_safe_until_private_windows_are_detectable():
    # Safari has no reliable AppleScript signal for private windows. Until the
    # spike proves one, Safari is app-name only: osascript is never called.
    run = FakeRun(stdout="normal\nTitle\nhttps://example.com\n")
    tab = read_tab("Safari", run=run)
    assert tab.title is None and tab.site is None
    assert run.calls == []
