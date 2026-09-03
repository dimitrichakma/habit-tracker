"""Phase 6.3 — AI gateway & infrastructure security.

Exercises the gateway protections that sit in front of ``src/main.py``'s two
agent entry points (``POST /chat`` and the Telegram webhook) WITHOUT a real
agent, database, Telegram client, or scheduler:

- rate limiting — slowapi, user-keyed on ``/chat`` and IP-keyed on
  ``/auth/login``; a hand-rolled chat-id-keyed sliding window on the webhook;
- the per-message size cap;
- PII masking (email / phone / Luhn-checked card number);
- the daily token budget block;
- the generic catch-all error handler (no leaked internals);
- the agent-call timeout → graceful "temporarily unavailable" message.

Isolation follows ``evaluation/test_rag_agent.py``: a throwaway SQLite app DB,
no real network. The FastAPI lifespan is deliberately NOT run — ``TestClient``
is used without its context manager and ``app.state`` is populated with fakes.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

# Rate-limit thresholds are frozen into the ``@limiter.limit(...)`` decorators
# at import time, so pin small, predictable limits BEFORE importing ``src.main``.
# This module is the only importer of ``src.main`` in the suite (and sorts
# first alphabetically), so these assignments always win.
os.environ["CHAT_RATE_LIMIT"] = "3/minute"
os.environ["LOGIN_RATE_LIMIT"] = "3/minute"
os.environ["SIGNUP_RATE_LIMIT"] = "3/minute"
os.environ["TELEGRAM_RATE_LIMIT_PER_MIN"] = "3"
os.environ["MAX_MESSAGE_CHARS"] = "200"
os.environ["MAX_DAILY_QUOTA"] = "1000"
os.environ["AGENT_TIMEOUT_SECONDS"] = "0.3"

import pytest  # noqa: E402
import sqlalchemy  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.database as database  # noqa: E402
import src.main as main  # noqa: E402
from src.auth import create_access_token  # noqa: E402

TG_USERNAME = "tg_account"
TG_SECRET = "test-webhook-secret"


# --- fixtures ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_langsmith(monkeypatch):
    """Keep the suite out of the real "habit-tracker" LangSmith project."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    from langsmith import utils as ls_utils

    ls_utils.get_env_var.cache_clear()
    yield
    ls_utils.get_env_var.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the app DB at a throwaway SQLite file (never the real Neon)."""
    engine = sqlalchemy.create_engine(
        f"sqlite:///{tmp_path / 'gateway.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    database.init_db()
    yield


class _FakeAgent:
    """Stands in for the compiled LangGraph agent. Records every ``ainvoke`` and
    can be told to stall (``delay``) or blow up (``exc``)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.reply = "Here's your habit update."
        self.delay = 0.0
        self.exc: Exception | None = None
        # per-model token usage this fake "spends" — worker + a stand-in for the
        # Haiku guardrail classifier, to prove _invoke_agent sums across models.
        self.model_usage = {
            "claude-sonnet-5": {"input_tokens": 10, "output_tokens": 5},
            "claude-haiku-4-5-20251001": {"input_tokens": 8, "output_tokens": 2},
        }

    def _feed_usage_callbacks(self, config):
        """Drive any UsageMetadataCallbackHandler in the config the way real
        model calls would (via on_llm_end), once per model."""
        from langchain_core.outputs import ChatGeneration, LLMResult

        for cb in (config or {}).get("callbacks", []) or []:
            if not hasattr(cb, "on_llm_end"):
                continue
            for model_name, usage in self.model_usage.items():
                msg = AIMessage(
                    content="",
                    usage_metadata={**usage, "total_tokens": usage["input_tokens"] + usage["output_tokens"]},
                    response_metadata={"model_name": model_name},
                )
                cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]))

    async def ainvoke(self, payload, config=None):
        self.calls.append({"payload": payload, "config": config})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        self._feed_usage_callbacks(config)
        user_text = payload["messages"][-1]["content"]
        return {
            "messages": [
                HumanMessage(content=user_text),
                AIMessage(content=self.reply),
            ]
        }


class _FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text})


class _FakeTelegramApp:
    """Minimal ``process_update`` — calls main's on_message callback the same
    way ``bot._handle_message`` does, then sends the reply."""

    def __init__(self, on_message):
        self.bot = _FakeBot()
        self._on_message = on_message

    async def process_update(self, update):
        reply = await self._on_message(update.message.text)
        await self.bot.send_message(chat_id=update.effective_chat.id, text=reply)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_update(data, bot=None):
    msg = data["message"]
    node = _Obj(text=msg["text"], message_id=msg.get("message_id", 1), chat=_Obj(id=msg["chat"]["id"]))
    return _Obj(
        message=node,
        edited_message=None,
        effective_chat=_Obj(id=msg["chat"]["id"]),
        update_id=msg.get("message_id", 1),
    )


