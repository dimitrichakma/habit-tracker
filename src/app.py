"""Streamlit chat frontend for the Habit Tracker. HTTP calls only — no agent logic here."""

import json
import os
import re

import plotly.graph_objects as go
import requests
import streamlit as st

# Palette roles (validated pairing, see the dataviz skill): single-series
# trend uses categorical slot 1 (blue); the heatmap uses the fixed
# good/critical status pair plus a neutral gray for "no data" — a status
# color never carries meaning alone, so it's always paired with the legend
# caption drawn under the chart, not color alone.
COLOR_TREND = "#2a78d6"
COLOR_DONE = "#0ca30c"
COLOR_MISSED = "#d03b3b"
COLOR_NEUTRAL = "#e1e0d9"
COLOR_GRIDLINE = "#e1e0d9"
COLOR_MUTED_TEXT = "#898781"

# Mirrored (duplicated, not imported) from src/auth.py's PASSWORD_REQUIREMENTS
# for live client-side feedback — app.py must stay HTTP-only with no backend
# imports, so keep these two lists in sync by hand if the rules ever change.
PASSWORD_REQUIREMENTS = [
    ("At least 8 characters", lambda p: len(p) >= 8),
    ("One uppercase letter (A-Z)", lambda p: re.search(r"[A-Z]", p) is not None),
    ("One lowercase letter (a-z)", lambda p: re.search(r"[a-z]", p) is not None),
    ("One number (0-9)", lambda p: re.search(r"[0-9]", p) is not None),
    ("One special character (e.g. ! @ # $ %)", lambda p: re.search(r"[^A-Za-z0-9]", p) is not None),
]

# Deployed frontend (Streamlit Cloud) sets BACKEND_BASE_URL to the public
# backend URL via app secrets; local dev falls back to the dev server.
BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000").rstrip("/")
SIGNUP_URL = f"{BACKEND_BASE_URL}/auth/signup"
LOGIN_URL = f"{BACKEND_BASE_URL}/auth/login"
CHAT_URL = f"{BACKEND_BASE_URL}/chat"
CHAT_STREAM_URL = f"{BACKEND_BASE_URL}/chat/stream"
TODAY_URL = f"{BACKEND_BASE_URL}/habits/today"
HISTORY_URL = f"{BACKEND_BASE_URL}/chat/history"
STATS_URL = f"{BACKEND_BASE_URL}/habits/stats"

st.set_page_config(page_title="Habit Tracker", page_icon="🏃")
st.title("Habit Tracker")

# Streamlit re-runs this whole script on every interaction, so session_state
# is the only place values survive between runs. Init both together — token
# is what gates the login screen below, username is only for display.
if "token" not in st.session_state:
    st.session_state.token = None
    st.session_state.username = None


def auth_headers() -> dict:
    """The header every authenticated backend call needs."""
    return {"Authorization": f"Bearer {st.session_state.token}"}


def authed_request(method: str, url: str, **kwargs):
    """Wraps requests.request with the auth header; drops back to the login
    screen on a 401 (expired/invalid token) instead of showing a raw error."""
    response = requests.request(method, url, headers=auth_headers(), **kwargs)
    if response.status_code == 401:
        st.session_state.clear()
        st.warning("Session expired — please log in again.")
        st.rerun()
    response.raise_for_status()
    return response


def stream_reply(prompt: str) -> str:
    """POST to /chat/stream and render the Server-Sent Events live: a status
    caption while the coach runs its tools, then the reply typed out token by
    token. Returns the final reply text so the caller can store it in the
    visible history. Mirrors authed_request's 401 -> login-screen behaviour."""
    status_ph = st.empty()
    reply_ph = st.empty()
    status_ph.caption("💭 Thinking…")
    acc = ""
    try:
        response = requests.post(
            CHAT_STREAM_URL,
            json={"message": prompt},
            headers=auth_headers(),
            stream=True,
            timeout=(10, 120),
        )
        if response.status_code == 401:
            st.session_state.clear()
            st.warning("Session expired — please log in again.")
            st.rerun()
        if response.status_code != 200:
            try:
                detail = response.json().get("detail", "the backend rejected the request")
            except ValueError:
                detail = f"the backend returned {response.status_code}"
            status_ph.empty()
            reply_ph.markdown(f"⚠️ {detail}")
            return f"⚠️ {detail}"

        for raw in response.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            event = json.loads(raw[6:])
            etype = event.get("type")
            if etype == "status":
                status_ph.caption(f"🔎 {event['text']}")
            elif etype == "token":
                status_ph.empty()
                acc += event["text"]
                reply_ph.markdown(acc + " ▌")
            elif etype == "replace":
                status_ph.empty()
                acc = event["text"]
                reply_ph.markdown(acc)
            elif etype == "error":
                acc += event["text"]
                reply_ph.markdown(acc)
            elif etype == "done":
                break
    except requests.RequestException as exc:
        acc = acc or f"Could not reach the habit tracker backend: {exc}"

    status_ph.empty()
    reply_ph.markdown(acc or "_(no reply)_")
    return acc or "_(no reply)_"


