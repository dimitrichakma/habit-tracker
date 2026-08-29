#!/usr/bin/env bash
# Manually run the evening "friction check" — the same flow src/scheduler.py's
# 8PM job runs automatically (POST /evaluate_friction). Prints the coach's
# nudge, or "nothing pending". Does NOT send a Telegram message.
#
# Needs: the backend running on 127.0.0.1:8000 and HABIT_TRACKER_USERNAME /
# HABIT_TRACKER_PASSWORD in .env at the project root.
#
#   ./scripts/friction-check.sh
set -eu

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

exec uv run python - "$PROJECT_DIR" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv(os.path.join(sys.argv[1], ".env"))

BASE = os.environ.get("BACKEND_BASE_URL", "http://127.0.0.1:8000")


def post(path, data=None, token=None):
    req = urllib.request.Request(BASE + path, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    payload = json.dumps(data).encode() if data is not None else b""
    with urllib.request.urlopen(req, payload) as resp:
        return json.load(resp)


try:
    token = post(
        "/auth/login",
        {
            "username": os.environ["HABIT_TRACKER_USERNAME"],
            "password": os.environ["HABIT_TRACKER_PASSWORD"],
        },
    )["access_token"]
    print(post("/evaluate_friction", token=token)["reply"])
except urllib.error.URLError as exc:
    sys.exit(f"Could not reach the backend at {BASE} — is it running? ({exc})")
except KeyError as exc:
    sys.exit(f"Missing {exc} in .env")
PY
