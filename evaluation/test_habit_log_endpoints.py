"""Offline tests for the desktop quick-logging path: ``POST /habits/{id}/log``
plus the shared ``database.upsert_habit_log`` / ``delete_habit_log`` helpers
it and the chat tools both write through.

Same isolation as ``test_gateway_security.py``: a throwaway SQLite, the
FastAPI lifespan NOT run (no agent, no Telegram, no Postgres), JWTs minted
directly with ``create_access_token``. No API keys or network needed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import src.database as database
import src.main as main
import src.tools as tools
from src.auth import create_access_token

TODAY = date.today()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the app DB at a throwaway SQLite file (never the real Neon)."""
    engine = sqlalchemy.create_engine(
        f"sqlite:///{tmp_path / 'habit_log.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    database.init_db()
    yield


@pytest.fixture
def client():
    main.app.state.limiter = main.limiter
    main.limiter.reset()
    return TestClient(main.app, raise_server_exceptions=False)


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


def _logs(habit_id: int) -> list[database.HabitLog]:
    session = database.get_session()
    try:
        return session.query(database.HabitLog).filter_by(habit_id=habit_id).all()
    finally:
        session.close()


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id, f'user{user_id}')}"}


@pytest.fixture
def owner():
    """(user_id, habit_id) for one user with one daily habit."""
    user_id = _make_user("alice")
    return user_id, _make_habit(user_id)


# --- POST /habits/{id}/log -------------------------------------------


def test_log_done_creates_one_row(client, owner):
    user_id, habit_id = owner
    resp = client.post(f"/habits/{habit_id}/log", json={"done": True}, headers=_auth(user_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"habit_id": habit_id, "name": "Read", "date": TODAY.isoformat(), "status": "done"}
    logs = _logs(habit_id)
    assert [(log.date, log.status) for log in logs] == [(TODAY, "done")]


def test_log_done_twice_no_duplicate(client, owner):
    user_id, habit_id = owner
    for _ in range(2):
        assert client.post(f"/habits/{habit_id}/log", json={"done": True}, headers=_auth(user_id)).status_code == 200
    assert len(_logs(habit_id)) == 1


def test_log_missed_then_done_updates_same_row(client, owner):
    user_id, habit_id = owner
    session = database.get_session()
    try:
        seeded = database.upsert_habit_log(session, habit_id, TODAY, "missed")
        session.commit()
        seeded_id = seeded.id
    finally:
        session.close()

    client.post(f"/habits/{habit_id}/log", json={"done": True}, headers=_auth(user_id))
    logs = _logs(habit_id)
    assert [(log.id, log.status) for log in logs] == [(seeded_id, "done")]


def test_not_done_removes_log(client, owner):
    user_id, habit_id = owner
    client.post(f"/habits/{habit_id}/log", json={"done": True}, headers=_auth(user_id))
    resp = client.post(f"/habits/{habit_id}/log", json={"done": False}, headers=_auth(user_id))
    assert resp.status_code == 200
    assert resp.json()["status"] is None
    assert _logs(habit_id) == []


def test_not_done_when_nothing_logged_is_noop(client, owner):
    user_id, habit_id = owner
    resp = client.post(f"/habits/{habit_id}/log", json={"done": False}, headers=_auth(user_id))
    assert resp.status_code == 200
    assert _logs(habit_id) == []


def test_explicit_past_date(client, owner):
    user_id, habit_id = owner
    past = TODAY - timedelta(days=2)
    resp = client.post(
        f"/habits/{habit_id}/log", json={"done": True, "date": past.isoformat()}, headers=_auth(user_id)
    )
    assert resp.status_code == 200
    assert resp.json()["date"] == past.isoformat()
    assert [log.date for log in _logs(habit_id)] == [past]


def test_future_date_rejected(client, owner):
    user_id, habit_id = owner
    future = TODAY + timedelta(days=1)
    resp = client.post(
        f"/habits/{habit_id}/log", json={"done": True, "date": future.isoformat()}, headers=_auth(user_id)
    )
    assert resp.status_code == 422
    assert _logs(habit_id) == []


def test_bad_date_rejected(client, owner):
    user_id, habit_id = owner
    resp = client.post(
        f"/habits/{habit_id}/log", json={"done": True, "date": "yesterday-ish"}, headers=_auth(user_id)
    )
    assert resp.status_code == 422
    assert _logs(habit_id) == []


def test_requires_auth(client, owner):
    _, habit_id = owner
    no_header = client.post(f"/habits/{habit_id}/log", json={"done": True})
    bad_token = client.post(
        f"/habits/{habit_id}/log", json={"done": True}, headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert no_header.status_code in (401, 403)
    assert bad_token.status_code == 401
    assert _logs(habit_id) == []


def test_other_users_habit_is_404(client, owner):
    _, alice_habit = owner
    bob = _make_user("bob")
    resp = client.post(f"/habits/{alice_habit}/log", json={"done": True}, headers=_auth(bob))
    assert resp.status_code == 404
    assert _logs(alice_habit) == []


def test_unknown_habit_is_404(client, owner):
    user_id, _ = owner
    resp = client.post("/habits/99999/log", json={"done": True}, headers=_auth(user_id))
    assert resp.status_code == 404


def test_get_today_reflects_post(client, owner):
    user_id, habit_id = owner

    def _ids(bucket: str) -> list[int]:
        return [h["id"] for h in client.get("/habits/today", headers=_auth(user_id)).json()[bucket]]

    assert _ids("pending") == [habit_id]
    client.post(f"/habits/{habit_id}/log", json={"done": True}, headers=_auth(user_id))
    assert _ids("done") == [habit_id]
    client.post(f"/habits/{habit_id}/log", json={"done": False}, headers=_auth(user_id))
    assert _ids("pending") == [habit_id]


class _FixedRuntime:
    """Minimal ToolRuntime stand-in (same shape as mcp_server._FixedRuntime)."""

    def __init__(self, user_id: int) -> None:
        self.config = {"configurable": {"thread_id": str(user_id)}}


def test_chat_and_http_share_one_row(client, owner):
    user_id, habit_id = owner
    # Exact habit name -> _find_habit's first step, no TypeSafe call.
    reply = tools.log_habit.func("Read", "missed", runtime=_FixedRuntime(user_id))
    assert "Logged 'Read'" in reply

    client.post(f"/habits/{habit_id}/log", json={"done": True}, headers=_auth(user_id))
    assert [log.status for log in _logs(habit_id)] == ["done"]

    reply = tools.undo_habit_log.func("Read", runtime=_FixedRuntime(user_id))
    assert "Removed the log" in reply
    assert _logs(habit_id) == []


# --- shared helpers ---------------------------------------------------


def test_upsert_habit_log_inserts_then_updates(owner):
    _, habit_id = owner
    session = database.get_session()
    try:
        first = database.upsert_habit_log(session, habit_id, TODAY, "missed")
        session.commit()
        second = database.upsert_habit_log(session, habit_id, TODAY, "done")
        session.commit()
        assert first.id == second.id
    finally:
        session.close()
    assert [log.status for log in _logs(habit_id)] == ["done"]


def test_delete_habit_log_reports_whether_it_deleted(owner):
    _, habit_id = owner
    session = database.get_session()
    try:
        assert database.delete_habit_log(session, habit_id, TODAY) is False
        database.upsert_habit_log(session, habit_id, TODAY, "done")
        session.commit()
        assert database.delete_habit_log(session, habit_id, TODAY) is True
        session.commit()
    finally:
        session.close()
    assert _logs(habit_id) == []
