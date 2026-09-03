"""FastAPI service exposing the habit coaching agent over HTTP."""

import asyncio
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict, deque
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from datetime import date, timedelta

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from telegram import Update

load_dotenv()

from .agent import build_agent, postgres_checkpointer
from .bot import build_application
from .auth import (
    create_access_token,
    get_current_user_id,
    hash_password,
    password_requirement_status,
    require_signup_allowed,
    verify_password,
)
from .database import (
    Habit,
    User,
    backfill_orphaned_habits,
    daily_token_total,
    get_session,
    init_db,
    is_due_today,
    is_satisfied,
    record_token_usage,
    satisfaction_window_days,
)
from .scheduler import run_friction_nudge, start_reminder_scheduler

# The scheduler's Telegram calls go through python-telegram-bot, which logs
# every request through httpx at INFO with the bot token in the URL — keep
# httpx at WARNING so the token never lands in the server logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# --- Observability (Phase 6.1) --------------------------------------------
# LangSmith tracing is enabled purely by env (LANGSMITH_TRACING / _API_KEY /
# _PROJECT="habit-tracker" in .env, set via `railway variables` in deployment).
# LangChain auto-traces the agent graph; the code here only enriches those
# traces with tags/metadata and adds a per-request correlation id to the logs.
# Nothing below fails a request if LangSmith is unset or unreachable — trace
# export is out-of-band, and every enrichment is a plain dict passed to the
# agent, never a network call.

# Per-request / per-Telegram-update correlation id, stamped on every log line.
_correlation_id: ContextVar[str] = ContextVar("habit_correlation_id", default="-")
# Populated by POST /webhook/telegram before process_update() runs inline, so
# _telegram_reply (which the bot.py callback only hands the message text) can
# still put chat_id / message_id on the trace.
_telegram_update_ctx: ContextVar[dict] = ContextVar("habit_telegram_update_ctx", default={})


