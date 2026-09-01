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
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# main.py passes a coroutine function: message text in, coach's reply out.
OnMessage = Callable[[str], Awaitable[str]]


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
    except Exception as exc:  # report the failure to the user — never crash silently
        logger.exception("Failed to handle a Telegram message.")
        await update.message.reply_text(f"⚠️ Sorry, something went wrong: {exc}")
        return
    await update.message.reply_text(reply)


def build_application(on_message: OnMessage) -> Application:
    """Build the Telegram `Application` with its handlers wired up. The caller
    (main.py's lifespan) is responsible for `initialize()` / `start()` /
    `stop()` / `shutdown()` and for registering the webhook.

    `on_message(text) -> reply_text` is stashed in `bot_data` and called by
    the message handler for every (non-command) text message.
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
    application.add_handler(MessageHandler(message_filter, _handle_message))
    return application
