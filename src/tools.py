"""LangChain tools the agent calls to read and write habit data.

Each tool opens its own DB session and executes queries directly against
SQLite via SQLAlchemy — no data is ever guessed by the agent.
"""

from datetime import date, timedelta

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from .database import Habit, HabitLog, get_session, is_due_today, is_satisfied, satisfaction_window_days
from .vector_store import get_habit_memory_store


def _current_user_id(runtime: ToolRuntime) -> int:
    """The authenticated user's id, taken from the LangGraph thread_id that
    main.py set from a verified JWT. NEVER accept a user/user_id argument
    from the LLM itself for this purpose — an LLM-suppliable argument is
    trivially spoofable via prompt injection ("pretend I'm user 1").

    Chat / Telegram / eval threads are a bare numeric id. The scheduled
    friction nudge runs on a throwaway thread `friction-<user_id>-<nonce>`
    (so it doesn't drag the whole conversation history) — pull the id out
    of that too, from a fixed position, never from anything the model said.
    """
    raw = str(runtime.config["configurable"]["thread_id"])
    if raw.isdigit():
        return int(raw)
    for part in raw.split("-"):
        if part.isdigit():
            return int(part)
    raise ValueError(f"cannot resolve a user id from thread_id {raw!r}")


def _resolve_log_date(log_date: str) -> date | None:
    """Resolve "today", "yesterday", or an ISO date string to a date."""
    if log_date == "yesterday":
        return date.today() - timedelta(days=1)
    if not log_date or log_date == "today":
        return date.today()
    try:
        return date.fromisoformat(log_date)
    except ValueError:
        return None


_WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_PATTERN_LOOKBACK_DAYS = 42  # six weeks — enough to see a weekday-level pattern


def _find_habit(session, habit_name: str, user_id: int) -> Habit | list[Habit] | None:
    """Look up a habit (scoped to user_id) by exact name, falling back to a
    case-insensitive substring match (either direction) if nothing matches
    exactly — so shorthand like "gym" can still resolve to "Gym in the
    Evening (except Friday)" instead of silently spawning a duplicate habit.

    Returns a single Habit on a clean match, a list of candidates when the
    fuzzy match is ambiguous (more than one), or None when nothing matches.
    """
    exact = session.query(Habit).filter(Habit.name == habit_name, Habit.user_id == user_id).first()
    if exact is not None:
        return exact

    needle = habit_name.strip().lower()
    if not needle:
        return None
    candidates = [
        habit for habit in session.query(Habit).filter(Habit.user_id == user_id).all()
        if needle in habit.name.lower() or habit.name.lower() in needle
    ]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        return candidates
    return None


@tool
def create_new_habit(name: str, frequency: str, *, runtime: ToolRuntime) -> str:
    """Create a new habit for the user to track.

    Args:
        name: The name of the habit, e.g. "Drink water" or "Read 10 pages".
        frequency: How often the habit should be done, e.g. "daily" or "weekly".
    """
    user_id = _current_user_id(runtime)
    session = get_session()
    try:
        existing = session.query(Habit).filter(Habit.name == name, Habit.user_id == user_id).first()
        if existing is not None:
            return f"A habit named '{name}' already exists."

        habit = Habit(name=name, frequency=frequency, user_id=user_id)
        session.add(habit)
        session.commit()
        return f"Created habit '{name}' with frequency '{frequency}'."
    finally:
        session.close()


@tool
def get_pending_habits(*, runtime: ToolRuntime) -> str:
    """List habits that have not yet been logged today."""
    user_id = _current_user_id(runtime)
    session = get_session()
    try:
        today = date.today()
        habits = session.query(Habit).filter(Habit.user_id == user_id).all()
        if not habits:
            return "No habits have been created yet."

        pending = [
            habit
            for habit in habits
            if is_due_today(habit, today) and not is_satisfied(habit, today)
        ]

        if not pending:
            return "All habits have already been logged today."

        lines = [f"- {habit.name} (id={habit.id}, frequency={habit.frequency})" for habit in pending]
        return "Pending habits for today:\n" + "\n".join(lines)
    finally:
        session.close()


