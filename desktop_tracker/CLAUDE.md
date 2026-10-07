# desktop_tracker

Local macOS activity tracker + menu bar app, a separate module inside
habit_tracker. Full design, thresholds and roadmap: `PLAN.md` (read it first).

## Working rule
Only do the step I ask for. Never build ahead in PLAN.md without my OK.

## Isolation (hard rules)
- Do not edit `src/`, `evaluation/` (root), `tests/` (root), `Dockerfile`,
  `requirements.txt`, root `pyproject.toml` / `uv.lock` / `pytest.ini`,
  or any deployment file. If a change there seems needed: stop and report.
- Never import from `src/` or root `evaluation/`; nothing outside this folder
  imports `desktop_tracker`. Talk to the backend over HTTP only.
- Own env: `pyproject.toml`, `uv.lock`, `.venv` here. Never add its deps to
  the root project or to the Docker image.

## Privacy (hard rules)
- Raw activity (app, window title, idle, tabs, sessions) stays on this Mac.
- Only `api_client/` calls the backend. Allowed calls: `POST /auth/login`,
  `GET /habits/today`, `POST /habits/{id}/log`. Payload = habit id, date,
  status only. Keep a test that asserts this.
- Activity DB: `~/Library/Application Support/FocusTracker/activity.db`.
  Never create DB / export files inside the repo (`.gitignore` is a backstop,
  not the plan). Never `git add -f` anything under it.
- Auth token in macOS Keychain (`keyring`); never in files, logs or the repo.
- Ollama is localhost only. Prompts are built from local aggregates.

## Behaviour rules
- The sampler never raises or blocks: missing permission -> clear message,
  keep running (app name only if titles are denied).
- Auto-log sends `done=true` only, after the configured Learning threshold,
  and must be undoable. Un-tick sends `done=false` (deletes the log).
- Offline: queue habit changes locally as pending, retry later.
- Alerts are plain rules, no AI. All thresholds live in `settings.yaml`.
- The model never writes SQL: it picks one of the Python query functions and
  arguments (Pydantic-validated, retry once). It answers only from returned
  numbers; Python verifies numbers in insights. Plain Python, no LangGraph.
- Categories are the fixed list: Work, Learning, Communication, Social,
  Entertainment, Other. Validate model output against it; fall back to Other.

## Commands (run from `desktop_tracker/`)
- Install: `uv sync`
- Tests: `uv run pytest`  (offline; fake sampler, fake API, temp SQLite)
- Run: `uv run python -m desktop_tracker`
- `conftest.py` makes the repo-root pytest skip this folder (different env);
  run these tests only from here. Don't edit the root `pytest.ini`.

## Layout
`sampler/` capture - `store/` SQLite - `categorize/` yaml + Ollama cache -
`queries/` Python stats - `rules/` auto-log + alerts - `api_client/` backend
- `assistant/` Ollama router + insights - `menubar/` rumps - `evaluation/`
router accuracy (this folder's own, not the root one).

## Permissions
Window titles need Accessibility and/or Screen Recording; Chrome/Safari tabs
need Automation (AppleScript). Document in README, never crash on denial.
