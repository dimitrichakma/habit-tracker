# AI Habit Tracker

A habit tracking application with an AI coach that lets you log and review
habits through natural conversation instead of forms and buttons. Talk to
it in plain English ("did my 5k and read a chapter", "how am I doing this
week"), and it logs the right habits, spots slipping patterns, and follows
up through Telegram.

**Live:** <https://habit-tracker-dimitri.streamlit.app/> (Streamlit frontend
→ FastAPI backend on Railway → Neon Postgres).

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
  metric and a faithfulness (no-invented-facts) check, proving the agent
  acknowledges failure without shaming and always offers an actionable step.
- **Safety in two layers.** In the agent: an input classifier (Claude
  Haiku, behind a regex pre-filter) that intercepts prompt injection,
  off-topic requests, and genuinely harmful ones (self-harm, disordered
  eating framed as a "habit") before the coach model runs, plus an output
  check for leaked internals. In front of it: a gateway with per-user rate
  limits, a message size cap, regex PII masking, a daily token budget, and
  a hard timeout — every check fails safe.
- **Request tracing.** Every conversation is traced to LangSmith with tags
  and a correlation id that also threads through the logs, so a single
  Telegram message can be followed end to end. Tracing is out-of-band — a
  LangSmith outage never touches a user request.

## Architecture

```
Streamlit Cloud            Telegram  --webhook-->  ┌──────────────────────────┐
(frontend)  --HTTP------------------------------>  │  FastAPI backend (Railway)│
                                                  │  LangGraph agent -> Claude │
                                                  │  APScheduler (2 cron jobs) │
                                                  └────────────┬─────────────┘
                                                               │
                                            ┌──────────────────┴──────────────────┐
                                            │        Neon PostgreSQL              │
                                            │  users / habits / habit_logs        │
                                            │  checkpoints* (conversation memory)  │
                                            │  pgvector (weekly summary vectors)   │
                                            └─────────────────────────────────────┘
```

- **FastAPI** (on Railway, from a `Dockerfile`) - REST API plus the Telegram
  webhook (`/webhook/telegram`) and a `/healthz` probe. Deployed with
  `railway up` from the CLI.
- **LangGraph** (`langchain.agents.create_agent`) - the coaching agent, with
  a dynamic system prompt (recomputed on every call, so the model always
  knows the real current time) and a fixed set of tools.
- **SQLAlchemy + Neon PostgreSQL** - one database holds three things: the
  relational tables (users, habits, logs), LangGraph's per-thread
  conversation memory (`AsyncPostgresSaver`), and the vector store. Local
  dev falls back to SQLite when `DATABASE_URL` is unset.
- **pgvector** - weekly behavioral summaries as searchable vectors, wrapped
  behind a single module (`vector_store.py`). Claude writes the summaries;
  OpenAI's `text-embedding-3-small` embeds them (the only non-Claude model
  call in the app). Migrated from ChromaDB in Phase 5 without touching the
  agent or the tests.
- **Streamlit** - chat UI, Today's Dashboard, and Plotly progress charts.
  HTTP-only; reads its backend URL from `BACKEND_BASE_URL`.
- **python-telegram-bot + APScheduler** - the bot runs *inside* the backend
  process now (a webhook, not a polling process). One scheduler, two jobs:
  the daily 8pm reminder / friction check and the Sunday-night weekly summary.
- **DeepEval + pytest** - the evaluation suite, judged entirely by Claude,
  plus a guardrail suite and a real (containerised) pgvector integration test.
- **LangSmith** - request tracing for both agent paths, enabled by env only.

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
- **Safety is layered and fails safe.** The guardrails are `create_agent`
  middleware, not a bespoke graph, so they don't fight the checkpointer; a
  deterministic regex pre-filter runs before any classifier call; and on a
  classifier error the request is *blocked*, never let through. The gateway
  sits outside the agent entirely — a message that's too long, or over the
  token budget, never reaches a model.
- **Classification is Claude too.** The guardrail classifier is Claude Haiku,
  not a cheaper non-Claude model — the "every model that generates, judges,
  or classifies text is Claude" rule holds, including in fallback paths.
  OpenAI is still only ever the embedding maths.

## Evaluation

`pytest evaluation/test_rag_agent.py` runs 6 golden coaching scenarios through
the **real** agent and grades each reply with two metrics, both judged by
`claude-opus-5` (a tier above the `claude-sonnet-5` agent, to reduce
self-grading bias):

| Metric | Checks | Threshold | Across the 6 scenarios |
|---|---|---|---|
| **Faithfulness** (DeepEval) | the reply sticks to what was actually retrieved — no invented facts | 0.70 | 0.75 – 1.00 (mean 0.92) |
| **Coaching Empathy** (custom) | acknowledges the setback without shaming **and** offers a concrete next step; any shaming caps the score at 0.2 | 0.70 | 0.60 – 1.00 (mean 0.89) |

A third metric, DeepEval's Contextual Precision, was removed: it grades a
*retrieval system*, but this suite mocks retrieval to hand the agent the exact
golden context — so it sat pinned at 1.00 with nothing to say about the agent.
Real retrieval correctness is checked separately by the non-mocked pgvector
integration test.