class _CorrelationIdLogFilter(logging.Filter):
    """Stamps the current correlation id onto every LogRecord so it can render
    on every log line for the request that is being handled."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()
        return True


def _new_correlation_id(prefix: str) -> str:
    """Open a new correlation scope for the current request/update and return
    its id — carried on every subsequent log line and copied into the agent
    trace metadata."""
    cid = f"{prefix}-{uuid.uuid4().hex[:12]}"
    _correlation_id.set(cid)
    return cid


def _configure_request_logging() -> None:
    """Route the app's own logs to stdout with the correlation id prefixed, and
    stamp the id onto records flowing through uvicorn's handlers too. Called
    once at startup; wrapped so a logging quirk can never break the server."""
    try:
        cid_filter = _CorrelationIdLogFilter()
        root = logging.getLogger()
        stream = next(
            (h for h in root.handlers if isinstance(h, logging.StreamHandler)), None
        )
        if stream is None:
            stream = logging.StreamHandler(sys.stdout)
            root.addHandler(stream)
        stream.addFilter(cid_filter)
        stream.setFormatter(
            logging.Formatter("[cid=%(correlation_id)s] %(levelname)s %(name)s: %(message)s")
        )
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            for handler in logging.getLogger(name).handlers:
                handler.addFilter(cid_filter)
    except Exception:  # never fail startup over logging config
        logger.warning("Could not attach correlation-id logging.", exc_info=True)


def _environment() -> str:
    """Deployment environment label for LangSmith trace metadata (Phase 6.1)."""
    return (
        os.environ.get("APP_ENV")
        or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or "local"
    )


def _agent_config(*, thread_id: str, tags: list[str], metadata: dict, run_name: str) -> dict:
    """A RunnableConfig for an agent.ainvoke() call: the thread_id that scopes
    every tool to this user, plus LangSmith trace tags/metadata. None-valued
    metadata is dropped. Never put a JWT, WEBHOOK_SECRET_TOKEN, the bot token,
    or DB credentials in here."""
    return {
        "configurable": {"thread_id": thread_id},
        "tags": tags,
        "metadata": {key: value for key, value in metadata.items() if value is not None},
        "run_name": run_name,
    }


def _checkpointer_conninfo() -> str | None:
    """Neon's DIRECT endpoint as a libpq URL for the agent's Postgres
    checkpointer, or None to fall back to SQLite (local dev without Postgres).
    Direct, not pooled: the checkpointer uses server-side prepared statements
    that PgBouncer transaction pooling rejects (see agent.postgres_checkpointer).
    """
    url = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL", "")
    if not url.startswith(("postgresql://", "postgres://")):
        return None
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


# --- AI gateway & infrastructure security (Phase 6.3) -------------------
# Protections that sit in front of BOTH agent entry points (POST /chat and the
# Telegram webhook): a per-message size cap, regex PII masking, a daily token
# budget, and a hard timeout on the agent call. Rate limiting is further down
# (it needs the FastAPI app object); the Telegram-side limiter is here because
# it can't use slowapi (see _telegram_rate_ok).

# Longest message accepted before the agent / masking / budget logic runs.
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "1000"))
# Daily input+output token ceiling per account for the worker model.
MAX_DAILY_QUOTA = int(os.environ.get("MAX_DAILY_QUOTA", "200000"))
# Hard cap on a single agent turn. On timeout the user gets AGENT_UNAVAILABLE_MESSAGE
# and the (partially checkpointed) run is abandoned — the next turn resumes from
# the last node boundary.
AGENT_TIMEOUT_SECONDS = float(os.environ.get("AGENT_TIMEOUT_SECONDS", "30"))
# Telegram webhook: messages per chat_id per rolling 60s.
TELEGRAM_RATE_LIMIT_PER_MIN = int(os.environ.get("TELEGRAM_RATE_LIMIT_PER_MIN", "5"))

AGENT_UNAVAILABLE_MESSAGE = (
    "The coaching assistant is temporarily unavailable, please try again in a moment."
)


class _AgentUnavailable(Exception):
    """The agent call exceeded AGENT_TIMEOUT_SECONDS. Caught at each entry point
    and turned into AGENT_UNAVAILABLE_MESSAGE (never surfaced to the client)."""


# --- PII masking -------------------------------------------------------
# Lightweight regex redaction, not Presidio. Applied to every inbound message
# before the agent sees it — so before any Anthropic call, before the message
# lands in the checkpoint store, and (transitively, via the habit names the
# agent creates from it) before the OpenAI embedding path in summarize_memory /
# vector_store. Masking failure is fail-CLOSED: the caller blocks rather than
# forward raw PII.

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Realistic phone shapes only, to keep dates / short counts / IDs out of scope:
#   +CC ... groups   |   (NNN) NNN-NNNN / NNN-NNN-NNNN   |   a bare 10-15 digit run
_PHONE_RE = re.compile(
    r"(?<![\w])(?:"
    r"\+\d{1,3}[\s.\-]?(?:\(?\d{1,4}\)?[\s.\-]?){1,4}\d{2,4}"
    r"|\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}"
    r"|\d{10,15}"
    r")(?![\w])"
)
# A candidate card is 13-19 digits, optionally in space/dash groups. The Luhn
# check below is what actually decides — so a plain 16-digit reference number
# (invalid Luhn) is left untouched.
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ \-]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask_cards(text: str) -> str:
    def _repl(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "<CARD_MASKED>"
        return match.group(0)

    return _CARD_CANDIDATE_RE.sub(_repl, text)


def mask_pii(text: str) -> str:
    """Redact emails, phone numbers, and Luhn-valid card numbers. Cards are
    masked first, before the phone pattern can bite into a card's digits."""
    text = _mask_cards(text)
    text = _EMAIL_RE.sub("<EMAIL_MASKED>", text)
    text = _PHONE_RE.sub("<PHONE_MASKED>", text)
    return text


# --- Telegram webhook rate limiting -----------------------------------
# slowapi's key_func only sees the Request, never the parsed body — so it can't
# key on telegram_chat_id. This is a small in-memory sliding window instead,
# checked synchronously (no await) inside the webhook handler, so no lock is
# needed on the single-threaded event loop. In-memory and per-process: fine for
# one Railway instance; a horizontally-scaled deploy would need a shared store
# (Redis) here and for the slowapi limiter below.
_telegram_hits: dict[int, deque] = defaultdict(deque)


