"""
auth.py — simple password gate for the dashboard.

Replaces Google OAuth (st.login), which kept looping on Streamlit Community
Cloud ("Missing provider for OAuth callback"). Instead the app asks for a
single shared password that you give to the people you want to let in.

Password source (priority order):
  1. st.secrets["app"]["password"]   (Streamlit Cloud Secrets + local secrets.toml)
  2. env var  APP_PASSWORD

Local testing: setting  [app] dev_no_auth = true  in .streamlit/secrets.toml
bypasses the gate entirely. It is OFF by default and is HARD-disabled on
Streamlit Cloud (apps run under /mount/...), so a stray secret can never open
the deployed app.

Call auth.require_login() once, right after st.set_page_config.
"""
import hmac
import os
import time
import streamlit as st


# ── small helpers ────────────────────────────────────────────────────────────
def _app_cfg(key, default):
    try:
        return st.secrets.get("app", {}).get(key, default)
    except Exception:
        return default


def _expected_password():
    """The password the user must type, from secrets or env. None if unset."""
    pw = _app_cfg("password", None)
    if not pw:
        pw = os.environ.get("APP_PASSWORD")
    return str(pw) if pw else None


def _dev_bypass() -> bool:
    """Local-only escape hatch (off unless [app] dev_no_auth=true). HARD-disabled on
    Streamlit Cloud (apps run under /mount/...), so a stray secret can never open the app."""
    if not bool(_app_cfg("dev_no_auth", False)):
        return False
    on_cloud = os.path.abspath(__file__).startswith("/mount/")
    return not on_cloud


def _timeout_seconds() -> int:
    try:
        return int(_app_cfg("session_timeout_min", 240)) * 60
    except Exception:
        return 240 * 60


def _login_screen():
    st.title("🔒 SPritamDas Stock Dashboard")
    st.subheader("This is a private app — please enter the password.")
    expected = _expected_password()
    with st.form("spd_login_form", clear_on_submit=False):
        pw = st.text_input("Password", type="password",
                           placeholder="Enter the access password")
        submitted = st.form_submit_button("Enter", type="primary")
    if submitted:
        if not expected:
            st.error("No password is configured. Add  [app] password = \"…\"  to the app's secrets.")
        elif hmac.compare_digest(pw, expected):     # constant-time compare
            st.session_state["_authed"] = True
            st.session_state["_authed_at"] = time.time()
            st.rerun()
        else:
            st.error("Incorrect password. Try again, or ask the owner for the password.")
    st.caption("Access is limited to people who have the password.")


def require_login() -> str:
    """Gate the whole app. Renders the password screen and st.stop()s until the
    correct password is entered. Returns a short identifier for the session."""

    # 1) LOCAL DEV BYPASS — never gate during local dev when explicitly enabled.
    if _dev_bypass():
        with st.sidebar:
            st.warning("🔓 DEV MODE — auth bypassed (local only)")
        return "dev@localhost"

    # 2) SESSION TIMEOUT — expire an old session so an unattended tab re-locks.
    if st.session_state.get("_authed"):
        now = time.time()
        started = st.session_state.get("_authed_at", now)
        if (now - started) > _timeout_seconds():
            st.session_state.pop("_authed", None)
            st.session_state.pop("_authed_at", None)

    # 3) GATE — show the password screen until they're in.
    if not st.session_state.get("_authed"):
        _login_screen()
        st.stop()

    # 4) Logout control in the sidebar.
    with st.sidebar:
        if st.button("Log out", use_container_width=True):
            st.session_state.pop("_authed", None)
            st.session_state.pop("_authed_at", None)
            st.rerun()

    return "user"
