"""Telegram front-end for the Habit Coach (Phase 2).

Own process, independently runnable:  uv run python -m src.bot

HTTP-only, exactly like src/app.py: this module never imports LangChain,
LangGraph, or the database layer. It talks to the FastAPI backend over httpx
and nothing else. Identity is a single fixed account — the bot logs in once
with HABIT_TRACKER_USERNAME / HABIT_TRACKER_PASSWORD and every Telegram
message is relayed to /chat as that user.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logger = logging.getLogger(__name__)

BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000")
LOGIN_URL = f"{BACKEND_BASE_URL}/auth/login"
CHAT_URL = f"{BACKEND_BASE_URL}/chat"

# Re-authenticate before the backend's 24h JWT expiry (src/auth.py: JWT_EXPIRY).
# This proactive refresh is the primary mechanism; the 401 retry in send_chat()
# is only a fallback, per CLAUDE.md.
TOKEN_REFRESH_AFTER = timedelta(hours=23)

# /chat runs an LLM agent end to end, so it can take a while to respond.
HTTP_TIMEOUT = httpx.Timeout(90.0)


class BackendAuthError(RuntimeError):
    """The bot could not obtain or keep a valid backend session."""


class BackendError(RuntimeError):
    """The backend was reachable but returned an unexpected error."""


class BackendClient:
    """Wraps the FastAPI backend: holds one JWT, refreshes it proactively,
    and relays chat messages. One instance per bot process."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._token: str | None = None
        self._token_issued_at: datetime | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        """Exchange the stored credentials for a fresh JWT. Called once at
        startup (fail fast on bad creds), on proactive refresh, and as the
        401 fallback."""
        try:
            response = await self._client.post(
                LOGIN_URL, json={"username": self._username, "password": self._password}
            )
        except httpx.RequestError as exc:
            raise BackendAuthError(f"Could not reach the backend to log in: {exc}") from exc
        if response.status_code != 200:
            raise BackendAuthError(
                f"Login failed ({response.status_code}): {response.text.strip()}"
            )
        self._token = response.json()["access_token"]
        self._token_issued_at = datetime.now(timezone.utc)
        logger.info("Obtained a fresh backend JWT.")

    async def _ensure_fresh_token(self) -> None:
        if self._token is None or self._token_issued_at is None:
            await self.login()
            return
        age = datetime.now(timezone.utc) - self._token_issued_at
        if age >= TOKEN_REFRESH_AFTER:
            logger.info("JWT is ~%s old — refreshing proactively.", age)
            await self.login()

    async def send_chat(self, message: str) -> str:
        """POST one message to /chat and return the coach's reply text.

        Proactive refresh happens first in _ensure_fresh_token(). The 401
        branch here is a reactive fallback only (e.g. the backend restarted
        with a new JWT secret, or clock skew): re-auth once, retry once,
        then give up.
        """
        await self._ensure_fresh_token()
        reply = await self._post_chat(message)
        if reply is not None:
            return reply

        logger.warning("Unexpected 401 from /chat — re-authenticating once and retrying.")
        await self.login()
        reply = await self._post_chat(message)
        if reply is None:
            raise BackendAuthError("Still unauthorized after re-authenticating.")
        return reply

    async def _post_chat(self, message: str) -> str | None:
        """Return the reply text, or None on a 401 (signal to the caller to
        re-auth). Any other failure raises."""
        try:
            response = await self._client.post(
                CHAT_URL,
                json={"message": message},
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.RequestError as exc:
            raise BackendError(f"Could not reach the backend: {exc}") from exc
        if response.status_code == 401:
            return None
        if response.status_code != 200:
            raise BackendError(
                f"Backend error ({response.status_code}): {response.text.strip()}"
            )
        return response.json()["reply"]


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
    client: BackendClient = context.application.bot_data["backend"]

    if update.effective_chat is not None:
        await context.bot.send_chat_action(update.effective_chat.id, action="typing")

    try:
        reply = await client.send_chat(update.message.text)
    except (BackendAuthError, BackendError) as exc:
        # Report the failure to the user — never crash or retry silently.
        logger.exception("Failed to relay a message to the backend.")
        await update.message.reply_text(f"⚠️ Sorry, something went wrong: {exc}")
        return
    await update.message.reply_text(reply)


def build_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    username = os.environ["HABIT_TRACKER_USERNAME"]
    password = os.environ["HABIT_TRACKER_PASSWORD"]

    async def _post_init(application: Application) -> None:
        client = BackendClient(username, password)
        await client.login()  # fail fast if the backend is down or creds are wrong
        application.bot_data["backend"] = client

    async def _post_shutdown(application: Application) -> None:
        client = application.bot_data.get("backend")
        if client is not None:
            await client.aclose()

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # python-telegram-bot logs every API call through httpx at INFO, and the
    # logged URL embeds the bot token — keep httpx quiet so it never leaks.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_application().run_polling()


if __name__ == "__main__":
    main()