@tool
def log_habit(habit_name: str, status: str, log_date: str = "today", *, runtime: ToolRuntime) -> str:
    """Record a habit's status for a given day.

    Args:
        habit_name: The exact name of the habit to log.
        status: The outcome, e.g. "done", "missed", or "skipped".
        log_date: Which day this applies to: "today" (default), "yesterday",
            or an explicit "YYYY-MM-DD" date. Use "yesterday" for a habit that
            spans midnight (e.g. a bedtime routine) when the user is logging
            it the next morning and means last night, not today.
    """
    target_date = _resolve_log_date(log_date)
    if target_date is None:
        return f"'{log_date}' isn't a date I understand. Use 'today', 'yesterday', or YYYY-MM-DD."

    user_id = _current_user_id(runtime)
    session = get_session()
    try:
        habit = _find_habit(session, habit_name, user_id)
        if habit is None:
            return f"No habit named '{habit_name}' exists. Create it first."
        if isinstance(habit, list):
            names = ", ".join(f"'{h.name}'" for h in habit)
            return f"'{habit_name}' matches more than one habit: {names}. Ask the user which one they mean."

        existing_log = session.query(HabitLog).filter(
            HabitLog.habit_id == habit.id, HabitLog.date == target_date
        ).first()

        if existing_log is not None:
            existing_log.status = status
        else:
            session.add(HabitLog(habit_id=habit.id, date=target_date, status=status))

        session.commit()
        return f"Logged '{habit.name}' as '{status}' for {target_date.isoformat()}."
    finally:
        session.close()


@tool
def list_habits(*, runtime: ToolRuntime) -> str:
    """List every habit that has been created, regardless of today's status.

    Call this before creating a new habit if the user's wording might refer
    to one that already exists under a slightly different name — logging
    against the existing habit is always preferred over creating a duplicate.
    """
    user_id = _current_user_id(runtime)
    session = get_session()
    try:
        habits = session.query(Habit).filter(Habit.user_id == user_id).all()
        if not habits:
            return "No habits have been created yet."
        lines = [f"- {habit.name} (id={habit.id}, frequency={habit.frequency})" for habit in habits]
        return "All habits:\n" + "\n".join(lines)
    finally:
        session.close()


@tool
def get_weekly_summary(*, runtime: ToolRuntime) -> str:
    """Summarize the last 7 days (including today), per habit. Use this
    when the user asks for a weekly review, e.g. "how was my week?",
    "give me a summary", "how am I doing overall".

    Daily-style habits are reported as "N/M days done", where M only counts
    days the habit was actually due (an "except Friday" habit isn't
    penalized for the Friday it was never supposed to happen). Weekly
    habits are reported as satisfied or not for the week as a whole,
    since "days done" doesn't make sense for something only due once a week.
    """
    user_id = _current_user_id(runtime)
    session = get_session()
    try:
        habits = session.query(Habit).filter(Habit.user_id == user_id).all()
        if not habits:
            return "No habits have been created yet."

        today = date.today()
        window = [today - timedelta(days=offset) for offset in range(6, -1, -1)]  # oldest -> newest

        lines = []
        for habit in habits:
            if satisfaction_window_days(habit.frequency) >= 7:
                status = "done" if is_satisfied(habit, today) else "not done"
                lines.append(f"- {habit.name}: {status} this week (weekly habit)")
                continue

            due_days = [day for day in window if is_due_today(habit, day)]
            done_count = sum(
                1 for log in habit.logs if log.status == "done" and log.date in due_days
            )
            lines.append(f"- {habit.name}: {done_count}/{len(due_days)} days done")

        return "Weekly summary (last 7 days):\n" + "\n".join(lines)
    finally:
        session.close()


def describe_habit_pattern(habit: Habit, today: date) -> str:
    """Plain-text six-week recurring-failure summary for one habit. Pure: no
    DB session opened, no user resolution — `habit.logs` must already be
    loadable (call inside an open session). Shared by the
    get_habit_history_pattern tool AND scheduler.run_friction_nudge, which
    pre-computes this so the evening nudge needs zero tool round trips."""
    window_start = today - timedelta(days=_PATTERN_LOOKBACK_DAYS - 1)
    done_dates = {
        log.date
        for log in habit.logs
        if log.status == "done" and window_start <= log.date <= today
    }

    # Weekly habits: "days of the week" don't apply — report satisfied-or-not
    # per trailing week instead, matching get_weekly_summary's treatment.
    if satisfaction_window_days(habit.frequency) >= 7:
        satisfied_weeks = sum(
            1
            for offset in range(6)
            if done_dates
            & {today - timedelta(days=7 * offset + d) for d in range(7)}
        )
        if satisfied_weeks >= 5:
            return (
                f"'{habit.name}' (weekly): satisfied in {satisfied_weeks} of the "
                "last 6 weeks — no recurring failure pattern."
            )
        return (
            f"'{habit.name}' (weekly): satisfied in only {satisfied_weeks} of the "
            "last 6 weeks — a recurring pattern of missing it."
        )

    due_days = [
        window_start + timedelta(days=i)
        for i in range(_PATTERN_LOOKBACK_DAYS)
        if is_due_today(habit, window_start + timedelta(days=i))
    ]
    if len(due_days) < 5:
        return (
            f"'{habit.name}' has only been due {len(due_days)} time(s) in the last "
            "six weeks — not enough history to call a pattern yet."
        )

    total_due = len(due_days)
    total_done = sum(1 for d in due_days if d in done_dates)
    overall_pct = round(100 * total_done / total_due)

    # Current run of consecutive missed due days, most recent first.
    miss_streak = 0
    for day in reversed(due_days):
        if day in done_dates:
            break
        miss_streak += 1

    # Per-weekday trouble spots: a weekday the habit was due on at least 3
    # times and missed on more than half of them.
    weekday_lines = []
    for weekday_index, weekday_name in enumerate(_WEEKDAY_LABELS):
        days = [d for d in due_days if d.weekday() == weekday_index]
        if len(days) < 3:
            continue
        missed = sum(1 for d in days if d not in done_dates)
        if missed > len(days) / 2:
            weekday_lines.append(f"{weekday_name}s: missed {missed} of the last {len(days)}")

    parts = [
        f"'{habit.name}' over the last six weeks: done {total_done}/{total_due} "
        f"due days ({overall_pct}%)."
    ]
    if miss_streak >= 2:
        parts.append(f"Currently missed {miss_streak} due days in a row.")
    if len(weekday_lines) >= 5:
        # Nearly every weekday is bad — that's an "almost stopped doing it"
        # pattern, not a weekday-specific one; don't enumerate all seven.
        parts.append("Missed on most due days across the whole week, not one particular weekday.")
    elif weekday_lines:
        parts.append("Weekday trouble spots — " + "; ".join(weekday_lines) + ".")
    if not weekday_lines and miss_streak < 2 and overall_pct >= 80:
        parts.append("No strong recurring failure pattern — looks like a one-off off day.")
    return " ".join(parts)


