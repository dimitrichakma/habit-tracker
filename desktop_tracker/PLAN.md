# desktop_tracker — Plan

Local macOS activity tracker (menu bar app) living beside the habit_tracker
backend as a fully separate module. Raw activity data never leaves the Mac;
only habit id, date and status go to the existing API.

## Scope and isolation
- Must NOT change: `src/`, `evaluation/`, `tests/`, `Dockerfile`,
  `requirements.txt`, root `pyproject.toml` / `uv.lock`, `pytest.ini`, `run.sh`,
  deployment. Only the root `CLAUDE.md` gets a short pointer block.
- Backend endpoints it may call (already exist, `src/main.py`):
  `POST /auth/login` (JWT, 24h, no refresh), `GET /habits/today`
  (`done` / `pending` lists with `id`), `POST /habits/{id}/log`
  body `{"done": bool, "date"?: "YYYY-MM-DD"}` (idempotent, 60/min per user).
  Un-ticking = `done:false` = deletes the log (never writes "missed").
- No imports from `src/` or `evaluation/`; nothing in `src/` imports this.

## Layout
```
desktop_tracker/
  PLAN.md  CLAUDE.md  README.md (Part 5)
  pyproject.toml  uv.lock  .python-version  .gitignore   # own env, own lock
  categories.yaml          # apps + site keywords -> category (Part 3)
  settings.yaml            # all thresholds (Part 4)
  src/desktop_tracker/
    config.py              # DATA_DIR / DB_PATH (outside repo)
    sampler/               # active app, window title, idle seconds, AppleScript tabs
    store/                 # SQLite: raw events, sessions, pending habit changes, category cache
    categorize/            # categories.yaml lookup + Ollama fallback + cache
    queries/               # 5-6 pure Python query functions (Part 5)
    rules/                 # skill auto-log, distraction + break alerts
    api_client/            # login + GET today + POST log ONLY; payload id/date/status
    assistant/             # Ollama JSON router, Pydantic validation, insights
    menubar/               # rumps app
    __main__.py
  evaluation/              # 15 questions, accuracy script, model comparison (Part 5)
  tests/                   # offline: fake sampler, fake API, temp SQLite
```

## Data safety (never committed)
- Real DB: `~/Library/Application Support/FocusTracker/activity.db` — outside the repo.
- Backstop: `desktop_tracker/.gitignore` (`*.db*`, `*.sqlite*`, `FocusTracker/`,
  `Application Support/`, `Library/`, exports); root `.gitignore` already has `*.db`.
- Test `tests/test_data_stays_local.py` asserts `DB_PATH` is outside the repo.
- Auth token in macOS Keychain (`keyring`); API URL in settings; no secrets in repo.

## Dependencies (own `pyproject.toml`, `uv.lock`, `.venv`)
Runtime: rumps, pyobjc-framework-Quartz, pyobjc-framework-ApplicationServices,
httpx, keyring, ollama, pydantic, pyyaml. Dev: pytest.
Commands run from `desktop_tracker/`: `uv sync`, `uv run pytest`,
`uv run python -m desktop_tracker`. Never add these to the root project.

## Risks
- Bare `pytest` at repo root would collect `desktop_tracker/tests` and fail
  importing pyobjc: solved by `desktop_tracker/conftest.py`, which skips
  collection unless pytest's rootdir is this folder. No `pytest.ini` edit.
- `railway up` uploads the folder in the build context (not in the image);
  optional `.dockerignore` line only with approval.
- Auto-log writes real habit history: `done=true` only, thresholded, undoable.
- Rate limits / JWT expiry: send on state change, re-login on 401, queue offline.
- Window titles need Accessibility / Screen Recording permission: degrade, never crash.

## Known limits
- If reading idle time fails, `idle_seconds` is stored as 0.0 (looks like active use) and an issue is reported.
- A failed tick (sampler or DB write error) saves no row, so it leaves a gap in the data.

---

## Part 3: Tracker and categories
- Every 5 seconds save timestamp, app name, window title, idle seconds.
- SQLite database at `~/Library/Application Support/FocusTracker/activity.db`.
- Idle after 2 minutes with no input.
- Missing permissions: don't crash, show a clear message.
- Group raw events into sessions (same app or tab in a row, ignore gaps under 30 seconds).
- `categories.yaml` maps apps and site keywords to: Work, Learning,
  Communication, Social, Entertainment, Other.
- Chrome and Safari tab titles through AppleScript.
- Unknown apps: ask the local Ollama model once, validate the answer against
  the fixed list, cache it. If Ollama is off, use Other.
- Command that prints today's time per category.
- Claude Code hook runs desktop_tracker tests after edits there.

Part 3 is built in two steps:
- **Part A (done):** sessions, `categories.yaml`, rule matching (title keyword > site
  keyword > app name, keywords >= 4 chars), `python -m desktop_tracker today`, unknown -> Other.
- **Part B (done, Safari pending):** Chrome tab title + host via AppleScript (incognito skipped), `site`
  column (URL host only, old DBs migrated), `category_cache`, local-Ollama fallback at report time.
  Safari is app-name only until `spikes/safari_private.py` shows private windows are detectable.

Decisions: sessions only bridge sampling gaps under 30s (a switch to another app always
splits). There is no `sessions` table; sessions are computed from `samples` on demand.
Time per category is the sum of non-idle samples.

## Part 4: Menu bar, habits, and alerts
- rumps menu bar app, icon shows focused, distracted, idle, or paused.
- Menu: today's totals, pause tracking, snooze alerts, quit.
- Today's habits checklist using the existing habit API endpoints.
- API URL in config, auth token in macOS Keychain, never in the repo.
- No internet: save habit changes locally as pending, send later.
- Only habit id, date, and status go to the API. No activity data.
- Auto log skill development habit when Learning passes 60 minutes, with undo.
  Rule values configurable.
- Simple rule alerts, no AI: distraction (20 min Social or Entertainment in
  last 30 min), break (50 min without 5 min idle), 15 minute cooldown, none
  while idle, paused, or snoozed.
- All thresholds in a config file.

## Part 5: Ask and insights with the local model
- Model never writes SQL.
- 5 or 6 Python query functions (time per category, top distracting sites,
  longest focus block, compare two days, most focused hour).
- Model returns JSON choosing a function and arguments, validated with
  Pydantic, retry once if invalid.
- Model answers using only the returned numbers.
- Daily and weekly insights: stats calculated in Python, model writes 3 tips,
  Python checks the numbers match, regenerate once if not.
- Eval folder with 15 questions and expected function choices, accuracy
  script, compare two Ollama models.
- README section: what it does, install, privacy design, eval results.
- Plain Python, not LangGraph, unless clearly simpler.

