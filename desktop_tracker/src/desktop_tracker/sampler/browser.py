"""Browser tab title + URL host through AppleScript (Automation permission).

`read_tab` never raises and never keeps more than the host of a URL. Private windows
yield nothing. Timeouts are short so a stuck browser can't stall the 5-second loop.
"""

import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse

TIMEOUT_SECONDS = 2
_AUTOMATION_DENIED = "-1743"

_CHROME_SCRIPT = """
tell application "Google Chrome"
    if (count of windows) is 0 then return ""
    set m to mode of front window
    set t to title of active tab of front window
    set u to URL of active tab of front window
    return m & linefeed & t & linefeed & u
end tell
"""

# Safari has no reliable AppleScript signal for private windows. Until a spike on a real
# Mac proves one (spikes/safari_private.py), Safari is app-name only: failing safe means
# never reading a tab we can't tell is private.
_SCRIPTS = {"Google Chrome": _CHROME_SCRIPT}
BROWSERS = ("Google Chrome", "Safari")


@dataclass(frozen=True)
class TabInfo:
    title: str | None
    site: str | None
    issue: str | None = None  # clear, user-facing message (e.g. Automation denied)
    private: bool = False


def automation_message(app: str) -> str:
    return (
        f"Browser tabs are unavailable: Automation permission for {app} is not granted. "
        "Tracking continues with window titles only. To fix it, open System Settings > "
        "Privacy & Security > Automation, enable "
        f"{app} for the app you run this from (e.g. Terminal), then restart the tracker."
    )


def host_of(url: str) -> str | None:
    """The host of an http(s) URL, lowercased. Path, query, port and userinfo are dropped."""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return None
        return parsed.hostname or None
    except ValueError:
        return None


def read_tab(app: str, run=subprocess.run) -> TabInfo | None:
    """None if `app` isn't a browser we handle; otherwise a TabInfo (possibly empty)."""
    if app not in BROWSERS:
        return None
    script = _SCRIPTS.get(app)
    if script is None:
        return TabInfo(None, None)
    try:
        done = run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except Exception:  # timeout, osascript missing, anything: degrade quietly
        return TabInfo(None, None)
    if done.returncode != 0:
        denied = _AUTOMATION_DENIED in (done.stderr or "")
        return TabInfo(None, None, issue=automation_message(app) if denied else None)
    out = done.stdout or ""
    lines = (out[:-1] if out.endswith("\n") else out).split("\n")  # keep an empty URL line
    if len(lines) < 3:
        return TabInfo(None, None)
    mode, url, title = lines[0].strip().lower(), lines[-1], "\n".join(lines[1:-1])
    if mode == "incognito":
        return TabInfo(None, None, private=True)
    return TabInfo(title, host_of(url))
