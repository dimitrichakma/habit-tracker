# Project Overview
- AI Habit Tracker — chat-based habit coaching agent (Anthropic-powered)
  with a Streamlit dashboard, FastAPI backend, SQLite storage.
- Phase 1 (complete): chat to create/log habits, live Today's Dashboard,
  real per-user JWT auth, Progress tab with interactive Plotly charts
  (trend + per-habit heatmap).
- Phase 2 (in progress): Telegram bot + daily reminder scheduler on top of
  the same backend. Single-user scope — bot/scheduler act as one existing
  account via stored credentials, not an anonymous identity.

# Tech Stack
- Backend: FastAPI, LangGraph, LangChain (Anthropic), SQLAlchemy, SQLite
- Frontend: Streamlit, Plotly (interactive charts)
- Auth: bcrypt (not passlib — incompatible with bcrypt≥4.1), pyjwt
- Phase 2: python-telegram-bot (v20+, async), apscheduler, httpx
- Package manager: uv
- `create_agent` lives in `langchain.agents`, not `langchain-anthropic`

# Project Structure
- `src/__init__.py` — package root; relative imports throughout; run via
  `uvicorn src.main:app` from project root.
- `src/database.py` — models + shared logic.
  - `User(id, username, hashed_password)`; `Habit(id, user_id FK, name,
    frequency)`; `HabitLog(id, habit_id, date, status)` — no `user_id` on
    logs, ownership flows through parent `Habit`.
  - `frequency`/`status` are free text, not enums.
  - `init_db()` additive/idempotent, never drops data; one-time nullable
    migration for pre-auth habits, then `backfill_orphaned_habits()`
    (safe no-op after first real signup).
  - `is_due_today()` / `is_satisfied()` — single source of truth for
    frequency logic (rolling 7-day "weekly", skip "except <Weekday>",
    `status=="done"`-only). Used by `/habits/today`, `/habits/stats`,
    `get_pending_habits`, and `get_weekly_summary` — never reimplement
    locally.
- `src/tools.py` — LangChain `@tool` functions.
  - Every tool scoped via keyword-only `*, runtime: ToolRuntime`
    (`from langchain.tools import ToolRuntime`) → resolves user from
    `runtime.config["configurable"]["thread_id"]`. No tool accepts a
    user/user_id argument from the LLM — spoofable via prompt injection.
  - `create_new_habit`, `get_pending_habits`, `list_habits`,
    `log_habit(name, status, log_date="today"|"yesterday"|"YYYY-MM-DD")`,
    `get_weekly_summary` (trailing 7 days; daily habits report "N/M days
    done" where M excludes days not due; weekly habits report
    satisfied-or-not, not a fraction — agent uses this for recaps, not
    `get_pending_habits`), `delete_habit` (exact match only, cascades —
    agent confirms first).
  - Name resolution via `_find_habit`: exact → case-insensitive substring
    → asks to disambiguate on multiple matches (avoids silent duplicates).
- `src/agent.py` — built via `langchain.agents.create_agent` (never the
  deprecated `langgraph.prebuilt.create_react_agent`).
  - Model: `ChatAnthropic(model="claude-sonnet-5")` — verify current
    before assuming.
  - Dynamic system prompt via `@dynamic_prompt` middleware
    (`langchain.agents.middleware`, `middleware=[...]`) — NOT a callable
    to `system_prompt=` (unsupported). Must include current date/time.
  - Memory: `AsyncSqliteSaver` (`langgraph.checkpoint.sqlite.aio`), keyed
    by `thread_id`. `build_agent()` is an `@asynccontextmanager`, one
    shared instance per server lifetime.
  - Persona: elite Habit Coach, concise, accountable, never guesses data
    — covers casual recaps too, must call tools fresh every time, not
    just on first ask.
- `src/auth.py` — password hashing + JWT issue/verify +
  `get_current_user_id` dependency. Leaf module, no import cycles. Reads
  `JWT_SECRET_KEY` at import time, fails fast if unset. Tokens expire in
  24h, no refresh flow (accepted trade-off — see Known Limitations).
- `src/main.py` — FastAPI app.
  - `POST /auth/signup`, `POST /auth/login` → JWT.
  - `POST /chat` — `{message}` only, no client-supplied identity; user
    from JWT via `Depends(get_current_user_id)`. Response passed through
    `_as_text()` (handles list-of-blocks from extended thinking).
  - `GET /habits/today` — auth-required, filtered to caller's `user_id`,
    same frequency logic as above, direct DB read.
  - `GET /habits/stats?days=N` (1-365, default 30) — daily completion
    `trend`, per-habit `logs`, `current_streak`, `this_week_rate`/
    `last_week_rate`. Trend only tallies daily-style habits (weekly habits
    would falsely look "missed" on days they were never due) — weekly
    habits still show in the per-habit heatmap via real logged days.
  - `GET /chat/history` — auth-required, no path param (avoids
    user-editable identity), replays caller's own thread only.
