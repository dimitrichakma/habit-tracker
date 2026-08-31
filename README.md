# AI Habit Tracker

A habit tracking application with an AI coach that lets you log and review
habits through natural conversation instead of forms and buttons. Talk to
it in plain English ("did my 5k and read a chapter", "how am I doing this
week"), and it logs the right habits, spots slipping patterns, and follows
up through Telegram.

## Why this exists

Most habit trackers are just checkbox apps. The interesting problem here was
making the *coach* itself reason about behaviour: telling a one-off missed
day apart from a genuine recurring failure, and reacting differently to each,
without ever guessing at data it hasn't actually looked up.

## What it does

- **Conversational logging.** "Did my run and skipped the gym" logs both in
  one turn, fuzzy-matching each name against your existing habits.
- **Today's Dashboard and Progress charts** - a plain data view (no LLM call)
  for the deterministic "what's done today" question, plus historical trend
  and streak charts.
- **Pattern-aware coaching.** When you report a miss, the coach checks six
  weeks of history before responding. A single off day gets acknowledged and
  dropped. A real pattern - a repeated weekday miss, a multi-day streak, a low
  completion rate - gets a smaller "micro-commitment" version of the habit
  suggested instead (a 45-minute gym session becomes a 10-minute walk).
- **Long-term semantic memory.** A weekly job writes a short third-person
  summary of how your week went and stores it in a vector database. When you
  later ask for advice or "why do I keep failing at mornings", the coach
  retrieves the relevant past-week summaries and grounds its answer in them
  instead of guessing.
- **Telegram bot** with a daily reminder and an evening "friction check" that
  proactively messages you if habits are still pending, using the same
  pattern-aware logic as the chat.
- **Per-user accounts** with JWT authentication - each user's habits, chat
  history, and Telegram reminders are fully isolated.
- **A standalone MCP server**, separate from the production agent, exposing
  the safe, low-risk tools (logging, viewing) to any MCP client like Claude
  Desktop, built as a learning exercise in the protocol.
- **An LLM-as-a-Judge evaluation suite** - a golden dataset of coaching
  scenarios, graded by a stronger Claude model against a custom empathy
  metric plus faithfulness and context-precision, proving the agent
  acknowledges failure without shaming and always offers an actionable step.

## Architecture

```
Streamlit frontend  --HTTP-->  FastAPI backend  -->  LangGraph agent  -->  Claude
                                     |                      |
                                SQLite (users,          SQLite (per-thread
                                habits, logs)          conversation memory)
                                     |                      |
                                     |              ChromaDB (weekly behavioral
                                     |              summaries, vector search)
                                     |
                              Telegram bot + APScheduler
                              (daily reminder, friction check, weekly summary)
```

- **FastAPI** - REST API: auth, dashboard/stats endpoints, chat endpoint.
- **LangGraph** (`langchain.agents.create_agent`) - the coaching agent, with
  a dynamic system prompt (recomputed on every call, so the model always
  knows the real current time) and a fixed set of tools.
- **SQLAlchemy + SQLite** - users, habits, and daily logs. A second SQLite
  database, managed by LangGraph's `AsyncSqliteSaver`, holds per-user
  conversation memory so the coach remembers context across sessions.
- **ChromaDB** - a local vector store of weekly behavioral summaries, wrapped
  behind a single module so it can be swapped for pgvector later. Claude
  writes the summaries; OpenAI's `text-embedding-3-small` embeds them (the
  only non-Claude model call in the app).
- **Streamlit** - chat UI, Today's Dashboard, and Plotly progress charts.
- **python-telegram-bot + APScheduler** - one scheduler, two jobs: the daily
  8pm reminder / friction check and the Sunday-night weekly summary. Runs a
  separate process from the API for Telegram.
- **DeepEval + pytest** - the evaluation suite, judged entirely by Claude.

## Key design decisions

- **Identity never comes from the client.** Every authenticated endpoint
  resolves the user id from the verified JWT only - never from a request
  body or path parameter. Tools are scoped to a user through LangGraph's
  `thread_id`, not through anything the model itself supplies, so the LLM
  cannot be prompted into acting on someone else's data.
- **No refresh tokens.** The frontend only ever holds the access token in
  Streamlit's session state, which is lost on a hard refresh anyway - adding
  a refresh token would add real complexity (rotation, a second endpoint)
  for no actual gain at this app's scale.