@tool
def get_habit_history_pattern(habit_name: str, *, runtime: ToolRuntime) -> str:
    """Analyze one habit's recent logged history for a *recurring* failure
    pattern (e.g. "missed 4 of the last 5 Mondays", "missed 3 due days in a
    row"). Read-only, over the last six weeks of HabitLog data — no data is
    guessed.

    Call this the moment the user reports missing a habit, before suggesting
    anything: a smaller "micro-commitment" version of the habit is only worth
    proposing when this shows a real repeating pattern, not a single off day.

    Args:
        habit_name: Name of the habit to analyze (fuzzy-matched like log_habit).
    """
    user_id = _current_user_id(runtime)
    session = get_session()
    try:
        habit = _find_habit(session, habit_name, user_id)
        if habit is None:
            return f"No habit named '{habit_name}' exists."
        if isinstance(habit, list):
            names = ", ".join(f"'{h.name}'" for h in habit)
            return (
                f"'{habit_name}' matches more than one habit: {names}. "
                "Ask the user which one they mean."
            )
        return describe_habit_pattern(habit, date.today())
    finally:
        session.close()


@tool
def query_past_behavior(topic: str, *, runtime: ToolRuntime) -> str:
    """Search the user's stored weekly behavioral summaries (their long-term
    memory) for context relevant to `topic`, returning up to 3 of the most
    similar.

    Call this BEFORE giving advice, BEFORE answering "why do I keep failing at
    X", and BEFORE reflecting on the user's longer-term patterns — it grounds
    the coaching in what actually happened over past weeks instead of guessing.

    Args:
        topic: A short description of the behaviour/theme to recall, e.g.
            "evening exercise consistency" or "why morning habits slip".
    """
    user_id = _current_user_id(runtime)
    # Generic LangChain VectorStore.similarity_search interface — never a
    # Chroma-specific call — so this survives the Phase 5 pgvector swap and
    # the eval suite can mock it at the vector-store level. Scoped to the
    # caller's own user_id via the metadata filter; user_id comes from
    # ToolRuntime, never from the LLM.
    documents = get_habit_memory_store().vectorstore.similarity_search(
        topic, k=3, filter={"user_id": user_id}
    )
    if not documents:
        return (
            "No past behavioral summaries are stored for this user yet (they "
            "accumulate one per week). Coach from this conversation and the live "
            "habit data instead."
        )
    lines = [f"{index}. {doc.page_content}" for index, doc in enumerate(documents, start=1)]
    return "Relevant past behavioral summaries (most similar first):\n" + "\n".join(lines)


@tool
def delete_habit(habit_name: str, *, runtime: ToolRuntime) -> str:
    """Permanently delete a habit and all of its logged history.

    Requires an exact name match (no fuzzy matching) since this is
    destructive — confirm the exact name with the user first if unsure.

    Args:
        habit_name: The exact name of the habit to delete.
    """
    user_id = _current_user_id(runtime)
    session = get_session()
    try:
        habit = session.query(Habit).filter(Habit.name == habit_name, Habit.user_id == user_id).first()
        if habit is None:
            return f"No habit named '{habit_name}' exists."
        session.delete(habit)
        session.commit()
        return f"Deleted habit '{habit_name}' and all of its logged history."
    finally:
        session.close()


TOOLS = [
    create_new_habit,
    get_pending_habits,
    log_habit,
    list_habits,
    get_weekly_summary,
    get_habit_history_pattern,
    query_past_behavior,
    delete_habit,
]
