"""In-process scheduler (Phase 2/3/4).

Runs in-process with the FastAPI app, started and stopped from main.py's
existing lifespan. Unlike src/bot.py and src/app.py, this module is a
*trusted* in-process context and may import src/database.py directly
(CLAUDE.md).

ONE AsyncIOScheduler instance, two jobs:
  - Daily 20:00 (Asia/Dhaka) — if the single configured account
    (HABIT_TRACKER_USERNAME) has habits still pending, send one Telegram
    nudge. Phase 3: the text comes from the shared run_friction_nudge()
    flow (same agent, same per-user thread); POST /evaluate_friction calls
    that same function, so the "what's pending / what to say" logic lives
    in exactly one place.
  - Weekly, Sunday 23:59 (Asia/Dhaka) — Phase 4. Calls
    summarize_memory.summarize_user_week() to write that week's behavioral
    summary into the habit_memory vector store.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot

from .database import Habit, User, get_session, is_due_today, is_satisfied
from .summarize_memory import summarize_user_week

logger = logging.getLogger(__name__)

REMINDER_HOUR = 20
REMINDER_MINUTE = 0
JOB_ID = "daily_habit_reminder"  # the daily nudge job

# Weekly behavioral-summary job (Phase 4).
SUMMARY_JOB_ID = "weekly_behavior_summary"
SUMMARY_DAY_OF_WEEK = "sun"
SUMMARY_HOUR = 23
SUMMARY_MINUTE = 59

# Pin both jobs to a fixed zone rather than the server's local time, so they
# still fire on Dhaka wall-clock after deploying to a UTC cloud host. Override
# with the REMINDER_TIMEZONE env var if the account ever moves zones.
REMINDER_TIMEZONE = ZoneInfo(os.environ.get("REMINDER_TIMEZONE", "Asia/Dhaka"))


def _resolve_user_id(username: str) -> int:
    """Look up the target account once, at startup. Raises if it doesn't
    exist yet — sign up through the app first, then (re)start the backend."""
    session = get_session()
    try:
        user = session.query(User).filter(User.username == username).first()
        if user is None:
            raise RuntimeError(
                f"HABIT_TRACKER_USERNAME={username!r} matches no account. "
                "Create it via the app, then restart the backend."
            )
        return user.id
    finally:
        session.close()


def _pending_habit_names(user_id: int, today: date) -> list[str]:
    """Names of habits that are due today but not yet satisfied — the same
    'pending' definition as /habits/today and the get_pending_habits tool,
    via the shared is_due_today / is_satisfied helpers (never a separate
    raw query)."""
    session = get_session()
    try:
        habits = session.query(Habit).filter(Habit.user_id == user_id).all()
        return [
            habit.name
            for habit in habits
            if is_due_today(habit, today) and not is_satisfied(habit, today)
        ]
    finally:
        session.close()


def _reply_text(content: object) -> str:
    """Pull plain text out of the agent's final message. Usually a str; a
    list of blocks (text + thinking) once extended thinking is on. Same
    normalization as main._as_text — kept local rather than imported, since
    main imports this module (importing back would be circular)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


async def run_friction_nudge(agent, user_id: int) -> str | None:
    """The shared 'evening friction check' flow behind BOTH the 20:00 job
    and POST /evaluate_friction — the pending-habit check lives here once,
    never duplicated (CLAUDE.md).

    Returns the coach's message, or None when nothing is pending (so the
    scheduler stays silent and the endpoint can say "all caught up").

    `agent` is the shared instance from build_agent(); it's invoked on the
    caller's own thread_id (== user_id), so the pattern-check ->
    micro-commitment reasoning and the never-guess / never-shame persona
    all come from agent.py's system prompt. This function only decides
    whether to nudge and what to ask.
    """
    pending = _pending_habit_names(user_id, date.today())
    if not pending:
        return None

    listed = ", ".join(pending)
    trigger = (
        "This is the automated evening check-in. The user still has these habits "
        f"pending today: {listed}. Give a short, encouraging nudge (a few sentences, "
        "no lists). If any of these habits shows a genuine recurring failure pattern, "
        "follow your normal missed-habit process for it and offer a smaller "
        "micro-commitment. Do not shame the user."
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": trigger}]},
        config={"configurable": {"thread_id": str(user_id)}},
    )
    return _reply_text(result["messages"][-1].content) or f"Still pending today: {listed}."


