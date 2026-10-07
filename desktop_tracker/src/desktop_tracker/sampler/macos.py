"""Real macOS sampler: frontmost app, focused window title, seconds idle.

`sample()` never raises. A missing permission yields a Sample with
window_title=None and `issue` set to a clear message.
"""

import time

from AppKit import NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    kAXFocusedWindowAttribute,
    kAXTitleAttribute,
)
from Quartz import (
    CGEventSourceSecondsSinceLastEventType,
    kCGAnyInputEventType,
    kCGEventSourceStateCombinedSessionState,
)

from .base import ACCESSIBILITY_MESSAGE, Sample

_AX_API_DISABLED = -25211  # kAXErrorAPIDisabled: Accessibility not allowed


def _frontmost():
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return "Unknown", None
    return app.localizedName() or "Unknown", app.processIdentifier()


def _title(pid):
    """(title | None, issue | None)."""
    if not AXIsProcessTrusted():
        return None, ACCESSIBILITY_MESSAGE
    ax_app = AXUIElementCreateApplication(pid)
    err, window = AXUIElementCopyAttributeValue(ax_app, kAXFocusedWindowAttribute, None)
    if err == _AX_API_DISABLED:
        return None, ACCESSIBILITY_MESSAGE
    if err or window is None:
        return None, None  # no focused window: normal, not a permission problem
    err, title = AXUIElementCopyAttributeValue(window, kAXTitleAttribute, None)
    if err == _AX_API_DISABLED:
        return None, ACCESSIBILITY_MESSAGE
    if err:
        return None, None
    return (str(title) if title is not None else ""), None


def _idle_seconds() -> float:
    return float(
        CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType
        )
    )


class MacSampler:
    def sample(self) -> Sample:
        ts = int(time.time())
        issue = None
        try:
            app, pid = _frontmost()
        except Exception as exc:
            app, pid, issue = "Unknown", None, f"Could not read the active app: {exc}"

        title = None
        if pid is not None:
            try:
                title, title_issue = _title(pid)
                issue = issue or title_issue
            except Exception as exc:
                issue = issue or f"Could not read the window title: {exc}"

        try:
            idle = _idle_seconds()
        except Exception as exc:
            idle = 0.0
            issue = issue or f"Could not read idle time: {exc}"

        return Sample(ts=ts, app=app, window_title=title, idle_seconds=idle, issue=issue)
