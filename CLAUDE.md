# Project Overview
- AI Habit Tracker — chat-based habit coaching agent (Anthropic-powered)
  with a Streamlit dashboard, FastAPI backend, SQLite storage.
- Phase 1 (complete): chat to create/log habits, live Today's Dashboard,
  real per-user JWT auth, Progress tab with interactive Plotly charts.
- Phase 2 (complete): Telegram bot + daily 8PM reminder scheduler,
  single-user scope, proactive JWT refresh.
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
- Phase 5 (planned): database + deployment hardening, in this order —
  (1) migrate SQLite → PostgreSQL and ChromaDB → pgvector, (2)
  containerize the whole app (FastAPI + Streamlit + Telegram bot) into
  one Docker image supervised by `supervisord`, (3) CI/CD via GitHub
  Actions building/pushing that image to a registry and deploying it.
  Migration first, then containerize, so the image isn't rebuilt twice.

# Tech Stack
- Backend: FastAPI, LangGraph, LangChain (Anthropic), SQLAlchemy, SQLite
- Frontend: Streamlit, Plotly
- Auth: bcrypt (not passlib — incompatible with bcrypt≥4.1), pyjwt
- Phase 2: python-telegram-bot (v20+, async), apscheduler, httpx
- Phase 3: `mcp` (FastMCP, standalone learning server only)
- Phase 4: `chromadb` + `langchain-chroma` (vector store), `langchain-openai`
  (`text-embedding-3-small` embeddings only), `deepeval` + `pytest` +
  `pytest-asyncio` (all in `[project.dependencies]`, per the phase spec).
- Package manager: uv
- `create_agent` lives in `langchain.agents`, not `langchain-anthropic`

# Project Structure
- `src/__init__.py` — package root; relative imports throughout; run via
  `uvicorn src.main:app` from project root.
- `src/database.py` — models + shared logic.
  - `User(id, username, hashed_password)`; `Habit(id, user_id FK, name,
    frequency)`; `HabitLog(id, habit_id, date, status)`.
  - `frequency`/`status` are free text, not enums.
  - `init_db()` additive/idempotent, never drops data; handles the
    nullable-column migration + `backfill_orphaned_habits()` for
    pre-auth data.
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
    Chroma-specific API — required both for test mocking and the planned
    Phase 5 pgvector swap) against the `habit_memory` collection,
    filtered to the caller's own `user_id` (via `ToolRuntime`, same as
    every other tool — never an LLM-supplied value). Returns top 3
    matches.
  - Name resolution via `_find_habit`: exact → case-insensitive substring
    → asks to disambiguate on multiple matches.
- `src/agent.py` — built via `langchain.agents.create_agent` (never the
  deprecated `langgraph.prebuilt.create_react_agent`).
  - Model: `ChatAnthropic(model="claude-sonnet-5")` — verify current
    before assuming.
  - Dynamic system prompt via `@dynamic_prompt` middleware
    (`langchain.agents.middleware`) — NOT a callable to `system_prompt=`
    (unsupported). Injects current date/time.
  - Memory: `AsyncSqliteSaver`, keyed by `thread_id`. `build_agent()` is
    an `@asynccontextmanager`, one shared instance per server lifetime.
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
- `src/main.py` — FastAPI app.
  - `POST /auth/signup`, `POST /auth/login` → JWT.
  - `POST /chat` — `{message}` only, identity from JWT via
    `Depends(get_current_user_id)`, never client-supplied.
  - `GET /habits/today`, `GET /habits/stats?days=N`, `GET /chat/history`
    — all auth-required, scoped to caller's `user_id`.
  - `POST /evaluate_friction` — **Phase 3**. Manual trigger for testing,
    same auth pattern as `/chat`: identity from
    `Depends(get_current_user_id)` only, never a client-supplied
    `user_id` — this was the exact vulnerability already fixed once in
    `/chat`, never reintroduce it here. Runs the same
    pending-habits-then-agent-nudge flow the scheduler runs automatically.
