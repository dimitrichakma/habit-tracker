# Project Overview
- AI Habit Tracker — chat-based habit coaching agent (Anthropic-powered)
  with a Streamlit dashboard, FastAPI backend, PostgreSQL (Neon) +
  pgvector storage. Backend deployed on Railway.
- Phase 1 (complete): chat to create/log habits, live Today's Dashboard,
  real per-user JWT auth, Progress tab with interactive Plotly charts.
- Phase 2 (complete): Telegram bot + daily 8PM reminder scheduler,
  single-user scope. (The bot's proactive-JWT-refresh mechanism was
  removed in Phase 5 when it moved in-process — see below.)
- Phase 3 (complete): pattern-aware coaching (checks a habit's own
  failure history, suggests a smaller micro-commitment), natural-language
  multi-habit logging in one message, manual `/evaluate_friction` test
  endpoint, and a standalone MCP learning server (not wired into
  production).
- Phase 4 (complete): semantic memory (RAG) over weekly behavioral
  summaries, and an LLM-as-a-Judge evaluation suite (golden dataset +
  DeepEval, `claude-opus-5` judge) proving the coaching agent's quality.
  Every model that *generates or judges* text is Claude (agent + weekly
  summariser = `claude-sonnet-5`, eval judge = `claude-opus-5`). OpenAI
  is used for exactly two non-reasoning things: the vector embeddings
  (`text-embedding-3-small`) and DeepEval's base dependency. `.env` now
  needs `OPENAI_API_KEY`.
