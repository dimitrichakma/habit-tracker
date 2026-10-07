"""Throwaway spike: can we read frontmost app / window title / idle time, and reach Ollama?

Run from desktop_tracker/:  uv run python spikes/check.py
Pass --once to take a single sample and exit; otherwise Ctrl-C to stop. Not part of the app; nothing here is imported elsewhere.
"""

import os
import sys
import time

import ollama
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

MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b-mlx")
INTERVAL_SECONDS = 3


def frontmost_app():
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return (app.localizedName(), app.processIdentifier()) if app else (None, None)


def window_title(pid):
    if not AXIsProcessTrusted():
        return "<denied - grant Accessibility to your terminal app>"
    ax_app = AXUIElementCreateApplication(pid)
    err, window = AXUIElementCopyAttributeValue(ax_app, kAXFocusedWindowAttribute, None)
    if err or window is None:
        return f"<no focused window (AX error {err})>"
    err, title = AXUIElementCopyAttributeValue(window, kAXTitleAttribute, None)
    if err:
        return f"<no title (AX error {err})>"
    return title or "<empty>"


def idle_seconds():
    return CGEventSourceSecondsSinceLastEventType(
        kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType
    )


def ollama_check():
    try:
        resp = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": "Reply with exactly: pong"}],
            think=False,
        )
        print(f"[ollama:{MODEL}] {resp['message']['content'].strip()}")
    except Exception as exc:  # spike: report any failure, keep going
        print(f"[ollama:{MODEL}] FAILED: {type(exc).__name__}: {exc}")


def main():
    once = "--once" in sys.argv[1:]
    print(f"Accessibility trusted: {bool(AXIsProcessTrusted())}")
    ollama_check()
    print("Sampling once" if once else f"Sampling every {INTERVAL_SECONDS}s (Ctrl-C to stop)")
    try:
        while True:
            try:
                name, pid = frontmost_app()
                title = window_title(pid) if pid else "<n/a>"
                print(f"app={name!r}  title={title!r}  idle={idle_seconds():.1f}s")
            except Exception as exc:
                print(f"sample failed: {type(exc).__name__}: {exc}")
            if once:
                break
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
