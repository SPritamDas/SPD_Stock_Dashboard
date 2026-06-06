"""
auth.py — access control for the dashboard.

Google sign-in (Streamlit native st.login / OIDC) + an email allow-list +
a session timeout. Call auth.require_login() once, right after st.set_page_config.

Allow-list: hard-coded below for now (easy to read/edit). To switch to a
Google-Sheet-driven list later, replace ALLOWED_EMAILS / is_allowed() with a
sheet read — nothing else changes.

Local testing: setting  [app] dev_no_auth = true  in .streamlit/secrets.toml
bypasses Google sign-in so you can run the dashboard locally WITHOUT setting up
an OAuth client. It is OFF by default and must never be set in production.
"""
import os
import time
import streamlit as st

# ── ALLOW-LIST ───────────────────────────────────────────────────────────────
# Only these Google accounts may use the app. Edit here for now; move to a Sheet later.
ALLOWED_EMAILS = {
    "shuvendu.pritam@wakefit.co",
    "shuvendupritamdas@gmail.com",
    "shuvendupritamdas181@gmail.com",
}


def is_allowed(email: str) -> bool:
    return (email or "").strip().lower() in {e.lower() for e in ALLOWED_EMAILS}


# ── small helpers ────────────────────────────────────────────────────────────
def _app_cfg(key, default):
    try:
        return st.secrets.get("app", {}).get(key, default)
    except Exception:
        return default


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
    st.subheader("This is a private app — please sign in.")
    st.write("Access is limited to approved Google accounts.")
    st.button("Sign in with Google", type="primary", on_click=st.login)
    st.caption("If you should have access but can't get in, ask the owner to add your email.")


def _deny_screen(email: str):
    st.title("🚫 Access not authorized")
    st.write(f"**{email}** is not on the allow-list for this app.")
    st.write("Ask the owner to add your email, then sign in again.")
    st.button("Log out", on_click=st.logout)


def require_login() -> str:
    """Gate the whole app. Returns the signed-in email (or 'dev@localhost' in dev bypass).
    Renders a login/deny screen and st.stop()s when access is not granted."""

    # 1) LOCAL DEV BYPASS — must come first so we never touch st.user when auth isn't configured.
    if _dev_bypass():
        with st.sidebar:
            st.warning("🔓 DEV MODE — auth bypassed (local only)")
        return "dev@localhost"

    # 2) Require a Google login. st.user.is_logged_in errors if [auth] isn't configured,
    #    so guide the operator instead of crashing.
    try:
        logged_in = bool(st.user.is_logged_in)
    except Exception:
        st.title("⚙️ Authentication not configured")
        st.write("Google sign-in (`[auth]`) is not set up in this app's secrets.")
        st.write("For local testing without OAuth, set `[app] dev_no_auth = true` in "
                 "`.streamlit/secrets.toml`. For deployment, fill the `[auth]` block "
                 "(see `secrets.toml.example`).")
        st.stop()

    if not logged_in:
        _login_screen()
        st.stop()

    email = (getattr(st.user, "email", "") or "").strip().lower()

    # 3) ALLOW-LIST check.
    if not is_allowed(email):
        _deny_screen(email)
        st.stop()

    # 4) SESSION TIMEOUT. Streamlit's identity cookie lasts 30 days and is not idle-aware,
    #    so enforce our own idle + absolute timeout on top, then a manual st.logout().
    now = time.time()
    timeout = _timeout_seconds()
    started = st.session_state.get("_auth_started")
    last    = st.session_state.get("_auth_last", now)
    if started is None:
        st.session_state["_auth_started"] = now
        started = now
    if (now - last) > timeout or (now - started) > timeout:
        st.session_state.pop("_auth_started", None)
        st.session_state.pop("_auth_last", None)
        st.logout()
        st.stop()
    st.session_state["_auth_last"] = now

    # 5) Identity + logout control in the sidebar.
    with st.sidebar:
        st.caption(f"👤 {email}")
        st.button("Log out", on_click=st.logout, use_container_width=True)

    return email