async def _send_reminder(agent, bot_token: str, chat_id: str, user_id: int) -> None:
    """The scheduled job. A fresh short-lived Bot per fire (once a day)
    sidesteps python-telegram-bot's connection-lifecycle handling."""
    try:
        message = await run_friction_nudge(agent, user_id)
        if message is None:
            logger.info("Daily check: nothing pending — no reminder sent.")
            return

        async with Bot(token=bot_token) as bot:
            await bot.send_message(chat_id=chat_id, text=message)
        logger.info("Sent daily agent nudge.")
    except Exception:
        # Never let a job failure bubble into (and silently kill) the
        # scheduler — log it loudly and let tomorrow's run try again.
        logger.exception("Daily reminder job failed.")


def _run_weekly_summary(user_id: int) -> None:
    """Weekly job (Sunday 23:59). A plain sync function on purpose:
    AsyncIOScheduler runs non-coroutine jobs in a worker thread, so the
    blocking Claude + embedding call inside summarize_user_week() never
    stalls the event loop (or the daily nudge job). Swallows its own
    errors — one bad week must not take the scheduler down."""
    try:
        summary = summarize_user_week(user_id)
        if summary is None:
            logger.info("Weekly summary: no logged activity this week — nothing stored.")
        else:
            logger.info("Weekly summary stored (%d chars).", len(summary))
    except Exception:
        logger.exception("Weekly behavioral-summary job failed.")


def start_reminder_scheduler(agent) -> AsyncIOScheduler:
    """Build, start, and return the scheduler. The caller (main.py's
    lifespan) owns the returned object and must call .shutdown() on teardown.

    `agent` is the shared instance from build_agent() — the 20:00 job passes
    it to run_friction_nudge() to generate the reminder text. It must already
    exist when this is called, so main.py's lifespan builds the agent first.

    Both jobs (daily nudge, weekly summary) go on this ONE scheduler instance
    — never a second one.

    Requires TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HABIT_TRACKER_USERNAME to
    be set and the account to exist — raises otherwise.
    """
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    username = os.environ["HABIT_TRACKER_USERNAME"]

    user_id = _resolve_user_id(username)

    scheduler = AsyncIOScheduler(timezone=REMINDER_TIMEZONE)
    scheduler.add_job(
        _send_reminder,
        trigger=CronTrigger(
            hour=REMINDER_HOUR, minute=REMINDER_MINUTE, timezone=REMINDER_TIMEZONE
        ),
        args=(agent, bot_token, chat_id, user_id),
        id=JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,  # still fire if the backend started slightly late
    )
    scheduler.add_job(
        _run_weekly_summary,
        trigger=CronTrigger(
            day_of_week=SUMMARY_DAY_OF_WEEK,
            hour=SUMMARY_HOUR,
            minute=SUMMARY_MINUTE,
            timezone=REMINDER_TIMEZONE,
        ),
        args=(user_id,),
        id=SUMMARY_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info(
        "Scheduler started for user_id=%d — daily nudge at %02d:%02d %s, "
        "weekly summary %s %02d:%02d %s.",
        user_id,
        REMINDER_HOUR,
        REMINDER_MINUTE,
        REMINDER_TIMEZONE.key,
        SUMMARY_DAY_OF_WEEK,
        SUMMARY_HOUR,
        SUMMARY_MINUTE,
        REMINDER_TIMEZONE.key,
    )
    return scheduler