def _telegram_rate_ok(chat_id: int) -> bool:
    """True if this chat is under TELEGRAM_RATE_LIMIT_PER_MIN over the trailing
    60s. Fail-open: any bookkeeping error lets the message through."""
    try:
        now = time.monotonic()
        hits = _telegram_hits[chat_id]
        while hits and now - hits[0] > 60.0:
            hits.popleft()
        if len(hits) >= TELEGRAM_RATE_LIMIT_PER_MIN:
            return False
        hits.append(now)
        return True
    except Exception:  # never block a message over a limiter bug
        logger.warning("Telegram rate-limit check errored — allowing the message.", exc_info=True)
        return True


# --- token budget + timed agent invocation ---------------------------


def _budget_exceeded(user_id: int) -> bool:
    """Whether this user has hit MAX_DAILY_QUOTA today. Fail-open: a ledger read
    error logs and allows the turn rather than hard-blocking coaching."""
    try:
        return daily_token_total(user_id, date.today()) >= MAX_DAILY_QUOTA
    except Exception:
        logger.warning("Token-budget check failed — allowing the request.", exc_info=True)
        return False


async def _invoke_agent(message_text: str, config: dict) -> tuple[dict, tuple[int, int]]:
    """One agent turn with a hard timeout (AGENT_TIMEOUT_SECONDS). Returns
    `(result, (input_tokens, output_tokens))`.

    The token totals cover EVERY model call the turn made — the worker
    (`claude-sonnet-5`) AND the Haiku guardrail classifiers — via a
    `UsageMetadataCallbackHandler` scoped to this one invocation. That's why
    the count comes from the callback, not from slicing `result["messages"]`
    (which only carries the worker's messages).

    Raises `_AgentUnavailable` on timeout; other errors (Anthropic API
    failures included) propagate to the generic exception handler."""
    usage_cb = UsageMetadataCallbackHandler()
    cfg = {**config, "callbacks": [*config.get("callbacks", []), usage_cb]}
    try:
        result = await asyncio.wait_for(
            app.state.agent.ainvoke(
                {"messages": [{"role": "user", "content": message_text}]},
                config=cfg,
            ),
            timeout=AGENT_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Agent invocation timed out after %.0fs.", AGENT_TIMEOUT_SECONDS)
        raise _AgentUnavailable from None

    # usage_metadata is {model_name: {input_tokens, output_tokens, ...}} — sum
    # across every model that ran this turn (worker + Haiku classifiers).
    per_model = usage_cb.usage_metadata.values()
    inp = sum(m.get("input_tokens", 0) or 0 for m in per_model)
    out = sum(m.get("output_tokens", 0) or 0 for m in per_model)
    return result, (inp, out)


async def _telegram_reply(message_text: str) -> str:
    """Drive the shared agent for one inbound Telegram message, acting as the
    single configured account (HABIT_TRACKER_USERNAME). This is the callback
    src/bot.py's message handler invokes — it mirrors POST /chat, minus the
    JWT: on Telegram, identity is the fixed account, resolved server-side
    here, never taken from the message.

    Gateway checks (Phase 6.3), in order, before the agent runs: size cap ->
    PII masking -> daily token budget. A per-chat rate limit is applied earlier,
    in the webhook handler. Anything the user shouldn't see (an oversized
    message, a masking failure, a timeout, the budget ceiling) comes back as a
    plain reply string, never an exception."""
    if len(message_text) > MAX_MESSAGE_CHARS:
        return (
            f"Please keep your messages under {MAX_MESSAGE_CHARS} characters — "
            "send it in a couple of shorter messages and I'll keep up."
        )
    try:
        message_text = mask_pii(message_text)
    except Exception:
        logger.warning("PII masking failed on a Telegram message — blocking (fail-closed).", exc_info=True)
        return "Sorry — I couldn't process that message. Please try rephrasing it."

    username = os.environ["HABIT_TRACKER_USERNAME"]
    session = get_session()
    try:
        user = session.query(User).filter(User.username == username).first()
        if user is None:
            raise RuntimeError(f"No account for HABIT_TRACKER_USERNAME={username!r}")
        user_id = user.id
    finally:
        session.close()

    if _budget_exceeded(user_id):
        return "You've hit today's usage limit for the coach — check back tomorrow."

    update_ctx = _telegram_update_ctx.get()
    config = _agent_config(
        thread_id=str(user_id),
        tags=["telegram", "webhook", "habit-tracking"],
        metadata={
            "user_id": user_id,
            "telegram_chat_id": update_ctx.get("telegram_chat_id"),
            "message_id": update_ctx.get("message_id"),
            "correlation_id": update_ctx.get("correlation_id"),
            "environment": _environment(),
            "source": "telegram",
        },
        run_name="telegram_chat",
    )
    try:
        result, (inp, out) = await _invoke_agent(message_text, config)
    except _AgentUnavailable:
        return AGENT_UNAVAILABLE_MESSAGE

    background_tasks = update_ctx.get("background_tasks")
    if background_tasks is not None:
        background_tasks.add_task(record_token_usage, user_id, date.today(), inp, out)
    else:  # no request context (shouldn't happen via the webhook) — record inline
        record_token_usage(user_id, date.today(), inp, out)

    return _as_text(result["messages"][-1].content)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at server startup/shutdown (not per-request). Prepares the
    database, wires the agent's per-conversation checkpointer (Postgres in
    deployment, SQLite locally), starts the daily Telegram reminder scheduler
    (Phase 2), builds one shared agent instance that every request reuses for
    the life of the process (see build_agent()'s docstring), and drives the
    Telegram bot in-process via webhook (src/bot.py — no polling)."""
    _configure_request_logging()
    init_db()
    async with AsyncExitStack() as stack:
        conninfo = _checkpointer_conninfo()
        checkpointer = (
            await stack.enter_async_context(postgres_checkpointer(conninfo))
            if conninfo
            else None
        )
        # Build the agent first: the scheduler's 20:00 job (Phase 3) invokes it
        # to generate the nudge text, so it must exist before the job is added.
        agent = await stack.enter_async_context(build_agent(checkpointer=checkpointer))
        app.state.agent = agent
        scheduler = start_reminder_scheduler(agent)

        # Telegram: build the Application, drive it in-process. Updates arrive
        # at POST /webhook/telegram, which calls telegram_app.process_update().
        telegram_app = build_application(on_message=_telegram_reply)
        app.state.telegram_app = telegram_app
        await telegram_app.initialize()
        await telegram_app.start()

        webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL")
        secret_token = os.environ.get("WEBHOOK_SECRET_TOKEN")
        if webhook_url:
            try:
                await telegram_app.bot.set_webhook(url=webhook_url, secret_token=secret_token)
                logger.info("Telegram webhook registered at %s", webhook_url)
            except Exception:
                # The real public URL often isn't known until after the first
                # deploy (Cloud Run). Don't let that block startup — the
                # webhook can be set once the URL exists.
                logger.warning(
                    "Could not register the Telegram webhook at %s — set it "
                    "manually once the public URL is known.", webhook_url, exc_info=True
                )
        else:
            logger.warning("TELEGRAM_WEBHOOK_URL not set — skipping webhook registration.")

        try:
            yield
        finally:
            await telegram_app.stop()
            await telegram_app.shutdown()
            scheduler.shutdown(wait=False)


# --- Rate limiting (Phase 6.2 / 6.3) ------------------------------------
# In-memory limiter (fine for the single Railway instance — no Redis; a
# horizontally-scaled deploy would need a shared storage_uri). `uvicorn.run`
# below sets proxy_headers so IP-keyed limits see the real caller behind
# Railway's proxy, not the proxy itself. swallow_errors=True makes the limiter
# fail-OPEN: a storage hiccup lets the request through rather than 500-ing it.
LOGIN_RATE_LIMIT = os.environ.get("LOGIN_RATE_LIMIT", "5/minute")  # by IP (brute-force)
SIGNUP_RATE_LIMIT = os.environ.get("SIGNUP_RATE_LIMIT", "3/minute")  # by IP
CHAT_RATE_LIMIT = os.environ.get("CHAT_RATE_LIMIT", "20/minute")  # by authed user_id (6.3)
FRICTION_RATE_LIMIT = os.environ.get("FRICTION_RATE_LIMIT", "10/minute")  # by IP

limiter = Limiter(key_func=get_remote_address, swallow_errors=True)


async def _rate_limit_user_id(request: Request, user_id: int = Depends(get_current_user_id)) -> int:
    """get_current_user_id, plus it stashes the verified id on request.state so
    the /chat rate limiter can key on the authenticated user rather than the
    shared proxy IP. FastAPI resolves dependencies before slowapi's limit check
    runs, so request.state.user_id is populated by the time _user_id_rate_key
    is called."""
    request.state.user_id = user_id
    return user_id


def _user_id_rate_key(request: Request) -> str:
    """Rate-limit key for /chat: the authenticated user id, or the client IP as
    a fallback if auth hasn't run (it always has, on /chat)."""
    user_id = getattr(request.state, "user_id", None)
    return f"user:{user_id}" if user_id is not None else get_remote_address(request)


app = FastAPI(title="Habit Tracker", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all (Phase 6.3): the client only ever sees a generic message — no
    stack trace, no DB / connection detail, no internal path. The real
    exception is logged server-side. HTTPException and RateLimitExceeded have
    their own, more specific handlers and are unaffected."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on our end. Please try again."},
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening headers on every response (Phase 6.2). HSTS is only
    honoured over HTTPS, which is what Railway serves."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
    )
    return response


class SignupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@app.post(
    "/auth/signup",
    response_model=TokenResponse,
    status_code=201,
    dependencies=[Depends(require_signup_allowed)],
)
@limiter.limit(SIGNUP_RATE_LIMIT)
async def signup(request: Request, payload: SignupRequest) -> TokenResponse:
    """Create a new account and return a JWT for it.

    Gated by `require_signup_allowed` (Phase 6.2): when SIGNUP_SECRET is set,
    the request must carry a matching X-Signup-Secret header. Password strength
    is checked before anything else touches the database — fail fast, and avoid
    a half-created-user edge case if it were checked later.

    `request: Request` is unused by the body but required by the rate limiter.
    """
    unmet = [label for label, met in password_requirement_status(payload.password) if not met]
    if unmet:
        raise HTTPException(422, "Password must include: " + "; ".join(unmet) + ".")

    session = get_session()
    try:
        if session.query(User).filter(User.username == payload.username).first() is not None:
            raise HTTPException(409, "That username is already taken.")
        user = User(username=payload.username, hashed_password=hash_password(payload.password))
        session.add(user)
        session.commit()
        session.refresh(user)  # populates user.id, needed below, before commit assigns it
        # One-time (per-signup, idempotent) migration: attach any pre-auth
        # habits with no owner to whichever account claims them first.
        backfill_orphaned_habits(session, user.id)
        return TokenResponse(access_token=create_access_token(user.id, user.username))
    finally:
        session.close()


