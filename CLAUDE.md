# Project Overview
- AI Habit Tracker — chat-based habit coaching agent (Anthropic-powered)
  with a Streamlit dashboard, FastAPI backend, SQLite storage.
- Phase 1 (complete): chat to create/log habits, live Today's Dashboard,
  real per-user JWT auth, Progress tab with interactive Plotly charts
  (trend + per-habit heatmap).
- Phase 2 (complete): Telegram bot + daily 8PM reminder scheduler on top
  of the same backend. Single-user scope — bot/scheduler act as one
  existing account via stored credentials, not an anonymous identity.
  Proactive JWT refresh in the bot.
- Phase 3 (complete): three independent upgrades —
  1. Agent checks a habit's own failure pattern when it's missed and
     suggests a smaller "micro-commitment" version of it.
  2. Agent parses messy natural-language messages ("did my 5k and read a
     chapter") into multiple separate habit logs in one turn.
  3. A standalone MCP server (learning exercise, not wired into the
     production agent) exposes a few read-mostly tools over MCP for
     testing with external clients like Claude Desktop.

# Tech Stack
- Backend: FastAPI, LangGraph, LangChain (Anthropic), SQLAlchemy, SQLite
- Frontend: Streamlit, Plotly (interactive charts)
- Auth: bcrypt (not passlib — incompatible with bcrypt≥4.1), pyjwt
- Phase 2: python-telegram-bot (v20+, async), apscheduler, httpx
- Phase 3: no new dependencies for items 1–2 (pure SQL + prompt work).
  Item 3 adds `mcp` for the standalone learning server only, pinned
  `mcp<2` (currently 1.29.1): mcp 2.x renamed `FastMCP` → `MCPServer`,
  and the tutorials/clients this exercise targets still use `FastMCP`.
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
  - `is_due_today()` / `is_satisfied()` / `satisfaction_window_days()` —
    single source of truth for frequency logic (rolling 7-day "weekly",
    skip "except <Weekday>", `status=="done"`-only). Used by
    `/habits/today`, `/habits/stats`, `get_pending_habits`,
    `get_weekly_summary`, `get_habit_history_pattern`, and the scheduler's
    `run_friction_nudge` — never reimplement locally.
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
  - `get_habit_history_pattern(habit_name)` — **Phase 3**. Plain-text
    recurring-failure summary from existing `HabitLog` over a 6-week
    lookback (no schema change). Daily habits: overall done/due rate,
    current consecutive-miss streak, per-weekday trouble spots (weekday
    due ≥3× and missed on >half); collapses to "across the whole week"
    when 5+ weekdays are bad; says "one-off off day" explicitly when rate
    ≥80% with no streak/spike. Weekly habits: satisfied-in-N-of-last-6
    -weeks. Reuses `is_due_today` / `satisfaction_window_days`.
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
  - **Phase 3** prompt additions: (a) on a reported missed habit, call
    `get_habit_history_pattern` first, then `get_pending_habits`; if a
    genuine recurring pattern shows (not a one-off), propose a smaller
    "micro-commitment" version of that same habit, from the agent's own
    judgment, never stored config. (b) parse compound messages ("did my
    5k and read a chapter") into multiple `log_habit` calls in one turn,
    each matched via `_find_habit`. (c) never shame — empathetic,
    solution-oriented.
  - `TOOLS` binds `get_habit_history_pattern` alongside the existing tools.
- `src/auth.py` — password hashing + JWT issue/verify +
  `get_current_user_id` dependency. Leaf module, no import cycles. Reads
  `JWT_SECRET_KEY` at import time, fails fast if unset. Tokens expire in
  24h, no refresh flow (accepted trade-off — see Known Limitations).
- `src/main.py` — FastAPI app.
  - `POST /auth/signup`, `POST /auth/login` → JWT.
  - `POST /chat` — `{message}` only, no client-supplied identity; user
    from JWT via `Depends(get_current_user_id)`. Response passed through
    `_as_text()` (handles list-of-blocks from extended thinking).
  - `POST /evaluate_friction` — **Phase 3**. No request body; identity
    from `Depends(get_current_user_id)` only. Runs the same
    `run_friction_nudge` the 8PM job runs (imported from `scheduler`) —
    the pending-check logic is never duplicated.
  - `GET /habits/today` — auth-required, filtered to caller's `user_id`,
    same frequency logic as above, direct DB read.
  - `GET /habits/stats?days=N` (1-365, default 30) — daily completion
    `trend`, per-habit `logs`, `current_streak`, `this_week_rate`/
    `last_week_rate`. Trend only tallies daily-style habits (weekly habits
    would falsely look "missed" on days they were never due) — weekly
    habits still show in the per-habit heatmap via real logged days.
  - `GET /chat/history` — auth-required, no path param (avoids
    user-editable identity), replays caller's own thread only.
  - Lifespan (**Phase 3** change): builds the agent *before*
    `start_reminder_scheduler(agent)` (the job invokes the agent); still
    one lifespan, scheduler `.shutdown()` in its `finally`.
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
  - Suppresses `httpx` INFO logs (the request URL embeds the bot token).
- `src/scheduler.py` — **Phase 2 + Phase 3**. Runs in-process with
  FastAPI (trusted context — may import `database.py` directly, unlike
  `bot.py`).
  - `AsyncIOScheduler`, one job, daily at 20:00 **Asia/Dhaka**
    (`REMINDER_TIMEZONE` env override) so it fires at 8pm Dhaka even on a
    UTC host.
  - Resolves target `User.id` once at startup from
    `HABIT_TRACKER_USERNAME`.
  - `run_friction_nudge(agent, user_id)` — **Phase 3** — the single
    shared "evening friction check": pending habits via
    `_pending_habit_names` (uses `is_due_today`/`is_satisfied`, never a
    raw query); returns `None` if nothing pending, else invokes the agent
    on the user's own `thread_id` and returns its nudge text. The 8PM job
    calls this and sends the result to `TELEGRAM_CHAT_ID` via the same
    `send_message` — same job, richer output. Also called directly by
    `POST /evaluate_friction`.
  - The nudge runs on the user's real chat thread, so its trigger +
    reply persist and appear in `/chat/history` (this is what lets a
    Telegram reply to the nudge continue the same conversation).
- `src/mcp_server.py` — **new (Phase 3), standalone learning exercise,
  NOT connected to the production agent.**
  - Built with `FastMCP` (`from mcp.server.fastmcp import FastMCP`, mcp
    pinned `<2`), stdio transport, run and tested independently (e.g. via
    Claude Desktop or MCP Inspector), reads/writes the same `habits.db`
    as the real app (so it must be run from the project root).
  - Exposes: `log_habit`, `get_pending_habits`, `list_habits`,
    `get_weekly_summary` — all read-mostly and low-risk. Each MCP tool is
    a thin shim that calls the matching `src/tools.py` function's `.func`
    with a fixed-identity `_FixedRuntime` (a minimal `ToolRuntime`
    stand-in exposing only `config["configurable"]["thread_id"]`) — no
    habit logic reimplemented here.
  - **Does NOT expose `delete_habit`.** That tool's only safety net (the
    "confirm before deleting" instruction) lives in the main app's system
    prompt, not in the tool itself — an external MCP client has no such
    safeguard and would delete a habit (cascading to its full log
    history) with no confirmation at all.
  - **Identity is fixed at server startup, never per tool call.** Reads
    `HABIT_TRACKER_USERNAME` from its environment once at launch and acts
    as that one account for its entire run — mirrors how `bot.py` and
    `scheduler.py` already establish identity. No tool takes a `user_id`
    parameter; nothing about identity is ever LLM-suppliable.
  - `src/agent.py`/`tools.py` are unaffected by this file — the
    production agent keeps using its local, `ToolRuntime`-scoped tools.
    This server is a separate, parallel path for learning MCP, not a
    replacement.
- `run.sh` — starts backend + frontend together; port-based cleanup trap.
  Backend uses `--reload-exclude 'src/app.py'` so frontend edits don't
  restart the backend mid-conversation.
- `deploy/launchd/` — macOS LaunchAgents (backend + bot) for always-on
  running; `manage.sh {install|uninstall|start|stop|restart|status|logs}`.
  Run `manage.sh stop` before `./run.sh` so nothing fights over port 8000.

# Architecture Notes
- **Identity is real JWT-based auth.** `user_id` = numeric `User.id`,
  resolved server-side from a verified token, never client input. Doubles
  as LangGraph `thread_id`. Pre-auth `checkpoints.db` rows are permanently
  orphaned (harmless, nothing deleted).
- **Habits are scoped per user** (`Habit.user_id` required FK) — enforced
  server-side everywhere, never by client input.
- **The friction nudge writes to the user's chat thread.** The scheduler
  and `/evaluate_friction` both invoke the agent with the real
  `thread_id`, so the nudge conversation is part of the user's history —
  required for the agent's tools to scope correctly and for Telegram
  replies to continue it.

# Workflows
- Install: `uv add <dependency>`
- Run everything: `./run.sh`
- Backend only: `uv run uvicorn src.main:app --reload`
- Frontend only: `uv run streamlit run src/app.py`
- Bot only (Phase 2, testing): `uv run python -m src.bot`
- Trigger a nudge without waiting for 8PM: `./scripts/friction-check.sh`
  (wraps `POST /evaluate_friction`; backend must be running).
- MCP server standalone (Phase 3): `uv run python -m src.mcp_server`, then
  connect an MCP client (Claude Desktop, MCP Inspector) separately.
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
  second lifespan context manager. The agent is built before the
  scheduler starts (the 8PM job invokes the agent).
- No circular imports (`main → agent → tools → database`, `auth` is a
  leaf; `main → scheduler → database`, `scheduler` imports no app code).
- Never delete/overwrite real `habits.db`/`checkpoints.db` for testing —
  use an isolated throwaway DB (different dir/port). Read-only inspection
  is fine.
- Kill whatever's on the port before restarting backend/frontend
  (`lsof -ti:8000`, `-ti:8501`) — don't touch unrelated processes.
- Single-user scope: one account, one `TELEGRAM_CHAT_ID` — no
  multi-user/multi-account logic without discussing it first.
- **Phase 3:** a micro-commitment suggestion comes from the agent's own
  judgment about the habit, never from stored per-habit configuration —
  only propose one on a genuine recurring pattern (via
  `get_habit_history_pattern`), not a single isolated miss.
- **Phase 3:** natural-language multi-habit parsing must reuse the
  existing `_find_habit` resolution for each mentioned item — never
  invent a new matching method.
- **Phase 3:** `POST /evaluate_friction` follows the same auth rule as
  every other endpoint: identity from `Depends(get_current_user_id)`
  only, never a client-supplied `user_id` — this was the exact
  vulnerability already fixed once in `/chat`.
- **Phase 3:** `scheduler.py`'s 8PM job and `/evaluate_friction` run the
  same underlying flow (`run_friction_nudge` in `scheduler.py`) — don't
  implement the pending-habit-check logic twice.
- **Phase 3:** `src/mcp_server.py` is standalone and experimental — never
  wire it into `agent.py`'s actual tool bindings without a separate,
  explicit decision. It never exposes `delete_habit`, and never accepts a
  `user_id` parameter on any tool — identity is fixed once via
  `HABIT_TRACKER_USERNAME` at process startup only.
- Update this file (CLAUDE.md) whenever a phase is completed.
- **On any missing import, circular dependency, ambiguity, or other
  issue: stop and report it. Never patch or work around it silently.**

# Known Limitations
- Frequency parsing only understands `"week"` (rolling 7-day) and
  `"except <Weekday>"` — anything more complex ("twice a week", "every 3
  days") stores fine but falls back to daily-style behavior in the
  dashboard, stats, weekly summary, and `get_habit_history_pattern`.
- `frequency`/`status` are unvalidated free text end-to-end.
- JWT has no refresh flow (Streamlit: re-login on expiry, accepted
  trade-off). `bot.py` compensates with proactive refresh (see Rules).