The scenarios cover the situations the coach has to get right: a habit
failing only on late-wake days, weekend structure collapse, one habit
dragging while the rest hold, a self-critical user who's actually improving,
an evening habit vs. late nights, and a reflection question about a one-off
bad week.

The suite has teeth — during Phase 4 it caught the agent leaking an internal
tool name into a reply (`"let me check list_habits"`) and answering a
reflection question with hollow reassurance and no next step; two
system-prompt rules fixed both. It's currently surfacing a second issue: the
empathy metric is built to grade *acknowledging a setback*, so it scores
inconsistently on the one scenario where the honest answer is "you're doing
fine" — a rubric-fit gap being addressed by revising that golden case.

LLM-as-a-Judge scores carry run-to-run variance; treat the ranges above as
indicative, not exact.

Retrieval is mocked (a fake vector store returns each scenario's golden
context), so the suite tests the agent's *reasoning over context* — which
is why the Phase 5 ChromaDB → pgvector migration didn't touch it at all.

## Tech stack

Python, FastAPI, LangChain / LangGraph, Anthropic Claude, SQLAlchemy,
PostgreSQL (Neon) + pgvector, OpenAI embeddings, Streamlit, Plotly,
python-telegram-bot (webhook), APScheduler, JWT (PyJWT), bcrypt, MCP,
LangSmith (tracing), slowapi (rate limiting), DeepEval, pytest,
testcontainers. Deployed on Railway (backend) + Streamlit Cloud
(frontend); containerized with a `Dockerfile`.

## Running it locally

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- An [Anthropic API key](https://console.anthropic.com/) and an
  [OpenAI API key](https://platform.openai.com/api-keys)
- Optional: a PostgreSQL database with the `pgvector` extension (e.g.
  [Neon](https://neon.tech)). Leave `DATABASE_URL` unset to run on a local
  SQLite file instead — everything works, you just don't get the vector
  memory (it needs pgvector).
- Optional (for the Telegram features): a bot token from
  [@BotFather](https://t.me/BotFather), plus a public HTTPS URL for the
  webhook — locally, a tunnel like `cloudflared tunnel --url http://localhost:8000`.

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
uv run python -m src.summarize_memory     # write this week's memory summary now
uv run python -m src.mcp_server           # standalone MCP server (stdio)
```

The Telegram bot runs inside the backend as a webhook — there's no separate
bot process. For local Telegram testing, expose the backend over a tunnel
and point the webhook at it:

```bash
cloudflared tunnel --url http://localhost:8000
# then set TELEGRAM_WEBHOOK_URL=https://<tunnel-host>/webhook/telegram in .env
```

### Test suites

```bash
uv run pytest evaluation/test_rag_agent.py        # LLM-as-a-Judge coaching eval (~3-4 min, real API calls)
uv run pytest evaluation/test_guardrails.py       # safety guardrails, drives the real agent
uv run pytest evaluation/test_gateway_security.py # rate limits / size cap / PII / budget / timeout (offline, fast)
uv run pytest tests/                              # real pgvector integration (Docker or TEST_DATABASE_URL)
```

See [Evaluation](#evaluation) above for what the coaching eval grades. The
`evaluation/` suites need `ANTHROPIC_API_KEY` (+ `OPENAI_API_KEY`); `tests/`
needs Docker or a scratch `TEST_DATABASE_URL` plus `OPENAI_API_KEY`, and
skips cleanly otherwise.

See `CLAUDE.md` for the full architecture reference and `docs/` for detailed
walkthroughs of specific pieces of logic.

## Deployment

The backend is containerized (`Dockerfile`) and runs on **Railway**. Deploys
are `railway up` from the CLI (the service isn't wired to GitHub auto-deploy).
It talks to a **Neon** PostgreSQL database (one database for the relational
tables, the conversation checkpoints, and the pgvector store). Environment
variables are set through Railway, not committed — see `.env.example` for the
full list, including the optional Phase 6 tracing / rate-limit / guardrail /
budget knobs.

The Streamlit frontend is deployed on **Streamlit Cloud** at
<https://habit-tracker-dimitri.streamlit.app/> — it reads `BACKEND_BASE_URL`
(pointing at the Railway backend) from app secrets and has its own slim
`requirements.txt`.

Telegram delivers updates to `POST /webhook/telegram`; the backend registers
the webhook on startup from `TELEGRAM_WEBHOOK_URL` and verifies every request
against `WEBHOOK_SECRET_TOKEN`.

## Status

Feature-complete through Phase 6.

- **Phase 1** — core chat, Today's Dashboard, JWT auth, Plotly progress charts ✅
- **Phase 2** — Telegram bot + daily 8pm reminder scheduler ✅
- **Phase 3** — pattern-aware coaching, micro-commitments, standalone MCP server ✅
- **Phase 4** — semantic memory (RAG) over weekly summaries + LLM-as-a-Judge evaluation suite ✅
- **Phase 5** — SQLite → Neon Postgres + pgvector, Telegram polling → webhook, Docker packaging; backend live on Railway, frontend on Streamlit Cloud ✅
- **Phase 6** — LangSmith tracing + correlation ids, security layer (signup gate, rate limiting, headers), prompt-caching latency work, a real non-mocked pgvector integration test, AI guardrails (input/output safety classification), and an infrastructure gateway (size cap, PII masking, token budget, timeout, generic errors) ✅