"""Weekly behavioral-summary memory (Phase 4).

`summarize_user_week(user_id)` is an importable function — `scheduler.py`'s
Sunday 23:59 job calls it directly. It pulls the user's last 7 days of habit
logs, asks a Claude model for a short third-person behavioral summary, and
stores that summary in the `habit_memory` vector collection (via
`src/vector_store.HabitMemoryStore`) tagged with the user's id. The agent's
`query_past_behavior` tool reads these back for advice/reflection questions.

The summary model is `langchain_anthropic.ChatAnthropic` (same library the
agent uses). Only the embedding of the stored text is non-Anthropic — that
happens inside `vector_store.HabitMemoryStore` (OpenAI `text-embedding-3-small`);
this module never calls an embedding API itself.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from langchain_anthropic import ChatAnthropic
from langsmith import traceable

from .database import Habit, User, get_session
from .vector_store import get_habit_memory_store

logger = logging.getLogger(__name__)

# Worker agent runs on claude-sonnet-5 (see agent.py). This weekly background
# summary is a simple summarization task, so it stays on the same tier for
# consistency; bump to a stronger model here if summary quality matters more.
SUMMARY_MODEL = "claude-sonnet-5"
SUMMARY_MAX_TOKENS = 400
LOOKBACK_DAYS = 7

_SUMMARY_SYSTEM = (
    "You write concise third-person behavioral summaries of one person's "
    "habit-tracking week, for a coaching assistant's long-term memory. "
    "Write 3-5 sentences. Be factual and specific: which habits held, which "
    "slipped, any day-of-week or timing pattern, and the overall trajectory "
    "versus a typical week. No advice, no encouragement, no second person — "
    "only what happened."
)


def _as_text(content: object) -> str:
    """Normalize an AIMessage.content (str, or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _collect_week(user_id: int, today: date) -> tuple[str, int]:
    """Return (plain-text per-habit log digest, total log-row count) for the
    trailing 7 days (inclusive of today)."""
    window_start = today - timedelta(days=LOOKBACK_DAYS - 1)
    session = get_session()
    try:
        habits = session.query(Habit).filter(Habit.user_id == user_id).all()
        lines: list[str] = []
        row_count = 0
        for habit in habits:
            entries = sorted(
                (log for log in habit.logs if window_start <= log.date <= today),
                key=lambda log: log.date,
            )
            row_count += len(entries)
            detail = (
                ", ".join(f"{log.date.isoformat()}={log.status}" for log in entries)
                if entries
                else "no entries this week"
            )
            lines.append(f"- {habit.name} (frequency: {habit.frequency}): {detail}")
        return "\n".join(lines), row_count
    finally:
        session.close()


def _scrub_summary_inputs(inputs: dict) -> dict:
    """Phase 6.1 — LangSmith records only the user id and target date for this
    run, never the pulled habit-log rows or any credential (none reach here)."""
    return {
        "user_id": inputs.get("user_id"),
        "today": str(inputs["today"]) if inputs.get("today") is not None else None,
    }


@traceable(
    run_type="chain",
    name="summarize_user_week",
    process_inputs=_scrub_summary_inputs,
)
def summarize_user_week(user_id: int, *, today: date | None = None) -> str | None:
    """Generate and store one weekly behavioral summary for `user_id`.

    Returns the summary text, or `None` when there is nothing to summarize
    (the user has no habits, or logged no activity in the window). Raises on
    an API/store failure — the scheduler job wraps the call and logs.
    """
    today = today or date.today()
    window_start = today - timedelta(days=LOOKBACK_DAYS - 1)

    digest, row_count = _collect_week(user_id, today)
    if not digest or row_count == 0:
        logger.info(
            "summarize_user_week: no logged activity for user_id=%d in %s..%s — skipped.",
            user_id,
            window_start.isoformat(),
            today.isoformat(),
        )
        return None

    model = ChatAnthropic(model=SUMMARY_MODEL, max_tokens=SUMMARY_MAX_TOKENS)
    response = model.invoke(
        [
            ("system", _SUMMARY_SYSTEM),
            (
                "human",
                f"Week of {window_start.isoformat()} to {today.isoformat()}.\n\n"
                f"Habit logs:\n{digest}\n\nWrite the behavioral summary.",
            ),
        ]
    )
    summary = _as_text(response.content).strip()
    if not summary:
        logger.warning(
            "summarize_user_week: model returned an empty summary for user_id=%d.", user_id
        )
        return None

    doc_id = f"user{user_id}-week-{today.isoformat()}"
    get_habit_memory_store().add_summary(
        doc_id=doc_id,
        text=summary,
        user_id=user_id,
        metadata={"week_ending": today.isoformat()},
    )
    logger.info("summarize_user_week: stored %s (%d chars).", doc_id, len(summary))
    return summary


def _resolve_user_id(username: str) -> int:
    """For the __main__ convenience path only — mirrors scheduler._resolve_user_id."""
    session = get_session()
    try:
        user = session.query(User).filter(User.username == username).first()
        if user is None:
            raise RuntimeError(f"HABIT_TRACKER_USERNAME={username!r} matches no account.")
        return user.id
    finally:
        session.close()


def main() -> None:
    """Manual run: `uv run python -m src.summarize_memory` — summarizes the
    HABIT_TRACKER_USERNAME account's current week. The scheduler calls
    `summarize_user_week` directly, not this."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # Standalone entry point — load .env ourselves (when imported by the
    # scheduler, main.py has already done this).
    from dotenv import load_dotenv

    load_dotenv()
    username = os.environ["HABIT_TRACKER_USERNAME"]
    summary = summarize_user_week(_resolve_user_id(username))
    print(summary if summary is not None else "(nothing to summarize this week)")


if __name__ == "__main__":
    main()