- **The agent never guesses.** The system prompt explicitly forbids
  repeating a "pending" summary from memory; it must call a tool fresh every
  time, since habit data can change between turns from another device.
- **The MCP server is intentionally separate** from the production agent and
  intentionally excludes the destructive `delete_habit` tool - its only
  safeguard (confirming the name first) lives in the main agent's system
  prompt, and an external MCP client has no equivalent safety net.
- **Deterministic reads skip the LLM.** The Today's Dashboard is a plain
  database query, not an agent call - a "what's due today" question doesn't
  need a model in the loop.
- **Claude does the thinking; OpenAI only does the maths.** Every model call
  that generates or judges text is Claude. OpenAI is used for one thing -
  turning summary text into embedding vectors - because that's a
  numeric-similarity task, not a reasoning one.
- **The agent is measured, not vibe-checked.** A golden dataset plus an
  LLM-as-a-Judge suite grades each release: retrieval is mocked so the tests
  survive a future vector-store swap unchanged, and the judge is a stronger
  Claude tier than the agent it grades to reduce self-grading bias.

## Tech stack

Python, FastAPI, LangChain / LangGraph, Anthropic Claude, SQLAlchemy, SQLite,
ChromaDB, OpenAI embeddings, Streamlit, Plotly, python-telegram-bot,
APScheduler, JWT (PyJWT), bcrypt, MCP, DeepEval, pytest.

## Running it locally

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- An [Anthropic API key](https://console.anthropic.com/) and an
  [OpenAI API key](https://platform.openai.com/api-keys)
- Optional (for the Telegram features): a bot token from
  [@BotFather](https://t.me/BotFather)

### Install

```bash
git clone <this-repo> habit_tracker
cd habit_tracker
uv sync                 # creates .venv and installs the exact pinned versions from uv.lock
cp .env.example .env     # then edit .env — every variable is documented in that file
```

Generate the JWT secret and paste it into `JWT_SECRET_KEY` in `.env`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**First account** — the Telegram bot and the schedulers act as one existing
account. Start the app once (below), sign up through the Streamlit UI, then
set `HABIT_TRACKER_USERNAME` / `HABIT_TRACKER_PASSWORD` in `.env` to match it.

### Run everything

```bash
./run.sh
```

Starts the FastAPI backend on `:8000` and the Streamlit UI on `:8501`
together (clearing those ports first). Open <http://localhost:8501>. The
daily-reminder and weekly-summary schedulers run inside the backend
automatically.

### Individual pieces

```bash
uv run uvicorn src.main:app --reload      # backend only  (API docs at /docs)
uv run streamlit run src/app.py           # frontend only
uv run python -m src.bot                  # Telegram bot   (needs the backend running)
uv run python -m src.summarize_memory     # write this week's memory summary now
uv run python -m src.mcp_server           # standalone MCP server (stdio)
```

Only one process may poll the Telegram token at a time — don't run two bots.

**Always-on (macOS)** — `deploy/launchd/manage.sh install` registers the
backend + bot as `launchd` agents (auto-start at login, restart on crash);
`manage.sh {status,logs,restart,stop,uninstall}` manage them. Run
`manage.sh stop` before `./run.sh` so nothing fights over port 8000.

### Evaluation suite

```bash
uv run pytest evaluation/
```

Runs the golden dataset through the real agent and grades every reply with
three Claude-judged metrics. Makes real API calls (~3–4 min; a few dollars of
Claude usage). Needs `ANTHROPIC_API_KEY` + `OPENAI_API_KEY`.

See `CLAUDE.md` for the full architecture reference and `docs/` for detailed
walkthroughs of specific pieces of logic.

## Status

Actively developed.

- **Phase 1** — core chat, Today's Dashboard, JWT auth, Plotly progress charts ✅
- **Phase 2** — Telegram bot + daily 8pm reminder scheduler ✅
- **Phase 3** — pattern-aware coaching, micro-commitments, standalone MCP server ✅
- **Phase 4** — semantic memory (RAG) over weekly summaries + LLM-as-a-Judge evaluation suite ✅
- **Phase 5** — Postgres + pgvector migration, then Docker packaging and CI/CD (planned)