@app.post("/auth/login", response_model=TokenResponse)
@limiter.limit(LOGIN_RATE_LIMIT)
async def login(request: Request, payload: LoginRequest) -> TokenResponse:
    """Verify credentials and return a fresh JWT. No password-strength check
    here — those rules only apply when a password is being *set* (signup);
    an existing account must still be able to log in even if the rules
    change later, since its stored password won't retroactively update.

    Rate-limited (Phase 6.2) to blunt password brute-forcing; `request: Request`
    is required by the limiter, not used by the handler."""
    session = get_session()
    try:
        user = session.query(User).filter(User.username == payload.username).first()
        if user is None or not verify_password(payload.password, user.hashed_password):
            # Deliberately the same error for "no such user" and "wrong password" —
            # a distinct message would let an attacker enumerate valid usernames.
            raise HTTPException(401, "Incorrect username or password.")
        return TokenResponse(access_token=create_access_token(user.id, user.username))
    finally:
        session.close()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


class HabitStatus(BaseModel):
    id: int
    name: str
    frequency: str
    status: str | None = None


class TodayDashboard(BaseModel):
    date: str
    done: list[HabitStatus]
    pending: list[HabitStatus]


@app.get("/habits/today", response_model=TodayDashboard)
async def habits_today(user_id: int = Depends(get_current_user_id)) -> TodayDashboard:
    """The Today's Dashboard data the frontend sidebar shows. A plain
    database read, not an agent call — a deterministic "what's the status
    right now" question doesn't need an LLM in the loop, so this skips
    src/agent.py entirely and goes straight to the DB."""
    today = date.today()
    session = get_session()
    try:
        habits = session.query(Habit).filter(Habit.user_id == user_id).all()
        done, pending = [], []
        for habit in habits:
            if not is_due_today(habit, today):
                continue  # excluded today (e.g. "daily except Friday") — nothing to show

            today_log = next((entry for entry in habit.logs if entry.date == today), None)
            item = HabitStatus(
                id=habit.id,
                name=habit.name,
                frequency=habit.frequency,
                status=today_log.status if today_log else None,
            )
            (done if is_satisfied(habit, today) else pending).append(item)
        return TodayDashboard(date=today.isoformat(), done=done, pending=pending)
    finally:
        session.close()