class _FakeUpdateCls:
    de_json = staticmethod(_fake_update)


@pytest.fixture
def client(_isolated_db, monkeypatch):
    monkeypatch.setenv("HABIT_TRACKER_USERNAME", TG_USERNAME)
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", TG_SECRET)
    monkeypatch.setattr(main, "Update", _FakeUpdateCls)

    # The Telegram path resolves HABIT_TRACKER_USERNAME -> user_id against the DB.
    session = database.get_session()
    try:
        session.add(database.User(username=TG_USERNAME, hashed_password="x"))
        session.commit()
        tg_user_id = session.query(database.User).filter_by(username=TG_USERNAME).first().id
    finally:
        session.close()

    agent = _FakeAgent()
    main.app.state.agent = agent
    main.app.state.telegram_app = _FakeTelegramApp(main._telegram_reply)
    main.app.state.limiter = main.limiter
    main.limiter.reset()
    main._telegram_hits.clear()

    test_client = TestClient(main.app, raise_server_exceptions=False)
    test_client.agent = agent
    test_client.tg = main.app.state.telegram_app
    test_client.tg_user_id = tg_user_id
    return test_client


def _auth_header(user_id=1):
    return {"Authorization": f"Bearer {create_access_token(user_id, 'tester')}"}


def _tg_body(text, chat_id=555, message_id=1):
    return {"message": {"text": text, "message_id": message_id, "chat": {"id": chat_id}}}


_TG_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": TG_SECRET}


# --- rate limiting ---------------------------------------------------


def test_chat_rate_limited_per_user(client):
    codes = [
        client.post("/chat", json={"message": "hi"}, headers=_auth_header(1)).status_code
        for _ in range(6)
    ]
    assert codes.count(200) == 3  # CHAT_RATE_LIMIT = 3/minute
    assert 429 in codes


def test_chat_rate_limit_is_per_user_not_shared(client):
    for _ in range(5):
        client.post("/chat", json={"message": "hi"}, headers=_auth_header(1))
    # user 1 is now throttled; user 2 has an untouched bucket
    assert client.post("/chat", json={"message": "hi"}, headers=_auth_header(2)).status_code == 200


def test_login_rate_limited_by_ip(client):
    codes = [
        client.post("/auth/login", json={"username": "nobody", "password": "nope"}).status_code
        for _ in range(6)
    ]
    assert codes.count(401) == 3  # LOGIN_RATE_LIMIT = 3/minute; bad creds -> 401
    assert 429 in codes


def test_telegram_webhook_rate_limited_by_chat_id(client):
    codes = [
        client.post("/webhook/telegram", json=_tg_body("hello", chat_id=777), headers=_TG_HEADERS).status_code
        for _ in range(6)
    ]
    assert all(code == 200 for code in codes)  # never 4xx — Telegram must not retry
    assert len(client.agent.calls) == 3  # only 3 reached the agent
    assert any("keep up" in msg["text"] for msg in client.tg.bot.sent)  # slow-down notice sent


def test_telegram_rate_limit_is_per_chat(client):
    for _ in range(4):
        client.post("/webhook/telegram", json=_tg_body("hi", chat_id=111), headers=_TG_HEADERS)
    client.agent.calls.clear()
    client.post("/webhook/telegram", json=_tg_body("hi", chat_id=222), headers=_TG_HEADERS)
    assert len(client.agent.calls) == 1  # a different chat is unaffected


# --- per-request size cap -------------------------------------------


def test_chat_rejects_oversized_message_before_agent(client):
    resp = client.post("/chat", json={"message": "a" * 250}, headers=_auth_header(1))
    assert resp.status_code == 413
    assert "brief" in resp.text or "shorter" in resp.text
    assert client.agent.calls == []


def test_telegram_rejects_oversized_message_before_agent(client):
    resp = client.post("/webhook/telegram", json=_tg_body("b" * 250), headers=_TG_HEADERS)
    assert resp.status_code == 200
    assert client.agent.calls == []
    assert any("shorter messages" in msg["text"] for msg in client.tg.bot.sent)


# --- PII masking ----------------------------------------------------


