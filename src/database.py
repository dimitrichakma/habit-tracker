"""SQLAlchemy models and table setup for the habit tracker.

Storage is Postgres (Neon, with the `pgvector` extension) in deployment and
SQLite locally / in the eval suite. Only the engine wiring below cares which
one it is — the models, relationships, per-user scoping, and every piece of
business logic (`is_due_today`, `is_satisfied`, `_migrate_add_habits_user_id`,
`backfill_orphaned_habits`) are identical on both.
"""

import os
import re
from datetime import date, datetime, timedelta

from sqlalchemy import Date, DateTime, ForeignKey, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

# Neon (or any Postgres) via DATABASE_URL; falls back to a local SQLite file so
# `import src.database` never hard-fails when the var is unset (local dev, and
# the Phase 4 eval suite, which swaps in its own throwaway SQLite engine).
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///habits.db")


def _normalize_pg_url(url: str) -> str:
    """Force the psycopg (v3) driver. SQLAlchemy maps a bare `postgresql://`
    to psycopg2, which isn't installed; Neon hands out bare URLs."""
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


if DATABASE_URL.startswith("sqlite"):
    #  check_same_thread=False: SQLite normally refuses to let a connection be
    # used from a different thread than the one that created it. FastAPI can
    # handle requests on different worker threads, so this relaxes that check —
    # safe here since every request opens (and closes) its own fresh session
    # via get_session() rather than sharing one connection across requests.
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # pool_pre_ping: Neon drops idle connections; ping-and-reconnect instead of
    # handing out a dead one. prepare_threshold=None: disable psycopg3 prepared
    # statements so the same URL works through Neon's pooled (PgBouncer /
    # "-pooler") endpoint as well as the direct one.
    engine = create_engine(
        _normalize_pg_url(DATABASE_URL),
        pool_pre_ping=True,
        connect_args={"prepare_threshold": None},
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    """A real account. Every Habit belongs to exactly one User (see
    Habit.user_id below) — this is what makes habit data private per
    account instead of shared globally."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.utcnow())


class Habit(Base):
    """One habit a user is tracking, e.g. "Drink water", frequency "daily"."""

    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    frequency: Mapped[str] = mapped_column(String, nullable=False)

    logs: Mapped[list["HabitLog"]] = relationship(back_populates="habit", cascade="all, delete-orphan")


class HabitLog(Base):
    """One day's recorded status for a habit, e.g. Habit "Drink water" on
    2026-08-28 was "done". No direct user_id column here on purpose —
    ownership flows through habit_id -> Habit.user_id instead (see
    CLAUDE.md for the reasoning), so there's nothing to keep in sync."""

    __tablename__ = "habit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    habit_id: Mapped[int] = mapped_column(ForeignKey("habits.id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)

    habit: Mapped["Habit"] = relationship(back_populates="logs")


def init_db() -> None:
    if engine.dialect.name == "postgresql":
        # pgvector's tables (langchain_pg_*) live in the same database; the
        # extension has to exist before the vector store creates them.
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        _migrate_add_habits_user_id()


def _migrate_add_habits_user_id() -> None:
    """One-time, additive migration for pre-auth databases: adds
    habits.user_id as a nullable column (SQLite can't add a NOT NULL column
    with no default to a table that already has rows). Idempotent — checks
    PRAGMA table_info first, so it's always safe to call on every startup.
    A fresh database never hits this path: create_all() above already
    creates user_id as NOT NULL from row one when the table doesn't exist yet.

    SQLite only — Postgres deployments start post-auth, so there is no
    pre-auth column to backfill, and PRAGMA isn't valid Postgres anyway.
    """
    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(habits)")}
        if "user_id" not in columns:
            conn.exec_driver_sql("ALTER TABLE habits ADD COLUMN user_id INTEGER REFERENCES users(id)")
            conn.commit()


def backfill_orphaned_habits(session: Session, owner_user_id: int) -> int:
    """Attach every still-ownerless habit (user_id IS NULL — pre-auth data)
    to owner_user_id. Safe to call on every signup, not just the first: the
    WHERE clause makes repeat calls free no-ops once the NULL set is empty,
    which avoids a race over "am I the first user" between concurrent signups.
    """
    count = session.query(Habit).filter(Habit.user_id.is_(None)).update({"user_id": owner_user_id})
    session.commit()
    return count


def get_session() -> Session:
    """A fresh, independent DB session. Every caller (each tool, each
    endpoint) opens its own via this and must close() it when done — see
    the try/finally pattern used everywhere this is called."""
    return SessionLocal()


# --- Shared scheduling logic -------------------------------------------
# Used by both main.py's /habits/today dashboard and tools.py's
# get_pending_habits() tool, so the dashboard and the chatbot's own
# reasoning about "what's pending" can never disagree with each other.

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _excluded_weekday(frequency: str) -> str | None:
    """Extract a weekday named in an "except <Weekday>" clause, if any."""
    match = re.search(r"except\s+(\w+)", frequency, re.IGNORECASE)
    if match and match.group(1).lower() in _WEEKDAYS:
        return match.group(1).lower()
    return None


def is_due_today(habit: Habit, today: date) -> bool:
    """Whether this habit is applicable today (not skipped by an "except <Weekday>" clause)."""
    excluded = _excluded_weekday(habit.frequency)
    return excluded != _WEEKDAYS[today.weekday()]


def satisfaction_window_days(frequency: str) -> int:
    """How many trailing days (including today) a 'done' log stays valid for."""
    return 7 if "week" in frequency.lower() else 1


def is_satisfied(habit: Habit, today: date) -> bool:
    """Whether this habit currently counts as done, per its frequency's rolling window."""
    window_start = today - timedelta(days=satisfaction_window_days(habit.frequency) - 1)
    return any(log.status == "done" and window_start <= log.date <= today for log in habit.logs)
