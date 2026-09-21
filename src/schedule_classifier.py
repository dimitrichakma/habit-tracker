"""Turns a habit's freeform frequency phrase ("daily", "except Sunday",
"3x a week") into a structured schedule — once, at habit-creation time via
`tools.create_new_habit`.

`database.is_due_today` / `is_satisfied` never call this: they read the
structured `Habit.schedule_type` / `excluded_weekday` columns this produces,
so the dashboard render / pending-check hot path stays pure deterministic
code with zero runtime model calls, same as everything else on that path.

Fails soft: any classifier miss (low confidence, an unsupported shape, or
the TypeSafe API being unreachable/unconfigured) returns None, and the
caller stores NULL in both columns. A NULL `schedule_type` means "use the
legacy regex parse of the raw `frequency` string" (see
`database._excluded_weekday`) — exactly today's behavior, so a classifier
outage never breaks habit creation or produces a wrong schedule.
"""

import logging

from langsmith import traceable

from .typesafe_client import get_typesafe_client

logger = logging.getLogger(__name__)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Below this, we don't trust the read enough to store structure over the
# legacy fallback — mirrors OFF_TOPIC_BLOCK_CONFIDENCE's role in agent.py:
# a wrong "confident" guess here would silently mis-schedule a habit, which
# costs the user real missed-nudge trust, so we'd rather fall back.
SCHEDULE_TYPE_CONFIDENCE = 0.6

# n_per_week ("3 times a week") is deliberately out of scope for now —
# is_satisfied only understands "any 'done' log in the window", not a
# target count, so storing it here would have nothing to act on yet.
_SCHEDULE_TYPE_CRITERIA = {
    "daily": "Every day, no exceptions mentioned",
    "daily_except_weekday": "Every day except one specific weekday is named",
    "weekly": "Once anywhere in a rolling 7-day window, no exceptions",
    "other": "Doesn't fit any of the above — too irregular to schedule automatically",
}


def _scrub_classify_frequency_inputs(inputs: dict) -> dict:
    """LangSmith records only the frequency phrase — the only argument
    this function takes, and nothing sensitive."""
    return {"frequency_text": inputs.get("frequency_text")}


@traceable(
    run_type="chain",
    name="schedule_classifier.classify_frequency",
    process_inputs=_scrub_classify_frequency_inputs,
)
def classify_frequency(frequency_text: str) -> dict | None:
    """Returns {"schedule_type", "excluded_weekday"} or None (see module
    docstring for what None means to the caller)."""
    try:
        from typesafe_sdk import Choice

        client = get_typesafe_client()
        result = client.system_one(
            {"frequency_text": frequency_text},
            {
                "schedule_type": Choice(
                    instructions=(
                        "What kind of recurrence does `frequency_text` describe for "
                        "this habit?"
                    ),
                    criteria=_SCHEDULE_TYPE_CRITERIA,
                ),
                "excluded_weekday": Choice(
                    instructions=(
                        "If `frequency_text` names a weekday this habit is skipped on, "
                        "which one? Answer none if no weekday exception is named."
                    ),
                    criteria={day: None for day in _WEEKDAYS} | {"none": "No weekday exception is named"},
                ),
            },
        )
    except Exception:
        logger.warning(
            "TypeSafe frequency classification failed — falling back to legacy regex parsing.",
            exc_info=True,
        )
        return None

    schedule = result.choices["schedule_type"]
    if schedule.confidence < SCHEDULE_TYPE_CONFIDENCE or schedule.choice == "other":
        return None

    excluded = result.choices["excluded_weekday"]
    excluded_weekday = excluded.choice if excluded.choice != "none" else None
    if schedule.choice != "daily_except_weekday":
        excluded_weekday = None

    return {"schedule_type": schedule.choice, "excluded_weekday": excluded_weekday}