- `src/app.py` — Streamlit frontend. NO LangChain/LangGraph logic, HTTP
  only. Login/signup gate, JWT in `st.session_state` only. Drops to login
  on `401`. Chat tab + Progress tab (Plotly trend/heatmap, KPI row).
- `src/bot.py` — **Phase 2**. Own process, independently runnable. NO
  LangChain/LangGraph/DB logic — HTTP only. Logs in via `/auth/login`
  using `HABIT_TRACKER_USERNAME`/`PASSWORD`, caches JWT + issue time.
  Proactively refreshes before ~23h of age; 401-retry is a fallback only,
  not the primary mechanism. Forwards Telegram messages to `/chat`.
- `src/scheduler.py` — **Phase 2/3/4**. Runs in-process with FastAPI
  (trusted context — may import `database.py` directly, unlike `bot.py`).
  One shared `AsyncIOScheduler` instance, two jobs:
  - Daily 20:00 — resolves `User.id` from `HABIT_TRACKER_USERNAME`,
    invokes the agent in-process for a coached nudge (pattern-check +
    micro-commitment, Phase 3 logic), sends via Telegram if habits pending.
  - Weekly, Sunday 23:59 — **Phase 4**. Calls `summarize_memory`'s
    function directly.
  - Both jobs share this one scheduler instance — never a second one.
- `src/mcp_server.py` — **Phase 3, standalone learning exercise, NOT
  connected to the production agent.** FastMCP, stdio transport, run
  independently (Claude Desktop, MCP Inspector), reads/writes the same
  `habits.db`. Exposes `log_habit`, `get_pending_habits`, `list_habits`,
  `get_weekly_summary` — never `delete_habit` (its only safety net is the
  main app's confirm-before-delete prompt, not the tool itself). Identity
  fixed once at server startup via `HABIT_TRACKER_USERNAME` env var — no
  tool takes a `user_id` parameter.
- `src/vector_store.py` — **Phase 4**. `HabitMemoryStore` — the ONE place
  the app touches the vector DB (swap ChromaDB → pgvector in Phase 5 by
  rewriting this file only). Holds a LangChain `Chroma` and exposes it as
  `.vectorstore` (a generic `VectorStore`), so callers use
  `.similarity_search(query, k=…, filter={"user_id": …})` and never a
  Chroma-specific method. Surface: `add_summary(doc_id, text, user_id,
  metadata)`, `similarity_search(...)`, `get_habit_memory_store()`
  singleton. Embeddings: `OpenAIEmbeddings("text-embedding-3-small")` —
  the one non-Claude model call in the whole app; `OPENAI_API_KEY`
  required. `chroma_db/` persist path (gitignored); `CHROMA_PATH` /
  `HABIT_MEMORY_COLLECTION` env overrides.
- `src/summarize_memory.py` — **Phase 4**. `summarize_user_week(user_id,
  *, today=None)` — importable (not just a script; `scheduler.py` calls
  it directly). Fetches the trailing 7 days of `HabitLog` rows → per-habit
  digest → `ChatAnthropic("claude-sonnet-5")` writes a 3-5 sentence
  third-person behavioral summary (system prompt forbids advice / 2nd
  person / encouragement) → `HabitMemoryStore.add_summary` with
  `doc_id = f"user{id}-week-{date}"` (stable → re-runs overwrite).
  Returns `None` when the user has no habits or no logged activity.
  `__main__` (`uv run python -m src.summarize_memory`) runs it for
  `HABIT_TRACKER_USERNAME`.
- `pytest.ini` — **Phase 4**, root level. Sets `asyncio_mode = auto` so
  `pytest-asyncio` handles the agent's async invocation in
  `evaluation/test_rag_agent.py` without per-test decoration. No
  LangSmith/tracing configuration — not in use yet, deferred to Phase 6
  when a security/observability layer is added.
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
    DeepEval's wrapper drops it anyway since opus-5 deprecated it.
    `test_rag_agent.py` reuses `get_judge_model()` for the two built-in
    RAG metrics (which default to OpenAI otherwise).
  - `evaluation/datasets/golden_habits.json` — at least 5 golden cases,
    each with `input`, `expected_output`, `retrieval_context`. The
    `retrieval_context` field here is ground truth for what *should* be
    retrievable — it feeds the mock in `test_rag_agent.py`, it is never
    passed directly into a real `LLMTestCase`.
  - `evaluation/conftest.py` — minimal. Carries no Chroma-seeding logic
    (removed — dead code once retrieval is mocked, since nothing ever
    reaches a real Chroma collection). Holds only fixtures the mocked
    suite actually needs, e.g. loading the golden dataset JSON.
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
      case's golden `retrieval_context`. (The plan called for patching
      `VectorStore.similarity_search` directly, but `Chroma` overrides
      that method so patching the abstract base is inert — the fake-store
      swap is the working equivalent, still provider-agnostic and
      unchanged by the Phase 5 pgvector swap.) Tests the agent's reasoning
      over retrieved context, not the real retrieval pipeline.
    - Invokes the real agent directly (`build_agent()` from
      `src/agent.py`, bypassing FastAPI/JWT).
    - After invoking, inspects the agent's final message history for a
      `ToolMessage` named `query_past_behavior` and extracts its content
      as `actual_retrieved_context` — this, not the golden JSON's
      `retrieval_context` field, is what's passed into `LLMTestCase`'s
      `retrieval_context` parameter, since the metrics need the literal
      text the agent actually saw (which should match the mock's output,
      confirming the plumbing works end to end).
    - `await metric.a_measure(test_case)` for `FaithfulnessMetric`,
      `ContextualPrecisionMetric` (both passed `model=get_judge_model()`,
      or they default to OpenAI) and `CoachingEmpathyMetric()`; collects
      per-metric failures with score + reason. All 6 cases currently pass.
  - **Deferred to Phase 5**: a real (non-mocked) integration test of the
    vector store pipeline is written once, after the pgvector migration,
    against whichever database is final — not built now against Chroma,
    since it would just be rewritten.
