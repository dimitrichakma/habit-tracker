"""Telegram front-end for the Habit Coach (Phase 2, webhook form).

This module owns the python-telegram-bot `Application` and its handlers. It
does NOT run as its own process and does NOT poll — `src/main.py` builds the
`Application` via `build_application()`, drives it in-process
(`initialize()` / `start()` / `process_update()` / `stop()`), and registers
the Telegram webhook. Polling (`run_polling`) is fatal in a serverless
environment like Cloud Run, where the container only runs while it's serving
an HTTP request.

Still no LangChain / LangGraph / database imports here: `build_application()`
takes an `on_message` callback and `main.py` supplies one that invokes the
shared agent. The handler just does Telegram I/O.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# main.py passes a coroutine function: message text in, coach's reply out.
OnMessage = Callable[[str], Awaitable[str]]


class TodayItem(NamedTuple):
    """One habit on the /today keyboard. `state` is what a tap will do:
    "done" (today's log is done — tap removes it), "earlier" (satisfied by an
    earlier day in a weekly window — tap logs today), "pending" (tap logs
    today). `earlier_day` is that earlier weekday's short name."""

    habit_id: int
    name: str
    state: str
    earlier_day: str | None


class TodayView(NamedTuple):
    label: str
    items: list[TodayItem]


OnToday = Callable[[], Awaitable[TodayView]]
OnToggle = Callable[[int], Awaitable[TodayView | None]]  # None: not found / not yours

_TOGGLE_PATTERN = r"^tog:\d+$"


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Hi — I'm your Habit Coach. Tell me about a habit you want to build, "
        "log one as done or missed, or ask how your week is going."
    )


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.text is None:
        return
    on_message: OnMessage = context.application.bot_data["on_message"]

    if update.effective_chat is not None:
        await context.bot.send_chat_action(update.effective_chat.id, action="typing")

    try:
        reply = await on_message(update.message.text)
    except Exception:  # generic reply only — the real error is logged server-side
        # `on_message` (main._telegram_reply) already returns a plain string for
        # every user-facing case (oversized / masking / budget / timeout). This
        # is the backstop for the unexpected: never echo `exc` to the user — it
        # can carry a DB error string or the "no account for
        # HABIT_TRACKER_USERNAME=<name>" RuntimeError.
        logger.exception("Failed to handle a Telegram message.")
        await update.message.reply_text(
            "⚠️ Something went wrong on my end. Please try again in a moment."
        )
        return
    await update.message.reply_text(reply)


def _is_linked_user(update: Update) -> bool:
    """Only the user behind TELEGRAM_CHAT_ID may use the tap-to-log buttons.
    Checked on /today and on EVERY tap (callback queries bypass the message
    chat filter). These handlers write data, so unlike plain chat this fails
    CLOSED when TELEGRAM_CHAT_ID is unset."""
    allowed = os.environ.get("TELEGRAM_CHAT_ID")
    user = update.effective_user
    return bool(allowed) and user is not None and user.id == int(allowed)


def render_today(view: TodayView) -> tuple[str, InlineKeyboardMarkup | None]:
    if not view.items:
        return f"Today ({view.label}): no habits due.", None
    rows = []
    for item in view.items:
        if item.state == "done":
            label = f"✅ {item.name}"
        elif item.state == "earlier":
            label = f"☑️ {item.name} (done {item.earlier_day})"
        else:
            label = f"⬜ {item.name}"
        rows.append([InlineKeyboardButton(label, callback_data=f"tog:{item.habit_id}")])
    return f"Today ({view.label}) — tap to toggle:", InlineKeyboardMarkup(rows)


async def _handle_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not _is_linked_user(update):
        return
    on_today: OnToday = context.application.bot_data["on_today"]
    try:
        text, markup = render_today(await on_today())
    except Exception:
        logger.exception("Failed to build the /today view.")
        await update.message.reply_text("⚠️ Something went wrong on my end. Please try again in a moment.")
        return
    await update.message.reply_text(text, reply_markup=markup)


async def _handle_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not _is_linked_user(update):
        await query.answer("Not authorized.", show_alert=True)
        return
    on_toggle: OnToggle = context.application.bot_data["on_toggle"]
    started = time.perf_counter()
    # Acknowledge first: Telegram shows a spinner on the button until the
    # query is answered, so do it before the database work, not after.
    await query.answer()
    answered = time.perf_counter()
    try:
        view = await on_toggle(int(query.data.split(":", 1)[1]))
        if view is None:
            # Missing / not this user's habit (a stale or forged button). The
            # query is already answered, so re-render the user's real view
            # instead of alerting; nothing was written.
            logger.warning("Telegram tap on an unknown or foreign habit; refreshing the view.")
            view = await context.application.bot_data["on_today"]()
    except Exception:  # generic message only; the real error stays in the logs
        logger.exception("Failed to toggle a habit from a Telegram tap.")
        if update.effective_chat is not None:
            await context.bot.send_message(
                update.effective_chat.id, "⚠️ Something went wrong. Please try again."
            )
        return
    worked = time.perf_counter()
    text, markup = render_today(view)
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    finished = time.perf_counter()
    logger.info(
        "Telegram tap timing: answer=%.0fms db=%.0fms edit=%.0fms total=%.0fms",
        (answered - started) * 1000,
        (worked - answered) * 1000,
        (finished - worked) * 1000,
        (finished - started) * 1000,
    )


def build_application(
    on_message: OnMessage,
    on_today: OnToday | None = None,
    on_toggle: OnToggle | None = None,
) -> Application:
    """Build the Telegram `Application` with its handlers wired up. The caller
    (main.py's lifespan) is responsible for `initialize()` / `start()` /
    `stop()` / `shutdown()` and for registering the webhook.

    `on_message(text) -> reply_text` is stashed in `bot_data` and called by
    the message handler for every (non-command) text message. `on_today()` /
    `on_toggle(habit_id)` back the /today tap-to-log keyboard (no LLM); they
    are optional so the chat-only wiring keeps working.
    """
    application = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    application.bot_data["on_message"] = on_message

    # Single-user scope: if TELEGRAM_CHAT_ID is set, only that chat is served
    # (every message still runs as the one configured account regardless).
    message_filter = filters.TEXT & ~filters.COMMAND
    allowed_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if allowed_chat:
        message_filter &= filters.Chat(int(allowed_chat))
    else:
        logger.warning(
            "TELEGRAM_CHAT_ID is not set — the bot will respond to anyone who "
            "messages it, all acting as the single configured account."
        )

    application.add_handler(CommandHandler("start", _handle_start))
    if on_today is not None and on_toggle is not None:
        application.bot_data["on_today"] = on_today
        application.bot_data["on_toggle"] = on_toggle
        application.add_handler(CommandHandler("today", _handle_today))
        application.add_handler(CallbackQueryHandler(_handle_toggle, pattern=_TOGGLE_PATTERN))
    application.add_handler(MessageHandler(message_filter, _handle_message))
    return application
