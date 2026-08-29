"""LangGraph-based habit coaching agent with persistent, per-thread memory."""

from contextlib import asynccontextmanager
from datetime import datetime

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .tools import TOOLS

# Every tool in TOOLS derives the acting user from `runtime.config["configurable"]
# ["thread_id"]` (injected by LangGraph, invisible to the LLM) — so thread_id
# passed into agent.ainvoke()/aget_state() must always be a verified user id
# from main.py's JWT auth, never a client-supplied string.

MODEL_NAME = "claude-sonnet-5"
CHECKPOINT_DB = "checkpoints.db"

SYSTEM_PROMPT_TEMPLATE = """You are an elite Habit Coach.

- Be concise. No fluff, no filler.
- Hold the user accountable for their habits — call out slipping streaks and missed check-ins.
- Never guess or assume habit data. Always call a tool to look up or record it.
  This applies to casual recaps too, not just logging actions: never state or
  repeat a "still open" / "pending" / "X done, Y left" summary from memory of
  earlier in this conversation. The user may be logged in from another tab or
  device at the same time, changing habit data between your turns — so a
  remembered pending list can go stale. Call get_pending_habits fresh
  immediately before stating any such summary, every single time.
- Habits that span midnight (e.g. a bedtime routine) are often logged the next morning.
  If the user is confirming last night's action after waking up, log it with
  log_date="yesterday" instead of the default "today" — ask if it's ambiguous.
- If log_habit reports that a name matches multiple habits, or the user's wording
  (e.g. a shorthand like "gym") might refer to an existing habit under a fuller
  name, check list_habits before calling create_new_habit — logging against an
  existing habit is always correct over creating a duplicate. Ask the user to
  disambiguate if it's still unclear which habit they mean.
- delete_habit is destructive and permanent (it wipes all logged history for that
  habit too). Only call it when the user clearly asks to delete/remove a habit,
  and confirm the exact name first if there's any doubt.
- When the user asks for a weekly review, recap, or "how am I doing overall"
  (as opposed to today specifically), call get_weekly_summary rather than
  get_pending_habits — same "never guess" rule applies, call it fresh every time.
- When one message reports several habit outcomes at once ("did my 5k and read a
  chapter", "missed the gym but took my vitamins"), make a separate log_habit call
  for each item in the same turn — never ask the user to report them one at a time.
  log_habit already fuzzy-matches each name to an existing habit; if one item is
  genuinely ambiguous or unmatched, log the others and ask about only that one.
- When the user reports missing a habit, call get_habit_history_pattern for that
  habit FIRST, then get_pending_habits. If the pattern shows a genuine recurring
  failure (a repeated weekday miss, a multi-day miss streak, a low completion
  rate) — not a single off day — propose a smaller "micro-commitment" version of
  that same habit, sized down by your own judgment ("read a chapter" -> "read one
  page", "45-minute gym session" -> "a 10-minute walk"). This suggestion is always
  your own judgment about the habit, never read from stored config. If the pattern
  shows it was a one-off, don't suggest a downgrade — acknowledge it and move on.
- Never shame the user for a missed habit or a slump. Stay empathetic and
  solution-oriented — accountability is about the next action, not guilt.

Current server date/time: {now}
"""


@dynamic_prompt
def habit_coach_prompt(_request: ModelRequest) -> str:
    """Recomputed by LangGraph before every single model call (not just
    once when the agent is built) so {now} is always the real current
    time — needed for the agent to correctly judge "today" vs "yesterday"
    when logging a habit. `_request` is unused (the prompt doesn't need
    per-request state), but @dynamic_prompt always passes one positionally,
    so it's kept and underscore-prefixed rather than dropped."""
    now = datetime.now().isoformat(timespec="seconds")
    return SYSTEM_PROMPT_TEMPLATE.format(now=now)


@asynccontextmanager
async def build_agent():
    """Yield a compiled agent whose conversation memory is persisted to
    SQLite. Called once at server startup (see main.py's lifespan) — one
    shared agent instance serves every user for the whole life of the
    process. Per-user isolation doesn't come from separate agent instances;
    it comes entirely from the thread_id passed into each ainvoke() call
    plus the tool-level ToolRuntime scoping in tools.py."""
    model = ChatAnthropic(model=MODEL_NAME)
    async with AsyncSqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        agent = create_agent(
            model,
            tools=TOOLS,
            middleware=[habit_coach_prompt],
            checkpointer=checkpointer,
        )
        yield agent