- `run.sh` — starts backend + frontend together; port-based cleanup trap.

# Architecture Notes
- **Identity is real JWT-based auth.** `user_id` = numeric `User.id`,
  resolved server-side from a verified token, never client input.
  Doubles as LangGraph `thread_id`.
- **Habits are scoped per user** (`Habit.user_id` FK) — enforced
  server-side everywhere.
- **`habit_memory` (Chroma, via `vector_store.HabitMemoryStore`) is
  scoped per user the same way** — tagged on write, `filter={"user_id":
  …}` on read, both via the resolved `user_id`, never a global unscoped
  collection. Embedded with OpenAI `text-embedding-3-small`.

# Workflows
- Install: `uv add <dependency>`
- Run everything: `./run.sh`
- Backend only: `uv run uvicorn src.main:app --reload`
- Frontend only: `uv run streamlit run src/app.py`
- Bot only (testing): `uv run python -m src.bot`
- MCP server only (learning/testing): `uv run python -m src.mcp_server`
- Trigger a nudge manually: `POST /evaluate_friction` while logged in
- Generate this week's memory summary now: `uv run python -m src.summarize_memory`
- Run the evaluation suite: `uv run pytest evaluation/` — real Anthropic +
  OpenAI-embedding calls; ~1 agent + 3 opus-5 judge calls per golden case,
  ~3.5 min for the 6 cases. Needs `ANTHROPIC_API_KEY` + `OPENAI_API_KEY`.
- Inspect DB (read-only): `sqlite3 -readonly -header -column habits.db "SELECT * FROM habits;"`

# Rules
- `create_agent` from `langchain.agents` only, never
  `langgraph.prebuilt.create_react_agent`. Verify the Claude model string
  before hardcoding.