class DayCompletion(BaseModel):
    date: str
    completed: int
    due: int


class HabitDayLog(BaseModel):
    date: str
    status: str


class HabitHistory(BaseModel):
    name: str
    frequency: str
    logs: list[HabitDayLog]


class HabitStats(BaseModel):
    trend: list[DayCompletion]
    habits: list[HabitHistory]
    current_streak: int
    this_week_rate: float
    last_week_rate: float


@app.get("/habits/stats", response_model=HabitStats)
async def habits_stats(days: int = 30, user_id: int = Depends(get_current_user_id)) -> HabitStats:
    """Historical data behind the frontend's Progress charts: a daily
    completion trend, each habit's raw log history in the window, the
    current streak, and a week-over-week comparison.

    The trend line only counts daily-style habits (satisfaction_window_days
    == 1) in its due/completed tally, deliberately excluding "weekly"
    habits — a weekly habit is only truly due once every 7 days, so folding
    it into a day-by-day rate would make it look "missed" on the 6 days it
    was never actually due, distorting the trend. Weekly habits still show
    up in the per-habit heatmap, just via their real logged days rather
    than a forced daily due/not-due grid.
    """
    days = max(1, min(days, 365))
    today = date.today()
    window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]  # oldest -> newest

    session = get_session()
    try:
        habits = session.query(Habit).filter(Habit.user_id == user_id).all()

        trend = []
        for day in window:
            due_habits = [
                habit for habit in habits
                if satisfaction_window_days(habit.frequency) == 1 and is_due_today(habit, day)
            ]
            completed = sum(
                1 for habit in due_habits
                if any(log.date == day and log.status == "done" for log in habit.logs)
            )
            trend.append(DayCompletion(date=day.isoformat(), completed=completed, due=len(due_habits)))

        habit_histories = [
            HabitHistory(
                name=habit.name,
                frequency=habit.frequency,
                logs=[
                    HabitDayLog(date=log.date.isoformat(), status=log.status)
                    for log in habit.logs
                    if log.date >= window[0]
                ],
            )
            for habit in habits
        ]

        # Consecutive days (ending today, walking backward) with every due
        # daily-style habit completed. A day with nothing due doesn't break
        # the streak — there was nothing to miss.
        current_streak = 0
        for day_stat in reversed(trend):
            if day_stat.due == 0:
                continue
            if day_stat.completed < day_stat.due:
                break
            current_streak += 1

        def _week_rate(days_slice: list[DayCompletion]) -> float:
            total_due = sum(entry.due for entry in days_slice)
            total_done = sum(entry.completed for entry in days_slice)
            return (total_done / total_due) if total_due else 0.0

        return HabitStats(
            trend=trend,
            habits=habit_histories,
            current_streak=current_streak,
            this_week_rate=_week_rate(trend[-7:]),
            last_week_rate=_week_rate(trend[-14:-7]),
        )
    finally:
        session.close()