- `src/app.py` — Streamlit frontend. NO LangChain/LangGraph logic, HTTP
  only. Login/signup gate, JWT held in `st.session_state` only (lost on
  hard refresh — deliberate, Streamlit has no persistent storage). Drops
  to login on `401`.
  - `st.tabs(["💬 Chat", "📊 Progress"])`. Progress tab: day-range radio
    (7/30/90), KPI row (`st.metric`), Plotly line/area trend chart, Plotly
    heatmap for per-habit daily status.
    - Trend uses categorical color slot 1 (`#2a78d6`); heatmap uses a
      discrete 3-band scale (done/missed/no-log: `#0ca30c`/`#d03b3b`/gray)
      with `showscale` off and a manual color-coded legend caption, since
      a status color never carries meaning alone.
    - Plotly (not `st.line_chart`) chosen for real interactivity — hover
      control, `hovermode="x unified"` crosshair.
- `src/bot.py` — **Phase 2**. Own process, independently runnable.
  - NO LangChain/LangGraph/DB logic — HTTP only, same boundary as
    `app.py`.
  - Startup: logs in via `/auth/login` using `HABIT_TRACKER_USERNAME`/
    `PASSWORD`, caches JWT + issue time.
  - Proactive refresh: re-authenticates before token age hits ~23h.
    Reactive fallback: on an unexpected `401`, re-auth once and retry; on
    a second failure, report the error to the Telegram user, never crash
    or retry silently.
  - Per message: `httpx` POST to `/chat` with `Authorization: Bearer`,
    returns the reply text to Telegram.
- `src/scheduler.py` — **Phase 2**. Runs in-process with FastAPI (trusted
  context — may import `database.py` directly, unlike `bot.py`).
  - `AsyncIOScheduler`, one job, daily at 20:00.
  - Resolves target `User.id` once at startup from
    `HABIT_TRACKER_USERNAME`. Checks pending habits via `is_due_today`/
    `is_satisfied` — never a separate raw query.
  - If pending habits exist, sends one Telegram reminder to
    `TELEGRAM_CHAT_ID`.
- `run.sh` — starts backend + frontend together; port-based cleanup trap.
  Backend uses `--reload-exclude 'src/app.py'` so frontend edits don't
  restart the backend mid-conversation.

# Architecture Notes
- **Identity is real JWT-based auth.** `user_id` = numeric `User.id`,
  resolved server-side from a verified token, never client input. Doubles
  as LangGraph `thread_id`. Pre-auth `checkpoints.db` rows are permanently
  orphaned (harmless, nothing deleted).
- **Habits are scoped per user** (`Habit.user_id` required FK) — enforced
  server-side everywhere, never by client input.

# Workflows
- Install: `uv add <dependency>`
- Run everything: `./run.sh`
- Backend only: `uv run uvicorn src.main:app --reload`
- Frontend only: `uv run streamlit run src/app.py`
- Bot only (Phase 2, testing): `uv run python -m src.bot`
- Inspect DB (read-only): `sqlite3 -readonly -header -column habits.db "SELECT * FROM habits;"`

# Rules
- `create_agent` from `langchain.agents` only, never
  `langgraph.prebuilt.create_react_agent`.
- Verify the Claude model string before hardcoding — names change.
- `.env`: `ANTHROPIC_API_KEY`, `JWT_SECRET_KEY` (app-generated via
  `secrets.token_hex(32)`, never a default), `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `HABIT_TRACKER_USERNAME`/`PASSWORD` — all
  user-managed except `JWT_SECRET_KEY`.
- Tools must derive the acting user only from `runtime` (ToolRuntime),
  never an LLM-supplied argument — spoofable via prompt injection.
- Dynamic prompt via `@dynamic_prompt` middleware only, not `system_prompt=`.
- `app.py` and `bot.py`: no LangChain/LangGraph/DB logic, HTTP only.
  `scheduler.py` is the one exception (trusted in-process context).
- `bot.py` refreshes its JWT proactively (~23h), with 401-retry as a
  fallback only — don't remove the proactive check and rely on 401 alone.
- Scheduler and agent share ONE `lifespan` in `main.py` — don't add a
  second lifespan context manager.
- No circular imports (`main → agent → tools → database`, `auth` is a leaf).
- Never delete/overwrite real `habits.db`/`checkpoints.db` for testing —
  use an isolated throwaway DB (different dir/port). Read-only inspection
  is fine.
- Kill whatever's on the port before restarting backend/frontend
  (`lsof -ti:8000`, `-ti:8501`) — don't touch unrelated processes.
- Single-user scope: one account, one `TELEGRAM_CHAT_ID` — no
  multi-user/multi-account logic without discussing it first.
- **On any missing import, circular dependency, ambiguity, or other
  issue: stop and report it. Never patch or work around it silently.**

# Known Limitations
- Frequency parsing only understands `"week"` (rolling 7-day) and
  `"except <Weekday>"` — anything more complex ("twice a week", "every 3
  days") stores fine but falls back to daily-style behavior in the
  dashboard, stats, and weekly summary.
- `frequency`/`status` are unvalidated free text end-to-end.
- JWT has no refresh flow (Streamlit: re-login on expiry, accepted
  trade-off). `bot.py` compensates with proactive refresh (see Rules).

