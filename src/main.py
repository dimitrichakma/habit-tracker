"""FastAPI service exposing the habit coaching agent over HTTP."""

from contextlib import asynccontextmanager
from datetime import date

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

load_dotenv()

from .agent import build_agent
from .auth import (
    create_access_token,
    get_current_user_id,
    hash_password,
    password_requirement_status,
    verify_password,
)
from .database import Habit, User, backfill_orphaned_habits, get_session, init_db, is_due_today, is_satisfied


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at server startup/shutdown (not per-request). Prepares the
    database and builds one shared agent instance that every request reuses
    for the life of the process — see build_agent()'s docstring for why."""
    init_db()
    async with build_agent() as agent:
        app.state.agent = agent
        yield


app = FastAPI(title="Habit Tracker", lifespan=lifespan)


class SignupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@app.post("/auth/signup", response_model=TokenResponse, status_code=201)
async def signup(request: SignupRequest) -> TokenResponse:
    """Create a new account and return a JWT for it.

    Password strength is checked before anything else touches the database —
    fail fast, and avoid a half-created-user edge case if it were checked later.
    """
    unmet = [label for label, met in password_requirement_status(request.password) if not met]
    if unmet:
        raise HTTPException(422, "Password must include: " + "; ".join(unmet) + ".")

    session = get_session()
    try:
        if session.query(User).filter(User.username == request.username).first() is not None:
            raise HTTPException(409, "That username is already taken.")
        user = User(username=request.username, hashed_password=hash_password(request.password))
        session.add(user)
        session.commit()
        session.refresh(user)  # populates user.id, needed below, before commit assigns it
        # One-time (per-signup, idempotent) migration: attach any pre-auth
        # habits with no owner to whichever account claims them first.
        backfill_orphaned_habits(session, user.id)
        return TokenResponse(access_token=create_access_token(user.id, user.username))
    finally:
        session.close()


@app.post("/auth/login", response_model=TokenResponse)
async def login(request: LoginRequest) -> TokenResponse:
    """Verify credentials and return a fresh JWT. No password-strength check
    here — those rules only apply when a password is being *set* (signup);
    an existing account must still be able to log in even if the rules
    change later, since its stored password won't retroactively update."""
    session = get_session()
    try:
        user = session.query(User).filter(User.username == request.username).first()
        if user is None or not verify_password(request.password, user.hashed_password):
            # Deliberately the same error for "no such user" and "wrong password" —
            # a distinct message would let an attacker enumerate valid usernames.
            raise HTTPException(401, "Incorrect username or password.")
        return TokenResponse(access_token=create_access_token(user.id, user.username))
    finally:
        session.close()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


class HabitStatus(BaseModel):
    id: int
    name: str
    frequency: str
    status: str | None = None


class TodayDashboard(BaseModel):
    date: str
    done: list[HabitStatus]
    pending: list[HabitStatus]


@app.get("/habits/today", response_model=TodayDashboard)
async def habits_today(user_id: int = Depends(get_current_user_id)) -> TodayDashboard:
    """The Today's Dashboard data the frontend sidebar shows. A plain
    database read, not an agent call — a deterministic "what's the status
    right now" question doesn't need an LLM in the loop, so this skips
    src/agent.py entirely and goes straight to the DB."""
    today = date.today()
    session = get_session()
    try:
        habits = session.query(Habit).filter(Habit.user_id == user_id).all()
        done, pending = [], []
        for habit in habits:
            if not is_due_today(habit, today):
                continue  # excluded today (e.g. "daily except Friday") — nothing to show

            today_log = next((entry for entry in habit.logs if entry.date == today), None)
            item = HabitStatus(
                id=habit.id,
                name=habit.name,
                frequency=habit.frequency,
                status=today_log.status if today_log else None,
            )
            (done if is_satisfied(habit, today) else pending).append(item)
        return TodayDashboard(date=today.isoformat(), done=done, pending=pending)
    finally:
        session.close()


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, user_id: int = Depends(get_current_user_id)) -> ChatResponse:
    """Send one message to the Habit Coach and get its reply.

    user_id comes only from the verified JWT (via Depends), never from the
    request body — this is what makes it impossible for a client to claim
    to be someone else's thread. It's passed as the LangGraph thread_id,
    which is also what src/tools.py's ToolRuntime reads to scope every tool
    call to the right user's data.
    """
    agent = app.state.agent
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": request.message}]},
        config={"configurable": {"thread_id": str(user_id)}},
    )
    # .content is a plain string for a simple reply, but a list of blocks
    # (thinking + text) once extended thinking kicks in on a longer reply.
    reply = _as_text(result["messages"][-1].content)
    return ChatResponse(reply=reply)


def _as_text(content: object) -> str:
    """Normalize a LangChain message's .content into plain text. It's a
    plain string for a short reply, but a list of content blocks (e.g. a
    thinking block + a text block) once Claude's extended thinking kicks
    in — pull out just the text blocks and ignore the rest (like thinking,
    which isn't meant for the end user to see)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class HistoryMessage(BaseModel):
    role: str
    content: str


class ChatHistory(BaseModel):
    messages: list[HistoryMessage]


@app.get("/chat/history", response_model=ChatHistory)
async def chat_history(user_id: int = Depends(get_current_user_id)) -> ChatHistory:
    """Replay this user's past conversation so the frontend can restore the
    visible chat after a page refresh (the browser holds no chat history of
    its own — src/app.py fetches this once on load). No {user_id} path
    param on purpose: that used to be editable by anyone, which was exactly
    the vulnerability real auth fixes — identity comes only from the JWT."""
    agent = app.state.agent
    snapshot = await agent.aget_state(config={"configurable": {"thread_id": str(user_id)}})
    raw_messages = snapshot.values.get("messages", []) if snapshot.values else []

    history = []
    for message in raw_messages:
        text = _as_text(message.content)
        if not text:
            continue  # skips tool-call-only messages (e.g. "call log_habit"), which have no user-facing text
        if isinstance(message, HumanMessage):
            history.append(HistoryMessage(role="user", content=text))
        elif isinstance(message, AIMessage):
            history.append(HistoryMessage(role="assistant", content=text))
        # ToolMessage (raw tool results) are deliberately not included —
        # they're internal plumbing, not part of the visible conversation.

    return ChatHistory(messages=history)
