"""Daily habit-reminder scheduler (Phase 2).

Runs in-process with the FastAPI app, started and stopped from main.py's
existing lifespan. Unlike src/bot.py and src/app.py, this module is a
*trusted* in-process context and may import src/database.py directly
(CLAUDE.md).

One job: every day at 20:00 (Asia/Dhaka), check whether the single configured
account (HABIT_TRACKER_USERNAME) has any habit that is due today and not yet
satisfied, and if so send one Telegram reminder to TELEGRAM_CHAT_ID.

Phase 3: the reminder text is no longer a static string — the job runs the
shared run_friction_nudge() flow, which invokes the same coaching agent
(same per-user thread) to produce a pattern-aware nudge. POST /evaluate_friction
in main.py calls that same function, so the "what's pending / what to say"
logic exists in exactly one place.
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

logger = logging.getLogger(__name__)

REMINDER_HOUR = 20
REMINDER_MINUTE = 0
JOB_ID = "daily_habit_reminder"
# Pin the reminder to a fixed zone rather than the server's local time, so it
# still fires at 20:00 Dhaka after deploying to a UTC cloud host. Override
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


def start_reminder_scheduler(agent) -> AsyncIOScheduler:
    """Build, start, and return the scheduler. The caller (main.py's
    lifespan) owns the returned object and must call .shutdown() on teardown.

    `agent` is the shared instance from build_agent() — the 20:00 job passes
    it to run_friction_nudge() to generate the reminder text. It must already
    exist when this is called, so main.py's lifespan builds the agent first.

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
    scheduler.start()
    logger.info(
        "Reminder scheduler started — daily at %02d:%02d %s for user_id=%d.",
        REMINDER_HOUR,
        REMINDER_MINUTE,
        REMINDER_TIMEZONE.key,
        user_id,
    )
    return scheduler