- `.env`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (embeddings only — see
  below), `JWT_SECRET_KEY` (app-generated via `secrets.token_hex(32)`),
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HABIT_TRACKER_USERNAME`/
  `PASSWORD` — all user-managed except `JWT_SECRET_KEY`.
- Provider split (deliberate): every model that GENERATES or JUDGES text
  is Claude — the agent and weekly summariser on `claude-sonnet-5`, the
  DeepEval judge on `claude-opus-5`. `OPENAI_API_KEY` is used for exactly
  one thing: `OpenAIEmbeddings("text-embedding-3-small")` in
  `vector_store.py`. `openai` is also DeepEval's unconditional base
  dependency. Do not route any generation or judging through OpenAI.
- Tools derive the acting user only from `runtime` (ToolRuntime), never
  an LLM-supplied argument.
- `app.py` and `bot.py`: no LangChain/LangGraph/DB logic, HTTP only.
  `scheduler.py` and `mcp_server.py` are trusted in-process/fixed-identity
  exceptions.
- `bot.py` refreshes its JWT proactively (~23h); 401-retry is a fallback
  only.
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
  `vector_store.py` — the single file to rewrite for the Phase 5 pgvector
  swap.
- `query_past_behavior` must call the generic `VectorStore.similarity_search`
  interface (`store.vectorstore.similarity_search(...)`), never a
  Chroma-specific API — required for both test mocking and the Phase 5
  pgvector swap.
- `evaluation/` may import from `src/`; `src/` never imports from
  `evaluation/`.
- Golden dataset tests swap `get_habit_memory_store` for a fake whose
  `.vectorstore.similarity_search` returns the golden `retrieval_context`
  as `Document`s — no real Chroma collection is ever seeded (intentional
  migration-proofing). `conftest.py` carries no Chroma-seeding logic —
  don't re-add it "just in case."
- Every parametrized evaluation test case uses a **unique per-case id**
  (`str(uuid.uuid4().int % 10**12)`) as its `thread_id` — never a fixed
  or shared one (leaks conversation state between cases via
  `AsyncSqliteSaver`). Integer form because the tools do `int(thread_id)`.
- `LLMTestCase.retrieval_context` is always the actual `ToolMessage`
  content extracted post-invocation, never the golden dataset's
  `retrieval_context` field directly — that field only seeds the mock's
  return value via `retrieval_context` → `Document.page_content`.
- The DeepEval Judge model runs at `temperature=0.0` to reduce test
  flakiness. No `seed` parameter is set — Anthropic's API has no
  equivalent to OpenAI's `seed`, so don't add one under the assumption
  it will work.
- A real (non-mocked) integration test of the vector store pipeline is
  deferred to Phase 5, written once against the final pgvector setup —
  not built now against Chroma.
- No circular imports (`main → agent → tools → database`, `auth` is a leaf).
- Never delete/overwrite real `habits.db`/`checkpoints.db` for testing —
  use an isolated throwaway DB. Read-only inspection is fine.
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
- JWT has no refresh flow (Streamlit: re-login on expiry). `bot.py`
  compensates with proactive refresh.
- `mcp_server.py` is a separate learning exercise, not integrated —
  tools/data there don't reflect any additional safeguards beyond what's
  listed above.
- The evaluation suite tests agent reasoning over mocked retrieval, not
  the real vector store pipeline — see the Phase 5 deferred integration
  test note above.
- `deepeval` / `pytest` / `pytest-asyncio` sit in `[project.dependencies]`
  (per the Phase 4 spec), so they ship with a plain `uv sync` — a lean
  production image would move them to a group in Phase 5's containerize
  step. `openai` rides in as `langchain-openai`'s and `deepeval`'s dep.
- No LangSmith/observability tracing yet — planned for Phase 6 alongside
  a security layer, not currently configured anywhere.
- No containerization or CI/CD yet — planned for Phase 5, after the
  database migration in that same phase.