@app.post("/chat", response_model=ChatResponse)
@limiter.limit(CHAT_RATE_LIMIT, key_func=_user_id_rate_key)
async def chat(
    request: Request,
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(_rate_limit_user_id),
) -> ChatResponse:
    """Send one message to the Habit Coach and get its reply.

    user_id comes only from the verified JWT (via Depends), never from the
    request body — this is what makes it impossible for a client to claim
    to be someone else's thread. It's passed as the LangGraph thread_id,
    which is also what src/tools.py's ToolRuntime reads to scope every tool
    call to the right user's data.

    Gateway checks (Phase 6.3), before the agent runs: rate limit (keyed on
    this user_id), size cap, PII masking, daily token budget. `request:
    Request` is required by the limiter.
    """
    correlation_id = _new_correlation_id("chat")
    if len(payload.message) > MAX_MESSAGE_CHARS:
        raise HTTPException(
            413,
            f"Please keep your message under {MAX_MESSAGE_CHARS} characters — "
            "send it in a couple of shorter messages.",
        )
    try:
        message = mask_pii(payload.message)
    except Exception:
        logger.warning("PII masking failed — blocking the request (fail-closed).", exc_info=True)
        raise HTTPException(500, "Something went wrong on our end. Please try again.") from None

    if _budget_exceeded(user_id):
        raise HTTPException(429, "You've reached today's usage limit. Please try again tomorrow.")

    logger.info("Chat request received (user_id=%s).", user_id)
    try:
        result, (inp, out) = await _invoke_agent(
            message,
            _agent_config(
                thread_id=str(user_id),
                tags=["web", "chat", "habit-tracking"],
                metadata={
                    "user_id": user_id,
                    "correlation_id": correlation_id,
                    "environment": _environment(),
                    "source": "web",
                },
                run_name="web_chat",
            ),
        )
    except _AgentUnavailable:
        return ChatResponse(reply=AGENT_UNAVAILABLE_MESSAGE)

    background_tasks.add_task(record_token_usage, user_id, date.today(), inp, out)

    # .content is a plain string for a simple reply, but a list of blocks
    # (thinking + text) once extended thinking kicks in on a longer reply.
    reply = _as_text(result["messages"][-1].content)
    logger.info("Chat request completed (user_id=%s, reply_chars=%d).", user_id, len(reply))
    return ChatResponse(reply=reply)