def render_progress_tab() -> None:
    """The "consistency over time" charts, backed by GET /habits/stats: a
    daily completion trend (line/area — the job is "trend over time") and
    a per-habit heatmap (the job is "compare across a grid"). The day-range
    radio is the interactive filter the charts respond to."""
    range_label = st.radio(
        "Time range", ["Last 7 days", "Last 30 days", "Last 90 days"], index=1, horizontal=True
    )
    days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}[range_label]

    try:
        stats = authed_request("GET", STATS_URL, params={"days": days}, timeout=10).json()
    except requests.RequestException as exc:
        st.error(f"Could not load progress data: {exc}")
        return

    trend = stats["trend"]
    habits = stats["habits"]

    if not any(entry["due"] > 0 for entry in trend):
        st.info("Add a daily habit and log a few days to see your consistency trend here.")
    else:
        this_week_pct = stats["this_week_rate"] * 100
        last_week_pct = stats["last_week_rate"] * 100

        col1, col2 = st.columns(2)
        col1.metric("Current streak", f"{stats['current_streak']} days")
        col2.metric(
            "This week's completion",
            f"{this_week_pct:.0f}%",
            delta=f"{this_week_pct - last_week_pct:+.0f}pp vs last week",
        )

        dates = [entry["date"] for entry in trend]
        # None (not 0%) on a day nothing was due, so the line gaps instead
        # of misleadingly dropping to the floor.
        rates = [(entry["completed"] / entry["due"] * 100) if entry["due"] > 0 else None for entry in trend]

        trend_fig = go.Figure(
            go.Scatter(
                x=dates,
                y=rates,
                mode="lines+markers",
                line=dict(color=COLOR_TREND, width=2),
                marker=dict(size=6),
                fill="tozeroy",
                fillcolor="rgba(42, 120, 214, 0.12)",
                connectgaps=False,
                hovertemplate="%{x}<br>%{y:.0f}% completed<extra></extra>",
            )
        )
        trend_fig.update_layout(
            title="Daily habit completion rate",
            yaxis=dict(title="% completed", range=[0, 100], gridcolor=COLOR_GRIDLINE, ticksuffix="%"),
            xaxis=dict(title=None, gridcolor=COLOR_GRIDLINE),
            plot_bgcolor="#fcfcfb",
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(color=COLOR_MUTED_TEXT),
            hovermode="x unified",
            margin=dict(l=10, r=10, t=40, b=10),
            height=320,
            showlegend=False,
        )
        st.plotly_chart(trend_fig, use_container_width=True)
        st.caption("Only counts daily-style habits — weekly habits show in the grid below instead.")

    if not habits:
        st.info("No habits yet — add one in the Chat tab to start tracking consistency.")
        return

    st.subheader("Per-habit consistency")
    all_dates = [entry["date"] for entry in trend]
    z_grid, hover_grid = [], []
    for habit in habits:
        logs_by_date = {log["date"]: log["status"] for log in habit["logs"]}
        z_row, hover_row = [], []
        for day in all_dates:
            status = logs_by_date.get(day)
            if status == "done":
                z_row.append(2.5)
                hover_row.append(f"{habit['name']}<br>{day}: done")
            elif status is not None:
                z_row.append(1.5)
                hover_row.append(f"{habit['name']}<br>{day}: {status}")
            else:
                z_row.append(0.5)
                hover_row.append(f"{habit['name']}<br>{day}: no log")
        z_grid.append(z_row)
        hover_grid.append(hover_row)

    # A discrete 3-band colorscale (not a continuous gradient) — each status
    # is a flat color, not a magnitude, so no colorbar is shown; the legend
    # caption below pairs color with label instead (color never carries
    # meaning alone). z values are the band midpoints (0.5/1.5/2.5) over a
    # 0-3 range, so each falls solidly inside its own band regardless of
    # any rounding at the exact band edges.
    band_colors = [COLOR_NEUTRAL, COLOR_MISSED, COLOR_DONE]
    colorscale = []
    for i, color in enumerate(band_colors):
        colorscale.append([i / 3, color])
        colorscale.append([(i + 1) / 3, color])

    heatmap_fig = go.Figure(
        go.Heatmap(
            z=z_grid,
            x=all_dates,
            y=[habit["name"] for habit in habits],
            text=hover_grid,
            hoverinfo="text",
            colorscale=colorscale,
            zmin=0,
            zmax=3,
            showscale=False,
            xgap=2,
            ygap=2,
        )
    )
    heatmap_fig.update_layout(
        xaxis=dict(title=None, showgrid=False),
        yaxis=dict(title=None, autorange="reversed"),
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color=COLOR_MUTED_TEXT),
        margin=dict(l=10, r=10, t=10, b=10),
        height=max(120, 40 * len(habits)),
    )
    st.plotly_chart(heatmap_fig, use_container_width=True)
    st.markdown(":green[● Done]&nbsp;&nbsp;:red[● Missed]&nbsp;&nbsp;:gray[● Not logged / not due]")


