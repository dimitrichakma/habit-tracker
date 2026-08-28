"""Streamlit chat frontend for the Habit Tracker. HTTP calls only — no agent logic here."""

import re

import requests
import streamlit as st

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

BACKEND_BASE_URL = "http://localhost:8000"
SIGNUP_URL = f"{BACKEND_BASE_URL}/auth/signup"
LOGIN_URL = f"{BACKEND_BASE_URL}/auth/login"
CHAT_URL = f"{BACKEND_BASE_URL}/chat"
TODAY_URL = f"{BACKEND_BASE_URL}/habits/today"
HISTORY_URL = f"{BACKEND_BASE_URL}/chat/history"

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


# --- Login / signup gate -------------------------------------------------
if st.session_state.token is None:
    st.subheader("Log in or sign up")
    mode = st.radio("Mode", ["Log in", "Sign up"], horizontal=True)
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    password_ok = True
    if mode == "Sign up":
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
        try:
            response = requests.post(url, json={"username": username, "password": password}, timeout=10)
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

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Talk to your Habit Coach..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            response = authed_request("POST", CHAT_URL, json={"message": prompt}, timeout=60)
            reply = response.json()["reply"]
        except requests.RequestException as exc:
            reply = f"Could not reach the habit tracker backend: {exc}"

        st.markdown(reply)

    st.session_state.messages.append({"role": "assistant", "content": reply})
    # Re-run so the sidebar dashboard re-fetches with the state this message just changed.
    st.rerun()