def _as_text(content: object) -> str:
    """Normalize a LangChain message's .content into plain text. It's a
    plain string for a short reply, but a list of content blocks (e.g. a
    thinking block + a text block) once Claude's extended thinking kicks
    in — pull out just the text blocks and ignore the rest (like thinking,
    which isn't meant for the end user to see)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


@app.post("/evaluate_friction", response_model=ChatResponse)
@limiter.limit(FRICTION_RATE_LIMIT)
async def evaluate_friction(
    request: Request, user_id: int = Depends(get_current_user_id)
) -> ChatResponse:
    """Manually run the same evening 'friction check' the 20:00 scheduler job
    runs automatically (Phase 3): look at what's still pending for THIS user
    and, if anything is, have the coach produce a pattern-aware nudge — same
    agent, same per-user thread, same micro-commitment logic (all via
    src/scheduler.run_friction_nudge, so the pending-habit check isn't
    written twice).

    Like every other endpoint, identity is the JWT's user_id only, never a
    request-body field — reintroducing a client-supplied user_id here would
    be the exact bug real auth already fixed in /chat. Rate-limited (Phase
    6.2); `request: Request` is required by the limiter.
    """
    correlation_id = _new_correlation_id("friction")
    reply = await run_friction_nudge(
        app.state.agent, user_id, origin="manual-trigger", correlation_id=correlation_id
    )
    if reply is None:
        return ChatResponse(reply="Nothing pending right now — you're all caught up for today.")
    return ChatResponse(reply=reply)


class HistoryMessage(BaseModel):
    role: str
    content: str


class ChatHistory(BaseModel):
    messages: list[HistoryMessage]


@app.get("/chat/history", response_model=ChatHistory)
async def chat_history(user_id: int = Depends(get_current_user_id)) -> ChatHistory:
    """Replay this user's past conversation so the frontend can restore the
    visible chat after a page refresh (the browser holds no chat history of
    its own — src/app.py fetches this once on load). No {user_id} path
    param on purpose: that used to be editable by anyone, which was exactly
    the vulnerability real auth fixes — identity comes only from the JWT."""
    agent = app.state.agent
    snapshot = await agent.aget_state(config={"configurable": {"thread_id": str(user_id)}})
    raw_messages = snapshot.values.get("messages", []) if snapshot.values else []

    history = []
    for message in raw_messages:
        text = _as_text(message.content)
        if not text:
            continue  # skips tool-call-only messages (e.g. "call log_habit"), which have no user-facing text
        if isinstance(message, HumanMessage):
            history.append(HistoryMessage(role="user", content=text))
        elif isinstance(message, AIMessage):
            history.append(HistoryMessage(role="assistant", content=text))
        # ToolMessage (raw tool results) are deliberately not included —
        # they're internal plumbing, not part of the visible conversation.

    return ChatHistory(messages=history)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe (Cloud Run / load balancer health checks)."""
    return {"status": "ok"}


async def _send_telegram_notice(chat_id: int, text: str) -> None:
    """Best-effort out-of-band Telegram message (e.g. a rate-limit notice),
    sent from a background task so the webhook can return 200 immediately."""
    try:
        await app.state.telegram_app.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.warning("Could not send a Telegram notice to chat_id=%s.", chat_id, exc_info=True)


@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    """Telegram delivers updates here (URL registered via set_webhook in the
    lifespan). Telegram echoes the secret token we set on every request, in
    the X-Telegram-Bot-Api-Secret-Token header — that's what proves the call
    is really from Telegram. Any mismatch or missing header → 401.

    Rate-limited per chat_id (Phase 6.3): over the limit, this returns 200
    straight away — so Telegram doesn't queue retries — plus one out-of-band
    "slow down" reply."""
    expected = os.environ.get("WEBHOOK_SECRET_TOKEN")
    if not x_telegram_bot_api_secret_token or x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    telegram_app = app.state.telegram_app
    update = Update.de_json(await request.json(), telegram_app.bot)
    correlation_id = _new_correlation_id("tg")

    incoming = (update.message or update.edited_message) if update else None
    chat_id = update.effective_chat.id if update and update.effective_chat else None

    if chat_id is not None and not _telegram_rate_ok(chat_id):
        logger.warning("Telegram rate limit hit for chat_id=%s.", chat_id)
        background_tasks.add_task(
            _send_telegram_notice,
            chat_id,
            "You're sending messages faster than I can keep up — give me a minute, then try again.",
        )
        return {"ok": True}

    _telegram_update_ctx.set(
        {
            "telegram_chat_id": chat_id,
            "message_id": incoming.message_id if incoming else None,
            "correlation_id": correlation_id,
            "background_tasks": background_tasks,
        }
    )
    logger.info(
        "Telegram update received (update_id=%s, chat_id=%s).",
        getattr(update, "update_id", None),
        chat_id,
    )
    # process_update() runs the handler chain inline in this task, so the
    # context vars set above are visible to _telegram_reply.
    await telegram_app.process_update(update)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    # Railway/Cloud Run inject PORT; bind all interfaces so the container is
    # reachable. proxy_headers + forwarded_allow_ips let the rate limiter (Phase
    # 6.2) key on the real client IP from X-Forwarded-For rather than Railway's
    # proxy, which would otherwise bucket every caller together.
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