# --- Login / signup gate -------------------------------------------------
if st.session_state.token is None:
    st.subheader("Log in or sign up")
    mode = st.radio("Mode", ["Log in", "Sign up"], horizontal=True)
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    password_ok = True
    signup_code = ""
    if mode == "Sign up":
        signup_code = st.text_input(
            "Signup code",
            type="password",
            help="Required when the backend has SIGNUP_SECRET set. Leave blank for a local backend with open signup.",
        )
        st.caption("Password requirements:")
        password_ok = True
        for label, check in PASSWORD_REQUIREMENTS:
            met = check(password)
            password_ok = password_ok and met
            color = "green" if met else "red"
            symbol = "✓" if met else "✗"
            st.markdown(f":{color}[{symbol} {label}]")

    if st.button(mode, disabled=(mode == "Sign up" and not password_ok)):
        url = LOGIN_URL if mode == "Log in" else SIGNUP_URL
        headers = {"X-Signup-Secret": signup_code} if (mode == "Sign up" and signup_code) else None
        try:
            response = requests.post(
                url, json={"username": username, "password": password}, headers=headers, timeout=10
            )
            response.raise_for_status()
            st.session_state.token = response.json()["access_token"]
            st.session_state.username = username
            st.rerun()
        except requests.RequestException as exc:
            if exc.response is not None:
                detail = exc.response.json().get("detail", str(exc))
                if isinstance(detail, list):  # Pydantic field-validation errors
                    detail = "; ".join(item.get("msg", str(item)) for item in detail)
            else:
                detail = str(exc)
            st.error(f"Could not {mode.lower()}: {detail}")

    st.stop()  # don't render the chat UI until authenticated


# --- Authenticated app -----------------------------------------------------
# Runs once per login (not fetched again on every rerun, since "messages"
# stays in session_state after this) — restores the visible chat history
# so a page refresh doesn't look like the conversation was wiped. Falls
# back to an empty list rather than blocking the whole app if this fails.
if "messages" not in st.session_state:
    try:
        history = authed_request("GET", HISTORY_URL, timeout=10).json()
        st.session_state.messages = history["messages"]
    except requests.RequestException:
        st.session_state.messages = []

with st.sidebar:
    st.header("Session")
    st.caption(f"Logged in as **{st.session_state.username}**")
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()

    st.divider()
    st.header("Today's Dashboard")
    if st.button("Refresh"):
        st.rerun()

    try:
        dashboard = authed_request("GET", TODAY_URL, timeout=10).json()

        st.caption(dashboard["date"])

        st.subheader("✅ Done")
        if dashboard["done"]:
            for habit in dashboard["done"]:
                st.write(f"- {habit['name']} — {habit['status']}")
        else:
            st.caption("Nothing logged yet today.")

        st.subheader("⏳ Pending")
        if dashboard["pending"]:
            for habit in dashboard["pending"]:
                note = f" — {habit['status']}" if habit["status"] else ""
                st.write(f"- {habit['name']} ({habit['frequency']}){note}")
        else:
            st.caption("All caught up!")
    except requests.RequestException as exc:
        st.error(f"Could not load dashboard: {exc}")

chat_tab, progress_tab = st.tabs(["💬 Chat", "📊 Progress"])

with chat_tab:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if prompt := st.chat_input("Talk to your Habit Coach..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            reply = stream_reply(prompt)

        st.session_state.messages.append({"role": "assistant", "content": reply})
        # Re-run so the sidebar dashboard re-fetches with the state this message just changed.
        st.rerun()

with progress_tab:
    render_progress_tab()
