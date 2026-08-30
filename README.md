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
- **Telegram bot** with a daily reminder and an evening "friction check" that
  proactively messages you if habits are still pending, using the same
  pattern-aware logic as the chat.
- **Per-user accounts** with JWT authentication - each user's habits, chat
  history, and Telegram reminders are fully isolated.
- **A standalone MCP server**, separate from the production agent, exposing
  the safe, low-risk tools (logging, viewing) to any MCP client like Claude
  Desktop, built as a learning exercise in the protocol.

## Architecture

```
Streamlit frontend  --HTTP-->  FastAPI backend  -->  LangGraph agent  -->  Claude
                                     |                      |
                                SQLite (users,          SQLite (per-thread
                                habits, logs)          conversation memory)
                                     |
                              Telegram bot + APScheduler
                              (daily reminder, friction check)
```

- **FastAPI** - REST API: auth, dashboard/stats endpoints, chat endpoint.
- **LangGraph** (`langchain.agents.create_agent`) - the coaching agent, with
  a dynamic system prompt (recomputed on every call, so the model always
  knows the real current time) and a fixed set of tools.
- **SQLAlchemy + SQLite** - users, habits, and daily logs. A second SQLite
  database, managed by LangGraph's `AsyncSqliteSaver`, holds per-user
  conversation memory so the coach remembers context across sessions.
- **Streamlit** - chat UI, Today's Dashboard, and Plotly progress charts.
- **python-telegram-bot + APScheduler** - daily reminders and evening
  follow-ups, running as a separate process from the API.

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

## Tech stack

Python, FastAPI, LangChain / LangGraph, Anthropic Claude, SQLAlchemy, SQLite,
Streamlit, Plotly, python-telegram-bot, APScheduler, JWT (PyJWT), bcrypt, MCP.

## Running it locally

```bash
uv sync
cp .env.example .env   # fill in ANTHROPIC_API_KEY, JWT_SECRET_KEY, TELEGRAM_BOT_TOKEN, etc.
./run.sh
```

See `CLAUDE.md` for the full architecture reference and `docs/` for detailed
walkthroughs of specific pieces of logic.

## Status

Actively developed. Phases 1 (core chat + dashboard), 2 (Telegram bot,
reminders), and 3 (pattern-aware coaching, micro-commitments, MCP server)
are complete.
