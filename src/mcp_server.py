"""Standalone MCP server (Phase 3) — a learning exercise, NOT wired into the
production agent.

Run it on its own and point an external MCP client (Claude Desktop, MCP
Inspector) at it:

    uv run python -m src.mcp_server

It talks to the *same* habits.db as the real app (so it must be run from the
project root, like everything else here), but it is a completely separate
path from src/agent.py + src/tools.py — the production agent keeps using its
own local, ToolRuntime-scoped tools. Nothing here is imported by the app.

Design constraints (see CLAUDE.md):
  * Exposes only log_habit, get_pending_habits, list_habits,
    get_weekly_summary — all read-mostly / low-risk.
  * Does NOT expose delete_habit. That tool cascades to a habit's entire
    log history, and its only safeguard ("confirm the exact name first")
    lives in the agent's system prompt — an external MCP client has no such
    safety net.
  * Identity is fixed ONCE at startup from HABIT_TRACKER_USERNAME. No tool
    takes a user_id; nothing about identity is caller-suppliable. This
    mirrors how src/bot.py and src/scheduler.py already establish identity.
  * The actual habit logic is not reimplemented here — each MCP tool is a
    thin shim over the corresponding function already written in
    src/tools.py, called with a fixed-identity runtime.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from .database import User, get_session
from .tools import get_pending_habits as _get_pending_habits
from .tools import get_weekly_summary as _get_weekly_summary
from .tools import list_habits as _list_habits
from .tools import log_habit as _log_habit

load_dotenv()

mcp = FastMCP("habit-tracker")


class _FixedRuntime:
    """Minimal stand-in for langchain's ToolRuntime. Every tool in
    src/tools.py reads exactly one thing off the runtime —
    runtime.config["configurable"]["thread_id"] (via _current_user_id) — to
    scope its queries to one account. Here that identity is set once, at
    startup, and never varies per call."""

    def __init__(self, user_id: int) -> None:
        self.config = {"configurable": {"thread_id": str(user_id)}}


# Set by main(); the tool shims read it via _runtime().
_RUNTIME: _FixedRuntime | None = None


def _runtime() -> _FixedRuntime:
    if _RUNTIME is None:  # pragma: no cover - only hit on misuse
        raise RuntimeError(
            "MCP server identity not initialised — start it via "
            "`uv run python -m src.mcp_server`, not by importing tool shims."
        )
    return _RUNTIME


def _resolve_user_id(username: str) -> int:
    """Look up the fixed account once, at startup. Raises if it doesn't
    exist — create it through the app first."""
    session = get_session()
    try:
        user = session.query(User).filter(User.username == username).first()
        if user is None:
            raise RuntimeError(
                f"HABIT_TRACKER_USERNAME={username!r} matches no account. "
                "Create it via the app first, then start this server."
            )
        return user.id
    finally:
        session.close()


@mcp.tool()
def log_habit(habit_name: str, status: str, log_date: str = "today") -> str:
    """Record a habit's status for a day.

    Args:
        habit_name: Name of the habit (fuzzy-matched to an existing habit).
        status: Outcome, e.g. "done", "missed", "skipped".
        log_date: "today" (default), "yesterday", or an ISO "YYYY-MM-DD" date.
    """
    return _log_habit.func(habit_name, status, log_date, runtime=_runtime())


@mcp.tool()
def get_pending_habits() -> str:
    """List the account's habits that are due today and not yet satisfied."""
    return _get_pending_habits.func(runtime=_runtime())


@mcp.tool()
def list_habits() -> str:
    """List every habit on the account, regardless of today's status."""
    return _list_habits.func(runtime=_runtime())


@mcp.tool()
def get_weekly_summary() -> str:
    """Summarise the last 7 days per habit (done/due counts, weekly habits
    reported as satisfied-or-not)."""
    return _get_weekly_summary.func(runtime=_runtime())


def main() -> None:
    global _RUNTIME
    username = os.environ.get("HABIT_TRACKER_USERNAME")
    if not username:
        raise RuntimeError(
            "HABIT_TRACKER_USERNAME must be set — it fixes the single account "
            "this MCP server acts as."
        )
    _RUNTIME = _FixedRuntime(_resolve_user_id(username))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
