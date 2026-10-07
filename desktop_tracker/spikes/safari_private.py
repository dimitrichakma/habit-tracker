"""Throwaway spike: can AppleScript tell a Safari private window from a normal one?

Run from desktop_tracker/:  uv run python spikes/safari_private.py
Open one normal and one private Safari window, put each in front in turn, and read what
this prints. Until a reliable difference shows up, Safari stays app-name only
(see sampler/browser.py). Prints only the property values, never the URL.
"""

import subprocess
import time

SCRIPT = '''
tell application "Safari"
    if (count of windows) is 0 then return "no windows"
    set w to front window
    return (name of w) & " | props: " & (properties of w as string)
end tell
'''

for _ in range(6):
    done = subprocess.run(["osascript", "-e", SCRIPT], capture_output=True, text=True, timeout=5)
    print((done.stdout or done.stderr).strip()[:400])
    time.sleep(4)
