"""Offline tests for the Telegram ``/today`` tap-to-log buttons.

Drives ``src/bot.py``'s handlers with fake ``Update`` / ``CallbackQuery``
objects against ``main.py``'s real in-process callbacks and a throwaway
SQLite — no Telegram, no agent, no network. Imports ``src.main``, so (like
``test_habit_log_endpoints.py``) it must sort AFTER ``test_gateway_security.py``,
which pins rate-limit env before the first import.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from telegram.error import BadRequest

import src.bot as bot
import src.database as database
import src.main as main
import src.tools as tools

TODAY = date.today()
USERNAME = "tg_tapper"
LINKED_TG_ID = 4242
OTHER_TG_ID = 9999


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    engine = sqlalchemy.create_engine(
        f"sqlite:///{tmp_path / 'tg_buttons.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    database.init_db()
    monkeypatch.setenv("HABIT_TRACKER_USERNAME", USERNAME)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(LINKED_TG_ID))
    yield


def _make_user(username: str) -> int:
    session = database.get_session()
    try:
        user = database.User(username=username, hashed_password="x")
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


def _make_habit(user_id: int, name: str = "Read", frequency: str = "daily") -> int:
    session = database.get_session()
    try:
        habit = database.Habit(name=name, frequency=frequency, user_id=user_id)
        session.add(habit)
        session.commit()
        return habit.id
    finally:
        session.close()


def _log(habit_id: int, day: date, status: str = "done") -> None:
    session = database.get_session()
    try:
        database.upsert_habit_log(session, habit_id, day, status)
        session.commit()
    finally:
        session.close()


def _logs(habit_id: int) -> list[database.HabitLog]:
    session = database.get_session()
    try:
        return session.query(database.HabitLog).filter_by(habit_id=habit_id).all()
    finally:
        session.close()


@pytest.fixture
def owner():
    user_id = _make_user(USERNAME)
    return user_id, _make_habit(user_id)


def _context():
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"on_today": main._telegram_today_view, "on_toggle": main._telegram_toggle}
        )
    )


def _tap_update(habit_id, *, tg_id=LINKED_TG_ID, data=None):
    query = SimpleNamespace(
        data=data if data is not None else f"tog:{habit_id}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=tg_id),
        callback_query=query,
        message=None,
    )
    return update, query


def _today_update(*, tg_id=LINKED_TG_ID):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=tg_id), message=message)
    return update, message


def _buttons(markup) -> list[tuple[str, str]]:
    return [(row[0].text, row[0].callback_data) for row in markup.inline_keyboard]


# --- rendering --------------------------------------------------------


async def test_today_keyboard_labels_and_callback_data(owner):
    user_id, read_id = owner
    run_id = _make_habit(user_id, "Run")
    _log(run_id, TODAY)

    update, message = _today_update()
    await bot._handle_today(update, _context())

    message.reply_text.assert_awaited_once()
    markup = message.reply_text.await_args.kwargs["reply_markup"]
    assert _buttons(markup) == [("⬜ Read", f"tog:{read_id}"), ("✅ Run", f"tog:{run_id}")]


async def test_today_skips_habits_excluded_today(owner):
    user_id, _ = owner
    skipped = _make_habit(user_id, "Gym", f"daily except {TODAY.strftime('%A')}")

    update, message = _today_update()
    await bot._handle_today(update, _context())

    ids = [data for _, data in _buttons(message.reply_text.await_args.kwargs["reply_markup"])]
    assert f"tog:{skipped}" not in ids


async def test_today_with_no_due_habits_sends_text_only():
    _make_user(USERNAME)

    update, message = _today_update()
    await bot._handle_today(update, _context())

    message.reply_text.assert_awaited_once()
    assert message.reply_text.await_args.kwargs["reply_markup"] is None
    assert "no habits due" in message.reply_text.await_args.args[0]


async def test_weekly_habit_done_earlier_renders_earlier_state(owner):
    user_id, _ = owner
    weekly = _make_habit(user_id, "Long run", "weekly")
    earlier = TODAY - timedelta(days=2)
    _log(weekly, earlier)

    update, message = _today_update()
    await bot._handle_today(update, _context())

    labels = dict((data, text) for text, data in _buttons(message.reply_text.await_args.kwargs["reply_markup"]))
    assert labels[f"tog:{weekly}"] == f"☑️ Long run (done {earlier.strftime('%a')})"


# --- tap behaviour ----------------------------------------------------


async def test_tap_logs_done_and_edits_same_message(owner):
    _, habit_id = owner
    update, query = _tap_update(habit_id)

    await bot._handle_toggle(update, _context())

    assert [log.status for log in _logs(habit_id)] == ["done"]
    query.answer.assert_awaited()
    query.edit_message_text.assert_awaited_once()
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert _buttons(markup) == [("✅ Read", f"tog:{habit_id}")]


async def test_tap_on_done_habit_removes_log_not_missed(owner):
    _, habit_id = owner
    _log(habit_id, TODAY)
    update, query = _tap_update(habit_id)

    await bot._handle_toggle(update, _context())

    assert _logs(habit_id) == []  # removed, never a "missed" row
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert _buttons(markup) == [("⬜ Read", f"tog:{habit_id}")]


def test_repeated_done_never_duplicates_a_row(owner):
    user_id, habit_id = owner
    session = database.get_session()
    try:
        for _ in range(3):
            database.set_habit_done(session, user_id, habit_id, TODAY, True)
            session.commit()
    finally:
        session.close()
    assert len(_logs(habit_id)) == 1


async def test_tap_and_chat_tool_share_one_row(owner):
    user_id, habit_id = owner
    update, _ = _tap_update(habit_id)
    await bot._handle_toggle(update, _context())

    runtime = SimpleNamespace(config={"configurable": {"thread_id": str(user_id)}})
    tools.log_habit.func("Read", "done", runtime=runtime)

    assert [log.status for log in _logs(habit_id)] == ["done"]


async def test_weekly_earlier_tap_cycle_keeps_earlier_log(owner):
    user_id, _ = owner
    weekly = _make_habit(user_id, "Long run", "weekly")
    earlier = TODAY - timedelta(days=2)
    _log(weekly, earlier)

    states = []
    for _ in range(2):
        update, query = _tap_update(weekly)
        await bot._handle_toggle(update, _context())
        states.append(_buttons(query.edit_message_text.await_args.kwargs["reply_markup"]))

    done_label = [t for rows in states[:1] for t, d in rows if d == f"tog:{weekly}"][0]
    back_label = [t for rows in states[1:] for t, d in rows if d == f"tog:{weekly}"][0]
    assert done_label == "✅ Long run"
    assert back_label == f"☑️ Long run (done {earlier.strftime('%a')})"
    assert [log.date for log in _logs(weekly)] == [earlier]


async def test_not_modified_error_is_swallowed(owner):
    _, habit_id = owner
    update, query = _tap_update(habit_id)
    query.edit_message_text.side_effect = BadRequest("Message is not modified")

    await bot._handle_toggle(update, _context())  # must not raise


# --- authorization ----------------------------------------------------


async def test_tap_from_other_telegram_user_is_refused(owner):
    _, habit_id = owner
    update, query = _tap_update(habit_id, tg_id=OTHER_TG_ID)

    await bot._handle_toggle(update, _context())

    assert _logs(habit_id) == []
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_args.kwargs.get("show_alert") is True


async def test_today_from_other_telegram_user_reveals_nothing(owner):
    update, message = _today_update(tg_id=OTHER_TG_ID)

    await bot._handle_today(update, _context())

    message.reply_text.assert_not_awaited()


async def test_unset_chat_id_fails_closed(owner, monkeypatch):
    _, habit_id = owner
    monkeypatch.delenv("TELEGRAM_CHAT_ID")

    update, message = _today_update()
    await bot._handle_today(update, _context())
    message.reply_text.assert_not_awaited()

    update, query = _tap_update(habit_id)
    await bot._handle_toggle(update, _context())
    assert _logs(habit_id) == []
    query.edit_message_text.assert_not_awaited()


async def test_forged_callback_for_another_users_habit_is_refused(owner):
    other_id = _make_user("someone_else")
    foreign_habit = _make_habit(other_id, "Secret")
    update, query = _tap_update(foreign_habit)

    await bot._handle_toggle(update, _context())

    assert _logs(foreign_habit) == []
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.parametrize("data", ["tog:abc", "tog:1;drop", "tog:", "x", "tog:-1", "tog:1:2"])
def test_malformed_callback_data_does_not_match_handler_pattern(data):
    assert re.match(bot._TOGGLE_PATTERN, data) is None


# --- shared helper ----------------------------------------------------


def test_set_habit_done_done_undone_and_ownership(owner):
    user_id, habit_id = owner
    other_id = _make_user("someone_else")
    session = database.get_session()
    try:
        assert database.set_habit_done(session, user_id, habit_id, TODAY, True).id == habit_id
        session.commit()
        assert len(_logs(habit_id)) == 1

        assert database.set_habit_done(session, user_id, habit_id, TODAY, False).id == habit_id
        session.commit()
        assert _logs(habit_id) == []

        assert database.set_habit_done(session, other_id, habit_id, TODAY, True) is None  # not theirs
        assert database.set_habit_done(session, user_id, 99999, TODAY, True) is None  # missing
        session.commit()
        assert _logs(habit_id) == []
    finally:
        session.close()


# --- wiring -----------------------------------------------------------


def test_build_application_wiring(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-token")

    chat_only = bot.build_application(on_message=AsyncMock())
    names = {type(h).__name__ for h in chat_only.handlers[0]}
    assert "CallbackQueryHandler" not in names  # old signature unchanged

    full = bot.build_application(
        on_message=AsyncMock(), on_today=main._telegram_today_view, on_toggle=main._telegram_toggle
    )
    handlers = full.handlers[0]
    assert any(type(h).__name__ == "CallbackQueryHandler" for h in handlers)
    assert any(type(h).__name__ == "CommandHandler" and "today" in h.commands for h in handlers)


class _FakeTelegramApp:
    def __init__(self):
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.updates = []

    async def process_update(self, update):
        self.updates.append(update)


def _callback_update(data, bot_=None):
    chat = SimpleNamespace(id=LINKED_TG_ID)
    return SimpleNamespace(
        message=None,
        edited_message=None,
        callback_query=SimpleNamespace(data="tog:1"),
        effective_chat=chat,
        update_id=1,
    )


@pytest.fixture
def webhook_client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "s3cret")
    monkeypatch.setattr(main, "Update", SimpleNamespace(de_json=staticmethod(_callback_update)))
    fake = _FakeTelegramApp()
    main.app.state.telegram_app = fake
    main.app.state.limiter = main.limiter
    main.limiter.reset()
    main._telegram_hits.clear()
    client = TestClient(main.app, raise_server_exceptions=False)
    client.tg = fake
    return client


_HDR = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
_BODY = {"update_id": 1, "callback_query": {"data": "tog:1"}}


def test_callback_query_webhook_reaches_process_update(webhook_client):
    assert webhook_client.post("/webhook/telegram", json=_BODY, headers=_HDR).status_code == 200
    assert len(webhook_client.tg.updates) == 1


def test_callback_query_webhook_is_rate_limited_per_chat(webhook_client, monkeypatch):
    monkeypatch.setattr(main, "_telegram_rate_ok", lambda chat_id: False)
    resp = webhook_client.post("/webhook/telegram", json=_BODY, headers=_HDR)
    assert resp.status_code == 200  # never 4xx — Telegram must not retry
    assert webhook_client.tg.updates == []


def test_callback_query_webhook_requires_secret(webhook_client):
    assert webhook_client.post("/webhook/telegram", json=_BODY).status_code == 401