- Phase 5 (complete): database + deployment.
  - **Storage:** SQLite → PostgreSQL (Neon), ChromaDB → pgvector. ONE Neon
    database now holds the relational tables, the agent's per-thread
    checkpoint tables, and the weekly-summary vectors.
  - **Checkpointer:** `AsyncSqliteSaver` → `AsyncPostgresSaver` (a small
    managed pool on Neon's DIRECT endpoint; SQLite on `CHECKPOINT_DB` is
    the fallback for local dev and the eval suite).
  - **Telegram:** the long-polling `src/bot.py` process → a webhook merged
    into `main.py`. `bot.py` is kept as a module (builds the
    `python-telegram-bot` Application + handlers); `main.py`'s lifespan
    drives it and registers the webhook.
  - **Deployment:** the FastAPI backend is containerized (`Dockerfile`)
    and runs on **Railway**. Deploys are **`railway up` from the CLI**
    (uploads the build context) — NOT `git push`. This repo's Railway
    service is not wired to GitHub auto-deploy; a `git push` alone does
    nothing until `railway up` runs. The Streamlit frontend is prepared
    for **Streamlit Cloud** (env-driven `BACKEND_BASE_URL`, a slim
    `requirements.txt`) but the deploy step is manual and not yet done.
    No `supervisord`, no GitHub Actions pipeline — the two services
    deploy independently.
  - The original plan routed through AWS then GCP; both were abandoned
    (GCP's org policies made public Cloud Run unworkable). Railway is the
    final answer.
- Phase 6 (complete): observability, safety, and infrastructure hardening.
  - **6.1 Observability:** LangSmith tracing on both agent paths (`/chat`,
    Telegram webhook) + `run_friction_nudge`, project `"habit-tracker"`.
    `@traceable` on the non-graph helpers (`summarize_user_week`,
    `HabitMemoryStore.add_summary` / `similarity_search`) with input
    scrubbing. Per-request correlation ids on every log line. Tracing is
    env-driven (`LANGSMITH_TRACING` etc.) and never fails a request.
  - **Security layer:** open `/auth/signup` gated behind `SIGNUP_SECRET`
    (an `X-Signup-Secret` header); in-memory rate limiting (slowapi) +
    baseline security headers.
  - **pgvector integration test:** a real, non-mocked `tests/` suite
    exercising `vector_store.HabitMemoryStore` write / read / per-user
    isolation against a real Postgres+pgvector (testcontainers or
    `TEST_DATABASE_URL`).
  - **Latency:** Anthropic prompt caching on the static system-prompt
    block, `pool_recycle` on the DB engine, Railway region co-located
    with Neon (`us-east-2`). `SummarizationMiddleware` was tried and
    **removed** — it fired twice per turn (+20-30s).
  - **AI guardrails & semantic safety:** input classification (regex
    pre-filter → Claude Haiku classifier) short-circuiting the agent on
    `PromptInjection` / `HarmfulBehavior` / `OffTopic`, plus an
    output guardrail (`after_model`) for leaked internals / medical
    advice. Fail-safe (block, never open) on classifier error.
  - **AI gateway & infrastructure security:** per-message size cap, regex
    PII masking, a daily token budget (`token_usage` table), a generic
    catch-all error handler, and a hard timeout on the agent call. See
    `src/main.py`'s gateway section.

# Tech Stack
- Backend: FastAPI, LangGraph, LangChain (Anthropic), SQLAlchemy,
  PostgreSQL (Neon) + pgvector
- Frontend: Streamlit, Plotly
- Auth: bcrypt (not passlib — incompatible with bcrypt≥4.1), pyjwt
- Phase 2: python-telegram-bot (v22, async — webhook, not polling), apscheduler
- Phase 3: `mcp` (FastMCP, standalone learning server only)
- Phase 4: `langchain-openai` (`text-embedding-3-small` embeddings only,
  runtime). `deepeval` + `pytest` + `pytest-asyncio` are in
  `[dependency-groups] dev` — test/eval only, nothing under `src/` imports
  them, and the `Dockerfile`'s `uv sync --no-dev` keeps them out of the
  Railway image.
- Phase 5: `psycopg[binary,pool]`, `pgvector`, `langchain-postgres`
  (`PGVector`), `langgraph-checkpoint-postgres`. Removed: `chromadb`,
  `langchain-chroma`. `langgraph-checkpoint-sqlite` kept (checkpointer
  fallback). Deploy: `Dockerfile` + `.dockerignore` + `requirements.txt`
  (Streamlit Cloud); Railway (backend), Neon (Postgres), Streamlit Cloud
  (frontend, pending).
- Phase 6: `langsmith` (≥0.12.1, tracing — bumped from 0.11.1),
  `slowapi` (in-memory rate limiting). The Haiku guardrail classifier
  uses the existing `langchain-anthropic`. Dev group (`[dependency-groups]
  dev`): `testcontainers[postgres]` (pgvector integration suite), plus
  `deepeval` / `pytest` / `pytest-asyncio` (moved out of runtime deps).
  No new runtime NLP deps — PII masking is plain `re`.
- Package manager: uv
- `create_agent` lives in `langchain.agents`, not `langchain-anthropic`

# Project Structure
- `src/__init__.py` — package root; relative imports throughout; run via
  `uvicorn src.main:app` from project root.
- `src/database.py` — models + shared logic. **Sync** SQLAlchemy (the
  tool layer stays synchronous; only the checkpointer is async).
  - Engine reads `DATABASE_URL`; falls back to `sqlite:///habits.db` when
    unset (local dev, and the eval suite, which swaps in its own throwaway
    SQLite). `_normalize_pg_url()` forces the `postgresql+psycopg://`
    driver; Postgres engine uses `pool_pre_ping=True` +
    `connect_args={"prepare_threshold": None}` so the SAME url works on
    Neon's POOLED (PgBouncer / `-pooler`) endpoint.
  - `User(id, username, hashed_password)`; `Habit(id, user_id FK, name,
    frequency)`; `HabitLog(id, habit_id, date, status)`.
  - `TokenUsage(id, user_id FK, usage_date, input_tokens, output_tokens)`
    — **Phase 6**. One row per `(user_id, usage_date)` (unique), the
    daily token-budget ledger. `daily_token_total(user_id, day)` sums it
    (0 when no row — that IS the per-calendar-day reset);
    `record_token_usage(...)` increments it (called post-reply from a
    `BackgroundTask`). Only the worker model's `usage_metadata` is
    counted — the Haiku guardrail classifier's tokens are not.
  - `frequency`/`status` are free text, not enums.
  - `init_db()` additive/idempotent, never drops data — `create_all`
    picks up `token_usage` on the next startup. On Postgres it runs
    `CREATE EXTENSION IF NOT EXISTS vector` (for pgvector's tables). The
    nullable-column migration `_migrate_add_habits_user_id()` is
    **SQLite-only** (guarded on `engine.dialect.name`; `PRAGMA` isn't
    valid Postgres). `backfill_orphaned_habits()` unchanged.
  - `is_due_today()` / `is_satisfied()` — single source of truth for
    frequency logic (rolling 7-day "weekly", skip "except <Weekday>",
    `status=="done"`-only). Used everywhere pending/satisfied status is
    checked — never reimplement locally.
- `src/tools.py` — LangChain `@tool` functions.
  - Every tool scoped via keyword-only `*, runtime: ToolRuntime` →
    resolves user from `runtime.config["configurable"]["thread_id"]`. No
    tool accepts a user/user_id argument from the LLM — spoofable via
    prompt injection.
  - `create_new_habit`, `get_pending_habits`, `list_habits`,
    `log_habit(name, status, log_date="today"|"yesterday"|"YYYY-MM-DD")`,
    `get_weekly_summary`, `delete_habit` (exact match, cascades, agent
    confirms first).
  - `get_habit_history_pattern(habit_name)` — **Phase 3**. Plain-text
    pattern summary from `HabitLog` history (e.g. "missed 4 of last 5
    Mondays"), pure SQL, no schema change.
  - `query_past_behavior(topic)` — **Phase 4**. Calls the generic
    LangChain `VectorStore.similarity_search` interface (never a
    backend-specific API — this is exactly why the Phase 5 pgvector
    migration didn't touch this function or its tests) against the
    `habit_summaries` collection, filtered to the caller's own `user_id`
    (via `ToolRuntime`, same as every other tool — never an LLM-supplied
    value). Returns top 3 matches. Unchanged by Phase 5.
  - Name resolution via `_find_habit`: exact → case-insensitive substring
    → asks to disambiguate on multiple matches.
- `src/agent.py` — built via `langchain.agents.create_agent` (never the
  deprecated `langgraph.prebuilt.create_react_agent`).
  - Model: `ChatAnthropic(model="claude-sonnet-5",
    output_config={"effort": WORKER_EFFORT})` — verify the model string is
    current before assuming. `WORKER_EFFORT` (env, default `"medium"`) caps
    adaptive-thinking depth: `claude-sonnet-5` defaults to `effort: high`,
    which drove the slow-turn latency tail; `"medium"` trims it without the
    shallowness `"low"` risks (validated against `evaluation/test_rag_agent.py`).
  - Dynamic system prompt via `@dynamic_prompt` middleware
    (`langchain.agents.middleware`) — NOT a callable to `system_prompt=`
    (unsupported). Returns two content blocks: the static ~90-line rules
    block carries an Anthropic prompt-cache breakpoint
    (`cache_control: {"type": "ephemeral", "ttl": "1h"}`, **Phase 6** latency
    work — the 1h TTL keeps the ~3.4k-token prefix warm across the 5-60 min
    gaps typical of single-user usage, which the 5-minute default missed); the
    per-second timestamp is a separate uncached block after it.
  - **Phase 6 guardrails** (the "AI guardrails & semantic safety"
    section): `input_guardrail` (`@before_model(can_jump_to=["end"])`)
    and `output_guardrail` (`@after_model`). Input: a deterministic
    `_INJECTION_PATTERNS` regex pre-filter runs first (a hit →
    `PromptInjection`, classifier skipped), then a Claude Haiku
    classifier (`CLASSIFIER_MODEL`, `.with_structured_output`) tags
    `Safe` / `PromptInjection` / `HarmfulBehavior` / `OffTopic`. A
    flagged turn jumps to end with a canned refusal (neutral for
    injection, friendly redirect for off-topic, genuinely caring +
    `HARM_SUPPORT_RESOURCE` for harmful behavior) — the worker model
    never sees it. Output: `_LEAK_PATTERNS` / `_PRESCRIPTION_PATTERNS`
    regex always run, then an optional Haiku semantic check
    (`GUARDRAIL_OUTPUT_LLM_CHECK`); a flagged reply is swapped for
    `_OUTPUT_FALLBACK` (kept `id` → `add_messages` upsert). **Fails safe**
    — any classifier error/timeout (`GUARDRAIL_TIMEOUT_SECONDS`) blocks /
    replaces, never opens. Blocked turns are tagged on the LangSmith run
    (`guardrail_blocked`, `<category>`). `middleware=[input_guardrail,
    habit_coach_prompt, output_guardrail]`.
  - Memory (**Phase 5**): `AsyncPostgresSaver`, keyed by `thread_id`.
    `build_agent(checkpointer=None)` is an `@asynccontextmanager`, one
    shared instance per server lifetime; `main.py`'s lifespan passes it
    the saver from `postgres_checkpointer(conninfo)` (a small managed
    `AsyncConnectionPool` on Neon's DIRECT endpoint — the saver uses
    server-side prepared statements, which PgBouncer transaction pooling
    rejects). With `checkpointer=None` it falls back to `AsyncSqliteSaver`
    on `CHECKPOINT_DB` — that's the path the eval suite and a
    Postgres-less laptop take. `CHECKPOINT_DB` is kept as a constant so
    the eval suite's monkeypatch of it stays a harmless no-op.
  - Persona: elite Habit Coach, concise, accountable, never guesses data
    — calls tools fresh every time, never assumes state from earlier turns.
  - **Phase 3**: on a reported missed habit, checks
    `get_habit_history_pattern` first, then `get_pending_habits`; if a
    real recurring pattern shows up (not a one-off), suggests a smaller
    micro-commitment version of that same habit — from the agent's own
    judgment, no stored config. Never shames the user. Also parses
    compound natural-language messages ("did my 5k and read a chapter")
    into multiple separate `log_habit` calls in one turn, via `_find_habit`.
  - **Phase 4**: if the user asks for advice, asks why they're failing,
    or reflects on past behavior, must call `query_past_behavior` first
    to retrieve historical context before coaching.
- `src/auth.py` — password hashing + JWT issue/verify +
  `get_current_user_id` dependency. Leaf module, no import cycles. Reads
  `JWT_SECRET_KEY` at import time, fails fast if unset. Tokens expire in
  24h, no refresh flow.
  - **Phase 6**: `require_signup_allowed` dependency — no-op when
    `SIGNUP_SECRET` is unset (local dev, signup open); otherwise a signup
    request must carry a matching `X-Signup-Secret` header
    (`hmac.compare_digest`), else 403. `password_requirement_status` /
    `is_strong_password` unchanged.
- `src/main.py` — FastAPI app. The `lifespan` (Phase 5) does, in order:
  `_configure_request_logging()` (Phase 6 — correlation-id log filter) →
  `init_db()` → open the Postgres checkpointer pool (if a Postgres
  `DATABASE_URL`/`DATABASE_URL_DIRECT` is set, else `None`) →
  `build_agent(checkpointer=…)` → `start_reminder_scheduler(agent)` →
  build the Telegram `Application` via `bot.build_application(_telegram_reply)`,
  `initialize()` / `start()` it, and `set_webhook(TELEGRAM_WEBHOOK_URL,
  secret_token=WEBHOOK_SECRET_TOKEN)` (non-fatal — logs a warning if the
  URL is a placeholder). Teardown reverses it (`delete`/`stop`/`shutdown`,
  scheduler shutdown, pool close) via one `AsyncExitStack`.
  - `POST /auth/signup` — **Phase 6** gated by
    `Depends(require_signup_allowed)` (`SIGNUP_SECRET`) + IP rate limit.
    `POST /auth/login` — IP rate-limited (brute-force). Both → JWT.
  - `POST /chat` — `{message}` only, identity from JWT via
    `Depends(_rate_limit_user_id)` (wraps `get_current_user_id`, stashes
    the id on `request.state` so the limiter keys on the user, not the
    proxy IP), never client-supplied. **Phase 6 gateway checks before the
    agent:** rate limit (per user_id) → size cap (`MAX_MESSAGE_CHARS`,
    413) → `mask_pii` (fail-closed) → daily token budget
    (`MAX_DAILY_QUOTA`, 429). Agent call is wrapped by `_invoke_agent`
    (timeout → `AGENT_UNAVAILABLE_MESSAGE`); token usage recorded after
    via `BackgroundTasks`.
  - `GET /habits/today`, `GET /habits/stats?days=N`, `GET /chat/history`
    — all auth-required, scoped to caller's `user_id`.
  - `POST /evaluate_friction` — **Phase 3**. Manual trigger for testing,
    same auth pattern as `/chat`: identity from
    `Depends(get_current_user_id)` only, never a client-supplied
    `user_id` — this was the exact vulnerability already fixed once in
    `/chat`, never reintroduce it here. Runs the same
    pending-habits-then-agent-nudge flow the scheduler runs automatically.
    Keeps only its IP rate limit — no size cap / masking / budget /
    timeout (it takes no message body; its agent call is in
    `scheduler.run_friction_nudge`, out of the gateway's path).
  - `POST /webhook/telegram` — **Phase 5**, hardened **Phase 6**. Checks
    `X-Telegram-Bot-Api-Secret-Token` == `WEBHOOK_SECRET_TOKEN` (401),
    parses the `Update`, then a per-`chat_id` in-memory sliding-window
    rate limit (`_telegram_rate_ok`, `TELEGRAM_RATE_LIMIT_PER_MIN`) —
    over the limit returns **200 immediately** + a background "slow down"
    reply (so Telegram doesn't retry). Otherwise stashes chat_id /
    message_id / correlation_id / `background_tasks` on
    `_telegram_update_ctx` and calls `telegram_app.process_update(update)`.
  - `GET /healthz` — **Phase 5**. `{"status": "ok"}`, no DB/agent touch.
  - `_telegram_reply(text)` — the callback handed to `build_application`.
    Size cap → `mask_pii` → resolve `HABIT_TRACKER_USERNAME` → `user_id`
    → token budget → `_invoke_agent` on that `thread_id` (mirrors
    `/chat`, minus the JWT). Every user-hidden case (oversized, masking
    failure, budget, timeout) returns a plain reply string, never an
    exception.
  - **Gateway helpers** (`# AI gateway & infrastructure security` +
    `# Rate limiting` sections): `mask_pii` (email / phone / Luhn-checked
    card regex → `<EMAIL_MASKED>` etc.), `_telegram_rate_ok`,
    `_budget_exceeded`, `_turn_token_usage` (sums `usage_metadata` for
    messages after the last `HumanMessage` — `ainvoke` returns the whole
    history), `_invoke_agent` (`asyncio.wait_for` on the agent call),
    `_AgentUnavailable`. `limiter = Limiter(key_func=get_remote_address,
    swallow_errors=True)` (fail-open); `@limiter.limit` on the four
    routes with env-configurable limits (`LOGIN_/SIGNUP_/CHAT_/
    FRICTION_RATE_LIMIT`); `@app.exception_handler(Exception)` →
    generic 500, real error logged server-side.
  - **Correlation ids** (Phase 6.1): `_new_correlation_id(prefix)` opens
    a `ContextVar` scope per request / Telegram update; a logging filter
    prints `[cid=…]` on every line; the id also rides into the agent
    trace metadata.
  - `_agent_config(*, thread_id, tags, metadata, run_name)` builds the
    `RunnableConfig` (LangSmith tags/metadata; drops `None` metadata).
    `/chat` → `run_name="web_chat"`, Telegram → `"telegram_chat"`.
  - `__main__` runs `uvicorn.run(app, host="0.0.0.0",
    port=int(os.environ.get("PORT", 8080)), proxy_headers=True,
    forwarded_allow_ips="*")` — the container entrypoint (`Dockerfile`
    CMD is `uv run python -m src.main`; Railway injects PORT). The proxy
    args let the IP-keyed rate limits see the real caller behind
    Railway's proxy.
- `src/app.py` — Streamlit frontend. NO LangChain/LangGraph logic, HTTP
  only. `BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL",
  "http://localhost:8000")` (**Phase 5** — Streamlit Cloud sets it via
  app secrets). Login/signup gate, JWT in `st.session_state` only. Drops
  to login on `401`. Chat tab + Progress tab (Plotly trend/heatmap, KPI row).
  **Phase 6**: the signup form has a "Signup code" field sent as the
  `X-Signup-Secret` header (matches `auth.require_signup_allowed`).
- `src/bot.py` — **Phase 2, rewritten Phase 5.** A module, NOT a process —
  no polling, no `__main__`. `build_application(on_message)` builds the
  `python-telegram-bot` `Application` + handlers and stashes the
  `on_message` callback in `bot_data`; `_handle_message` calls it.
  Still no LangChain/LangGraph/DB imports — `main.py` injects the
  agent-calling callback (`_telegram_reply`). The old `BackendClient` /
  `/auth/login` / proactive-JWT-refresh machinery is gone (it only
  existed because the bot was a separate HTTP client). `_handle_message`'s
  `except` block replies with a **generic** message and logs the real
  exception server-side — never echoes `exc` (it can carry a DB error
  string or the `HABIT_TRACKER_USERNAME` `RuntimeError`).
- `src/scheduler.py` — **Phase 2/3/4**, unchanged in Phase 5. Runs
  in-process with FastAPI
  (trusted context — may import `database.py` directly, unlike `bot.py`).
  One shared `AsyncIOScheduler` instance, two jobs:
  - Daily 20:00 — resolves `User.id` from `HABIT_TRACKER_USERNAME`,
    invokes the agent in-process for a coached nudge (pattern-check +
    micro-commitment, Phase 3 logic), sends via Telegram if habits pending.
  - Weekly, Sunday 23:59 — **Phase 4**. Calls `summarize_memory`'s
    function directly.
  - Both jobs share this one scheduler instance — never a second one.
  - **Phase 6.1**: `run_friction_nudge(agent, user_id, *, origin,
    correlation_id)` — the shared flow behind the 20:00 job AND
    `/evaluate_friction`. `origin` (`"scheduled"` / `"manual-trigger"`)
    and `correlation_id` ride the LangSmith trace tags/metadata so the
    two callers are distinguishable; they never affect the coaching.
    `_environment()` is duplicated here (not imported) — `main` imports
    this module, so importing back would be circular.
- `src/mcp_server.py` — **Phase 3, standalone learning exercise, NOT
  connected to the production agent.** FastMCP, stdio transport, run
  independently (Claude Desktop, MCP Inspector). Reuses the `tools.py`
  functions via a `_FixedRuntime` stand-in; identity fixed once at
  startup via `HABIT_TRACKER_USERNAME`. Exposes `log_habit`,
  `get_pending_habits`, `list_habits`, `get_weekly_summary` — never
  `delete_habit`. It imports `database.py`, so with a Postgres
  `DATABASE_URL` set it now talks to Neon too (Phase 5 side effect, not a
  deliberate wiring).
- `src/vector_store.py` — **Phase 4, rewritten Phase 5.** `HabitMemoryStore`
  — the ONE place the app touches the vector DB, and the single file the
  ChromaDB → pgvector migration changed. Now wraps a
  `langchain_postgres.PGVector` (`collection_name="habit_summaries"`,
  `use_jsonb=True`), built lazily by `get_vector_store() -> PGVector` and
  reused. Still exposes `.vectorstore` (a generic `VectorStore`) so
  callers use `.similarity_search(query, k=…, filter={"user_id": …})` and
  never a backend-specific method. Same surface: `add_summary(doc_id,
  text, user_id, metadata)`, `similarity_search(...)`,
  `get_habit_memory_store()` singleton. Connects to `DATABASE_URL`
  (normalized to `postgresql+psycopg://`) with `engine_args`
  `connect_args={"prepare_threshold": None}` for Neon's pooled endpoint.
  Embeddings unchanged: `OpenAIEmbeddings("text-embedding-3-small")` —
  the one non-Claude model call in the whole app; `OPENAI_API_KEY`
  required. `HABIT_MEMORY_COLLECTION` env override (default
  `habit_summaries`).
  - **Phase 6.1**: `add_summary` / `similarity_search` carry
    `@traceable` (`run_type` `chain` / `retriever`) with a
    `process_inputs` allow-list scrubber (query / user_id / k / doc_id /
    text length / metadata keys only — never raw secrets).
- `src/summarize_memory.py` — **Phase 4**, unchanged in Phase 5.
  `summarize_user_week(user_id,
  *, today=None)` — importable (not just a script; `scheduler.py` calls
  it directly). Fetches the trailing 7 days of `HabitLog` rows → per-habit
  digest → `ChatAnthropic("claude-sonnet-5")` writes a 3-5 sentence
  third-person behavioral summary (system prompt forbids advice / 2nd
  person / encouragement) → `HabitMemoryStore.add_summary` with
  `doc_id = f"user{id}-week-{date}"` (stable → re-runs overwrite).
  Returns `None` when the user has no habits or no logged activity.
  `__main__` (`uv run python -m src.summarize_memory`) runs it for
  `HABIT_TRACKER_USERNAME`.
  - **Phase 6.1**: `summarize_user_week` carries `@traceable`
    (`run_type="chain"`) with a `process_inputs` scrubber that passes
    only `{user_id, today}`.
- `pytest.ini` — **Phase 4**, root level. Sets `asyncio_mode = auto` so
  `pytest-asyncio` handles the async agent invocations in
  `evaluation/test_rag_agent.py` / `test_guardrails.py` without per-test
  decoration. LangSmith tracing is disabled per-suite by fixtures (see
  below), not here.
- `tests/` — **Phase 6**, root-level, separate from `evaluation/` (same
  `src/` isolation rule — `src/` never imports from here). Non-mocked
  integration tests against real infrastructure.
  - `tests/conftest.py` — `load_dotenv`, forces `LANGSMITH_TRACING=false`,
    `os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")` (Ryuk
    bind-mounts the Docker socket, which Colima rejects). Session fixture
    `pgvector_url`: `TEST_DATABASE_URL` (must differ from `DATABASE_URL`)
    if set, else a disposable `pgvector/pgvector:pg16` container via
    `testcontainers`, else `pytest.skip`.
  - `tests/test_vector_store_integration.py` — real write / read /
    per-user-`filter`-isolation / `doc_id`-upsert tests of
    `vector_store.HabitMemoryStore` against that Postgres. Module-skips
    without `OPENAI_API_KEY` (real `text-embedding-3-small` calls). Uses
    a throwaway collection, torn down after.
- `evaluation/` — **Phase 4, root-level, strictly isolated from `src/`.**
  `evaluation/` may import from `src/`; `src/` never imports from
  `evaluation/`.
  - `evaluation/metrics/custom_empathy.py` — `CoachingEmpathyMetric`, a
    `deepeval.metrics.BaseMetric` subclass (`measure` / `a_measure` /
    `is_successful` / `@property __name__`). One hand-written grading
    prompt → the judge returns `{"score", "reason"}` → parsed. Grades
    acknowledgement-without-shaming AND a concrete actionable alternative
    together; any shaming caps the score at 0.2. Judge:
    `get_judge_model()` → `AnthropicModel("claude-opus-5",
    temperature=0.0)`. `claude-opus-5` was verified via the Models API to
    be the strongest model that still accepts `temperature` (Claude Fable
    5, the only tier above, rejects the sampling params and needs 30-day
    retention) and is a tier above the `claude-sonnet-5` worker. Anthropic
    has no `seed` — temperature=0 is the only determinism lever, and
    DeepEval's wrapper drops it anyway since opus-5 deprecated it. (DeepEval
    4.2's `AnthropicModel` also sends `thinking: disabled` to opus-5, so the
    judge already runs without thinking tokens.)
    `test_rag_agent.py` reuses `get_judge_model()` for `FaithfulnessMetric`
    (which defaults to OpenAI otherwise). `ContextualPrecisionMetric` was
    **removed** from that suite: it grades a retrieval system, but retrieval
    there is mocked to return the golden `retrieval_context` verbatim — it sat
    pinned at 1.00 with no signal about the agent, so it was pure judge cost.
  - `evaluation/datasets/golden_habits.json` — at least 5 golden cases,
    each with `input`, `expected_output`, `retrieval_context`. The
    `retrieval_context` field here is ground truth for what *should* be
    retrievable — it feeds the mock in `test_rag_agent.py`, it is never
    passed directly into a real `LLMTestCase`.
  - `evaluation/conftest.py` — minimal. Carries no vector-store-seeding
    logic (dead code once retrieval is mocked — nothing ever reaches a
    real collection). Holds only fixtures the mocked suite actually needs,
    e.g. loading the golden dataset JSON. Loads `.env` (which sets
    `LANGSMITH_TRACING=true`), so each suite turns tracing back off with
    its own `no_langsmith_tracing` fixture.
  - `evaluation/test_guardrails.py` — **Phase 6**. Drives the real agent
    end to end: safe requests pass through and hit tools; realistic
    injections are intercepted (`category == "PromptInjection"`, no
    tools, exact refusal); harmful-behavior gets the *caring* refusal
    (asserts on category + non-dismissive wording, not exact resource
    text); off-topic gets the redirect; a monkeypatched `_classifier`
    failure fails safe; a monkeypatched `_output_classifier` verdict
    swaps the reply for `_OUTPUT_FALLBACK`. Same isolation as
    `test_rag_agent.py` (unique thread_id, throwaway SQLite).
  - `evaluation/test_gateway_security.py` — **Phase 6**. Drives
    `src/main.py`'s HTTP layer via `TestClient` (lifespan NOT run —
    `app.state` gets a `_FakeAgent` / fake Telegram app; throwaway
    SQLite). Covers rate limiting (user-keyed `/chat`, IP-keyed
    `/auth/login`, chat-id-keyed webhook), the size cap, `mask_pii`
    (incl. valid vs invalid Luhn), budget blocking, the generic error
    handler (no leaked internals), and the agent-timeout graceful
    message. Rate-limit thresholds are set via env vars at the top of
    the file *before* `import src.main` (limits are frozen into the
    decorators at import). No real Anthropic / Neon / Telegram.
  - `evaluation/test_rag_agent.py` — loads the golden dataset,
    `@pytest.mark.parametrize` loops through it. Cases are `async def`
    (they `await` the agent); `pytest.ini`'s `asyncio_mode = auto` runs
    them. For each case:
    - Generates a **unique per-case id** `str(uuid.uuid4().int % 10**12)`
      used as the `thread_id` (never fixed/shared — prevents
      conversation-memory bleed via `AsyncSqliteSaver`). Integer form
      because the tools do `int(thread_id)`; a raw uuid string would
      crash user-id resolution.
    - `autouse` fixture `isolated_state` points `agent.CHECKPOINT_DB` and
      `database.engine`/`SessionLocal` at throwaway temp files (`init_db()`
      on the empty one) — the suite never touches the real DBs.
    - `autouse` fixture `mock_vector_search` swaps
      `src.tools.get_habit_memory_store` for a fake whose
      `.vectorstore.similarity_search` returns `Document`s built from that
      case's golden `retrieval_context`. (Patching
      `VectorStore.similarity_search` on the abstract base is inert
      because the concrete store overrides it — the fake-store swap is the
      working equivalent, provider-agnostic, and needed zero changes when
      Phase 5 swapped Chroma → pgvector.) Tests the agent's reasoning over
      retrieved context, not the real retrieval pipeline.
    - Invokes the real agent directly (`build_agent()` from
      `src/agent.py`, bypassing FastAPI/JWT).
    - After invoking, inspects the agent's final message history for a
      `ToolMessage` named `query_past_behavior` and extracts its content
      as `actual_retrieved_context` — this, not the golden JSON's
      `retrieval_context` field, is what's passed into `LLMTestCase`'s
      `retrieval_context` parameter, since the metrics need the literal
      text the agent actually saw (which should match the mock's output,
      confirming the plumbing works end to end).
    - `await metric.a_measure(test_case)` for `FaithfulnessMetric`
      (`model=get_judge_model()`, or it defaults to OpenAI — the
      RAG-hallucination check: does the reply stay true to the retrieved
      summaries) and `CoachingEmpathyMetric()`; collects per-metric failures
      with score + reason. All 6 cases pass. `ContextualPrecisionMetric` was
      dropped — vacuous under mocked retrieval (always 1.00); the real
      retrieval-relevance check lives in `tests/`.
    - **Phase 6.1**: an autouse `no_langsmith_tracing` fixture forces
      `LANGSMITH_TRACING=false` (+ `get_env_var.cache_clear()`) so the
      suite never exports to the real `habit-tracker` project.
  - The real (non-mocked) vector-store integration test deferred here now
    exists as `tests/test_vector_store_integration.py` (Phase 6). The eval
    suite still mocks retrieval — that's deliberate migration-proofing.
- `Dockerfile` — **Phase 5**. `python:3.13-slim` + `uv`; two-layer cache
  (deps from `pyproject.toml`/`uv.lock` first, then `src/`);
  `CMD ["uv", "run", "python", "-m", "src.main"]` (run as a module —
  `python src/main.py` fails on the relative imports). This is the image
  Railway builds.
- `.dockerignore` — **Phase 5**. Keeps `.env`, `.venv/`, `evaluation/`,
  local DB files, `deploy/` etc. out of the build context.
- `requirements.txt` — **Phase 5**. Streamlit Cloud's dependency file —
  `streamlit`, `plotly`, `requests` ONLY. Do NOT add the backend stack;
  the frontend imports nothing else.
- `run.sh` — starts backend + frontend together; port-based cleanup trap.

# Architecture Notes
- **Deployed:** FastAPI backend on **Railway** (Docker). Deploys are
  `railway up` from the CLI — NOT `git push` (the service is not connected
  to GitHub auto-deploy; a push alone changes nothing). One **Neon**
  Postgres database; Streamlit frontend headed for **Streamlit Cloud**
  (not yet deployed). Telegram reaches the backend by webhook. No AWS, no
  GCP, no `supervisord`, no CI pipeline.
- **Observability (Phase 6.1):** LangSmith tracing, project
  `"habit-tracker"`, enabled purely by env (`LANGSMITH_TRACING` /
  `_API_KEY` / `_PROJECT`). LangChain auto-traces the agent graph; the
  code adds tags (`web`/`telegram`/`friction-check`, `guardrail_blocked`,
  …), metadata (user_id, telegram_chat_id, correlation_id, environment),
  and `run_name`. Trace export is out-of-band — a LangSmith outage never
  fails a request or a Telegram retry.
- **Safety (Phase 6):** two layers. Guardrails in the agent
  (`agent.py` middleware — input classification short-circuits the
  worker model, output check on the reply) and a gateway in front of it
  (`main.py` — size cap, PII masking, per-day token budget, timeout,
  generic errors, rate limits). Both fail safe: the guardrail blocks on
  classifier error; masking is fail-closed; the rate limiter is
  fail-open (infra hiccup shouldn't 500 a user).
- **One Neon database, three concerns:** the relational tables
  (`users`/`habits`/`habit_logs`), the LangGraph checkpoint tables
  (`checkpoints*`), and pgvector (`langchain_pg_*`, collection
  `habit_summaries`). The app + vector store use Neon's POOLED endpoint;
  the checkpointer uses the DIRECT endpoint.
- **Identity is real JWT-based auth.** `user_id` = numeric `User.id`,
  resolved server-side from a verified token, never client input.
  Doubles as LangGraph `thread_id`. Telegram messages resolve to the one
  `HABIT_TRACKER_USERNAME` account server-side (no JWT on that path).
- **Habits are scoped per user** (`Habit.user_id` FK) — enforced
  server-side everywhere.
- **`habit_summaries` (pgvector, via `vector_store.HabitMemoryStore`) is
  scoped per user the same way** — tagged on write, `filter={"user_id":
  …}` on read, both via the resolved `user_id`, never a global unscoped
  collection. Embedded with OpenAI `text-embedding-3-small`.

# Workflows
- Install: `uv add <dependency>`
- Local dev: leave `DATABASE_URL` unset → SQLite + `AsyncSqliteSaver`
  fallback, no Postgres needed. Set `DATABASE_URL` (+ `DATABASE_URL_DIRECT`)
  to point local runs at Neon.
- Run everything: `./run.sh` (backend `:8000`, Streamlit `:8501`)
- Backend only: `uv run uvicorn src.main:app --reload`
- Frontend only: `uv run streamlit run src/app.py`
- MCP server only (learning/testing): `uv run python -m src.mcp_server`
- Telegram (local): `src/bot.py` is no longer standalone. Run the backend
  behind a tunnel (`cloudflared tunnel --url http://localhost:8000`) and
  set `TELEGRAM_WEBHOOK_URL` to `https://<tunnel>/webhook/telegram`.
- Trigger a nudge manually: `POST /evaluate_friction` while logged in
- Generate this week's memory summary now: `uv run python -m src.summarize_memory`
- Run the RAG/coaching evaluation suite: `uv run pytest evaluation/test_rag_agent.py`
  — real Anthropic + OpenAI-embedding calls; per golden case ~1 agent turn +
  the opus-5 judge for `FaithfulnessMetric` (several sub-calls) and
  `CoachingEmpathyMetric` (one call), ~2.5-3 min for the 6 cases. Needs
  `ANTHROPIC_API_KEY` + `OPENAI_API_KEY` (does NOT need `DATABASE_URL` — it
  mocks retrieval and uses a throwaway SQLite). Transient `529 Overloaded`
  from Anthropic can fail a case on the judge — retry that case.
- Run the guardrail suite: `uv run pytest evaluation/test_guardrails.py`
  — real Anthropic (Haiku classifier + Sonnet worker on the pass-through
  cases). Throwaway SQLite; no `DATABASE_URL`.
- Run the gateway/security suite: `uv run pytest evaluation/test_gateway_security.py`
  — fully offline (fake agent), fast, no API keys needed beyond what
  importing `src` requires.
- Run the pgvector integration test: `uv run pytest tests/` — needs Docker
  (spins up a `pgvector/pgvector` container) OR `TEST_DATABASE_URL` set to
  a scratch DB, plus `OPENAI_API_KEY`. Skips cleanly if neither is
  available. NEVER point `TEST_DATABASE_URL` at the real `DATABASE_URL`
  (asserted).
- Deploy the backend: **`railway up`** (uploads the build context; e.g.
  `railway up --service <name> --ci`). `git push` does NOT deploy — the
  service isn't wired to GitHub. Commit + push for history, then
  `railway up`. Env is set via `railway variables` / dashboard, not
  committed. Verify with `railway status --json` (`cliCaller`, `reason`)
  and `/healthz`.
- Inspect the DB (read-only): `psql "$DATABASE_URL" -c "select * from habits;"`
  (or the SQLite equivalent locally).

# Rules
- `create_agent` from `langchain.agents` only, never
  `langgraph.prebuilt.create_react_agent`. Verify the Claude model string
  before hardcoding.
- `.env`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (embeddings only — see
  below), `JWT_SECRET_KEY` (app-generated via `secrets.token_hex(32)`),
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HABIT_TRACKER_USERNAME`/
  `PASSWORD` (`PASSWORD` is now only the Streamlit login — the bot no
  longer logs in), plus **Phase 5**: `DATABASE_URL` (Neon POOLED
  endpoint), `DATABASE_URL_DIRECT` (Neon un-pooled — for the
  checkpointer), `TELEGRAM_WEBHOOK_URL` (public backend URL +
  `/webhook/telegram`), `WEBHOOK_SECRET_TOKEN` (any random string).
  Leave `DATABASE_URL` unset for a SQLite-backed local run. On Railway
  these are set via `railway variables`, never committed.
  - **Phase 6**, all optional (sane defaults; full docs in `.env.example`):
    `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT`
    (`habit-tracker`) — tracing off if unset. `SIGNUP_SECRET` — gates
    `/auth/signup` (open if unset). `LOGIN_/SIGNUP_/CHAT_/FRICTION_RATE_LIMIT`
    (slowapi strings), `TELEGRAM_RATE_LIMIT_PER_MIN` (int). `MAX_MESSAGE_CHARS`
    (1000), `MAX_DAILY_QUOTA` (200000 tokens/user/day), `AGENT_TIMEOUT_SECONDS`
    (30). `GUARDRAIL_TIMEOUT_SECONDS` (8), `GUARDRAIL_OUTPUT_LLM_CHECK` (on),
    `HARM_SUPPORT_RESOURCE` (override the support-resource text in the
    harmful-behavior refusal). `WORKER_EFFORT` (`medium`) — adaptive-thinking
    depth for the coaching model; `low`/`medium`/`high`. `TEST_DATABASE_URL`
    — scratch DB for `tests/` (must differ from `DATABASE_URL`).
- **Checkpointer uses Neon's DIRECT endpoint; everything else uses the
  POOLED endpoint.** `postgres_checkpointer()` runs server-side prepared
  statements (`prepare_threshold=0`) that PgBouncer transaction pooling
  rejects; the relational engine and `PGVector` set
  `prepare_threshold=None` and work through the pooler.
- **Telegram is a webhook (`POST /webhook/telegram`), driven from
  `main.py`'s lifespan.** Never re-add `run_polling` / a standalone
  `bot.py` process — polling can't run on Railway/serverless hosting.
- Provider split (deliberate): every model that GENERATES, JUDGES, or
  CLASSIFIES text is Claude — the agent and weekly summariser on
  `claude-sonnet-5`, the guardrail input/output classifiers on
  `claude-haiku-4-5-20251001` (fastest tier; `agent.CLASSIFIER_MODEL`),
  the DeepEval judge on `claude-opus-5`. `OPENAI_API_KEY` is used for
  exactly one thing: `OpenAIEmbeddings("text-embedding-3-small")` in
  `vector_store.py` (the Railway image keeps `openai` only because
  `langchain-openai` depends on it; `deepeval`, which also pulls `openai`,
  is dev-only now). Do not route any generation, judging, or classification
  through OpenAI — including in error/fallback paths.
- Tools derive the acting user only from `runtime` (ToolRuntime), never
  an LLM-supplied argument.
- `app.py`: no LangChain/LangGraph/DB logic, HTTP only. `bot.py`: no
  LangChain/LangGraph/DB imports either — it takes an `on_message`
  callback from `main.py` and does Telegram I/O only. `scheduler.py` and
  `mcp_server.py` are trusted in-process/fixed-identity exceptions.
- `scheduler.py`'s two jobs (daily nudge, weekly summary) share ONE
  `AsyncIOScheduler` instance — never a second one. Shares the same
  `lifespan` in `main.py` as the agent.
- Micro-commitment suggestions come from the agent's own judgment, only
  on a genuine recurring pattern — never a stored config, never on a
  single isolated miss.
- `POST /evaluate_friction` and the daily scheduler job run the same
  underlying flow — don't implement the pending-habit-check logic twice.
- `mcp_server.py` is standalone/experimental — never wired into
  `agent.py`'s actual tool bindings without a separate, explicit
  decision. Never exposes `delete_habit`. No tool takes a `user_id`
  parameter — identity fixed once via `HABIT_TRACKER_USERNAME` at
  startup only.
- `habit_memory` entries and `query_past_behavior` queries are always
  scoped to the resolved `user_id` — never global, never LLM-filterable.
- Embeddings are `OpenAIEmbeddings("text-embedding-3-small")`, set once in
  `vector_store.py`. This is the ONLY OpenAI call in the app — never add
  another, and never route generation/judging through OpenAI.
- All vector-store access goes through `HabitMemoryStore` in
  `vector_store.py` — the single file the pgvector migration touched, and
  still the only place to change the vector backend.
- `query_past_behavior` must call the generic `VectorStore.similarity_search`
  interface (`store.vectorstore.similarity_search(...)`), never a
  backend-specific API — this is why the Chroma → pgvector migration
  needed zero changes to the tool or the eval suite. Keep it that way.
- `evaluation/` may import from `src/`; `src/` never imports from
  `evaluation/`.
- Golden dataset tests swap `get_habit_memory_store` for a fake whose
  `.vectorstore.similarity_search` returns the golden `retrieval_context`
  as `Document`s — no real vector collection is ever seeded (intentional
  migration-proofing — it survived the pgvector swap untouched).
  `conftest.py` carries no seeding logic — don't re-add it "just in case."
- Every parametrized evaluation test case uses a **unique per-case id**
  (`str(uuid.uuid4().int % 10**12)`) as its `thread_id` — never a fixed
  or shared one (leaks conversation state between cases via the
  checkpointer). Integer form because the tools do `int(thread_id)`.
- `LLMTestCase.retrieval_context` is always the actual `ToolMessage`
  content extracted post-invocation, never the golden dataset's
  `retrieval_context` field directly — that field only seeds the mock's
  return value via `retrieval_context` → `Document.page_content`.
- The DeepEval Judge model runs at `temperature=0.0` to reduce test
  flakiness. No `seed` parameter is set — Anthropic's API has no
  equivalent to OpenAI's `seed`, so don't add one under the assumption
  it will work.
- `test_rag_agent.py` grades with `FaithfulnessMetric` + `CoachingEmpathyMetric`
  only. Do NOT re-add `ContextualPrecisionMetric` (or any retrieval-system
  metric): retrieval is mocked to return the golden context verbatim, so it
  has no signal and only burns judge tokens. Retrieval correctness is
  `tests/test_vector_store_integration.py`'s job.
- The real (non-mocked) vector-store integration test now lives in
  `tests/test_vector_store_integration.py` (Phase 6) — the eval suite
  still mocks retrieval on purpose. Never run either against the real
  `DATABASE_URL`.
- **Guardrails (Phase 6) live as `create_agent` middleware, never a
  hand-built `StateGraph`** — the `AsyncPostgresSaver` checkpointer
  persists at node boundaries, and a bespoke graph would fight that.
  The deterministic regex pre-filter ALWAYS runs before the classifier
  call and short-circuits on a hit. Classifier failure/timeout must fail
  SAFE (block / replace), never open. Don't put a `Field(max_length=…)`
  on an LLM free-text output field — the model overruns it and structured
  parsing throws (this already caused a fail-safe on a good reply once).
- **The gateway (Phase 6) covers exactly the two agent entry points —
  `/chat` and the Telegram webhook.** `/evaluate_friction` is out of its
  path by design. Order at each entry point: rate limit → size cap →
  PII masking → token budget → timed agent call → record usage. Masking
  is fail-CLOSED; the rate limiter is fail-OPEN; the budget check is
  fail-open.
- **PII masking is `main.mask_pii` (plain `re`) — never add Presidio or
  another NLP dep.** It's applied at the entry point only; raw chat text
  never reaches `summarize_memory` / the embedding path (that's built
  from `HabitLog` rows, and habit names the agent creates are already
  masked). `vector_store.py` / `summarize_memory.py` need no masking.
- **`_turn_token_usage` sums `usage_metadata` only for messages after the
  last `HumanMessage`** — `agent.ainvoke` returns the whole accumulated
  history; summing all of it re-counts every prior turn.
- **LangSmith calls are best-effort and wrapped** — `get_current_run_tree()`,
  `@traceable`, tag adds all sit in try/except or are pure dict-building.
  A tracing failure must never fail a user request. Test suites that
  touch real Claude force `LANGSMITH_TRACING=false` via a fixture.
- **`client.list_runs()` is deprecated** (removed after 2027-01-31) — for
  reading runs use `client.runs.query()`. The app itself never reads
  runs; the "legacy API usage detected" LangSmith banner comes from
  ad-hoc `list_runs()` debugging calls and self-clears.
- No circular imports (`main → agent → tools → database`; `main → bot`
  with no cycle back; `auth` is a leaf; `main → scheduler` /
  `main → agent` with `_environment` duplicated in `scheduler` to avoid
  a cycle back to `main`).
- Never run tests against the real Neon database or a real
  `habits.db`/`checkpoints.db` — use an isolated throwaway DB (the eval
  suite already does). Read-only inspection is fine.
- Kill whatever's on the port before restarting (`lsof -ti:8000`,
  `-ti:8501`) — don't touch unrelated processes.
- Single-user scope throughout: one account, one `TELEGRAM_CHAT_ID` — no
  multi-user logic without discussing it first.
- **On any missing import, circular dependency, ambiguity, or other
  issue: stop and report it. Never patch or work around it silently.**

# Known Limitations
- Frequency parsing only understands `"week"` (rolling 7-day) and
  `"except <Weekday>"` — anything more complex stores fine but falls
  back to daily-style behavior everywhere (dashboard, stats, patterns).
- `frequency`/`status` are unvalidated free text end-to-end.
- JWT has no refresh flow — Streamlit re-logs in on expiry. (Nothing
  compensates any more; the bot no longer holds a JWT.)
- `mcp_server.py` is a separate learning exercise, not integrated —
  tools/data there don't reflect the Phase 6 guardrails or gateway; its
  only safeguards are the ones listed above.
- The evaluation suite tests agent reasoning over mocked retrieval; the
  real vector-store pipeline is covered separately by `tests/` (Phase 6).
- The token budget counts only the worker model (`claude-sonnet-5`); the
  Haiku guardrail classifier's tokens are not metered. PII masking is a
  regex heuristic — it will miss unusual formats and can over-mask an
  odd bare 10-15 digit run.
- **Deployment gaps:** the Streamlit frontend is prepared for Streamlit
  Cloud but not actually deployed. `src/bot.py` is no longer runnable on
  its own (`python -m src.bot` is a no-op) — local Telegram testing needs
  a tunnel + `TELEGRAM_WEBHOOK_URL`. The `deploy/launchd/` scripts still
  target the old always-on-Mac model and their `bot` service now runs
  dead code — superseded by Railway. There is no `docker compose` for a
  local Postgres; local dev uses the SQLite fallback or points at Neon.
- Neon free tier suspends the compute after ~5 min idle — the first
  request after idle pays a ~0.5s reconnect (the pool's `check` handles
  the stale connection).
- Rate limiting and the Telegram sliding window are **in-memory /
  per-process** — correct for the single Railway instance, but a
  horizontally-scaled deploy would need a shared store (Redis).
- `/auth/signup` is gated by `SIGNUP_SECRET` in deployment, but stays
  fully open whenever that var is unset (local dev, or a misconfigured
  deploy).