@pytest.mark.parametrize(
    "raw, marker, leaked",
    [
        ("email me at bob.smith@example.co.uk please", "<EMAIL_MASKED>", "bob.smith@example.co.uk"),
        ("call me on +1 415 555 2671 tomorrow", "<PHONE_MASKED>", "555 2671"),
        ("here's my visa 4242 4242 4242 4242 ok", "<CARD_MASKED>", "4242 4242 4242 4242"),
    ],
)
def test_mask_pii_redacts(raw, marker, leaked):
    out = main.mask_pii(raw)
    assert marker in out
    assert leaked not in out


def test_mask_pii_keeps_invalid_luhn_16_digits():
    # 1234567890123456 fails the Luhn checksum -> not a card number
    out = main.mask_pii("order reference 1234567890123456 confirmed")
    assert "<CARD_MASKED>" not in out
    assert "1234567890123456" in out


def test_chat_masks_pii_before_the_agent_sees_it(client):
    client.post(
        "/chat",
        json={"message": "log my run and reach me at me@test.com"},
        headers=_auth_header(1),
    )
    seen = client.agent.calls[0]["payload"]["messages"][-1]["content"]
    assert "me@test.com" not in seen
    assert "<EMAIL_MASKED>" in seen


# --- daily token budget -------------------------------------------


def test_chat_blocks_once_budget_exceeded(client):
    database.record_token_usage(1, date.today(), 900, 200)  # 1100 >= MAX_DAILY_QUOTA (1000)
    resp = client.post("/chat", json={"message": "hi"}, headers=_auth_header(1))
    assert resp.status_code == 429
    assert client.agent.calls == []


def test_chat_records_token_usage_after_reply(client):
    resp = client.post("/chat", json={"message": "hi"}, headers=_auth_header(1))
    assert resp.status_code == 200
    # _FakeAgent spends 10+5 on the worker and 8+2 on the Haiku classifier;
    # the budget meters both -> 25.
    assert database.daily_token_total(1, date.today()) == 25


def test_telegram_blocks_once_budget_exceeded(client):
    database.record_token_usage(client.tg_user_id, date.today(), 1200, 0)
    resp = client.post("/webhook/telegram", json=_tg_body("hi", chat_id=333), headers=_TG_HEADERS)
    assert resp.status_code == 200
    assert client.agent.calls == []
    assert any("usage limit" in msg["text"] for msg in client.tg.bot.sent)


# --- generic error handler --------------------------------------


def test_internal_error_is_generic_not_leaked(client):
    client.agent.exc = ValueError("boom: password=hunter2 host=10.0.0.5 /srv/app/main.py")
    resp = client.post("/chat", json={"message": "hi"}, headers=_auth_header(1))
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert "10.0.0.5" not in resp.text
    assert "main.py" not in resp.text
    assert resp.json()["detail"] == "Something went wrong on our end. Please try again."


async def test_bot_handler_does_not_leak_exception_text():
    """src/bot.py's _handle_message backstop: an unexpected error from the
    on_message callback gets a generic reply, never the exception string (which
    can carry a DB error or the HABIT_TRACKER_USERNAME RuntimeError)."""
    import src.bot as bot

    sent: list[str] = []

    async def _boom(_text: str) -> str:
        raise RuntimeError("No account for HABIT_TRACKER_USERNAME='secret_account'")

    class _Msg:
        text = "hey coach"

        async def reply_text(self, text: str) -> None:
            sent.append(text)

    class _Update:
        message = _Msg()
        effective_chat = None  # skips the send_chat_action branch

    class _App:
        bot_data = {"on_message": _boom}

    class _Ctx:
        application = _App()

    await bot._handle_message(_Update(), _Ctx())

    assert sent, "the handler sent no reply"
    assert "secret_account" not in sent[0]
    assert "RuntimeError" not in sent[0]
    assert "went wrong on my end" in sent[0]


# --- agent timeout ---------------------------------------------


def test_chat_timeout_returns_graceful_message(client):
    client.agent.delay = 1.0  # AGENT_TIMEOUT_SECONDS = 0.3
    resp = client.post("/chat", json={"message": "hi"}, headers=_auth_header(1))
    assert resp.status_code == 200
    assert resp.json()["reply"] == main.AGENT_UNAVAILABLE_MESSAGE


def test_telegram_timeout_returns_graceful_message(client):
    client.agent.delay = 1.0
    resp = client.post("/webhook/telegram", json=_tg_body("hi", chat_id=444), headers=_TG_HEADERS)
    assert resp.status_code == 200
    assert any(msg["text"] == main.AGENT_UNAVAILABLE_MESSAGE for msg in client.tg.bot.sent)
