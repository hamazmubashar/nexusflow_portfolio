"""Login gate: split-pane welcome screen in front of the console.

Two ways in, both checked against the API (GET /api/v1/auth/me) before anything unlocks:
  * Explore the demo - the read-only OPERATOR token run.sh mints (NEXUSFLOW_DEMO_TOKEN): every view, no decisions
  * Reviewer token   - any user JWT, e.g. Parker Kim's MANAGER token from
                       `backend/.venv/bin/python -m app.core.security issue-token EMP-0082 MANAGER`
The token lives only in this browser session's server-side state; it is never displayed or logged.
"""

from __future__ import annotations

import os

import streamlit as st

from ui.api import ApiError, NexusFlowClient
from ui.components import esc, html_block, md

# Product copy. Structure follows the reference layout; wording describes what NexusFlow actually does.
SUBTITLE = "IT Governance • AI Workflows • Human Control"
HERO = "Turn every request into a governed decision."
HERO_SUB = ("Seat expansions, P1 tickets and overdue invoices flow through rules and a grounded AI copilot. "
            "Anything that moves money waits for a person.")
FLOW = [
    ("Capture requests", "Seat requests, tickets and invoices arrive as signed webhooks"),
    ("Check with AI", "Contract rules, the $500/month threshold and a RAG review of the SOW"),
    ("Human review", "A manager approves or rejects; nothing is provisioned before that"),
]
FOOTER_LEFT = "Human approval required • No messages sent automatically"
FOOTER_RIGHT = "Designed & developed by Hamaz Mubashar"


def _flow() -> str:
    nodes = []
    for i, (title, detail) in enumerate(FLOW):
        nodes.append(f'<div class="nf-flow-node" tabindex="0"><span class="nf-flow-index">{i + 1}</span>'
                     f'<span class="nf-flow-title">{esc(title)}</span>'
                     f'<span class="nf-flow-detail">{esc(detail)}</span></div>')
        if i < len(FLOW) - 1:
            nodes.append('<div class="nf-flow-edge" aria-hidden="true"><span class="nf-flow-pulse"></span></div>')
    return f'<div class="nf-flow" role="list" aria-label="How a request flows">{"".join(nodes)}</div>'


def _sign_in(client: NexusFlowClient, token: str, mode: str) -> str | None:
    """Validate the token against the API; on success store the session and rerun. Returns an error message."""
    if client.sample:
        st.session_state["auth"] = {"token": "", "me": client.me(), "mode": "sample"}
        st.rerun()
    candidate = NexusFlowClient(base_url=client.base_url, token=token)
    try:
        me = candidate.me()
    except ApiError as exc:
        return {
            "TOKEN_EXPIRED": "That token has expired. Ask for a new one.",
            "INVALID_TOKEN": "That token isn't valid for this API.",
            "AUTHENTICATION_REQUIRED": "Paste a token first.",
            "API_UNREACHABLE": exc.detail,
        }.get(exc.code, f"Sign-in failed: {exc.detail} ({exc.code}).")
    if me.get("kind") != "user":
        return "Use a person's token, not a service credential."
    st.session_state["auth"] = {"token": token, "me": me, "mode": mode}
    st.session_state.pop("reviewer_token", None)
    st.rerun()
    return None


def render(client, health: dict) -> None:
    html_block("<style>[data-testid='stSidebar'], [data-testid='stExpandSidebarButton'] { display: none !important; }"
               ".block-container { max-width: 1100px; padding: 4rem 1.5rem !important; }"
               "</style>")
    hero_col, card_col = st.columns(2, gap="large", vertical_alignment="top")   # equal 50/50 split
    with hero_col, st.container(key="login_hero"):
        html_block(f'<div class="nf-login-brand"><div class="nf-brand-mark" aria-hidden="true"></div>'
                   f'<div><div class="nf-login-name">NexusFlow Autopilot</div>'
                   f'<div class="nf-login-sub">{esc(SUBTITLE)}</div></div></div>'
                   + _flow()
                   + f'<h1 class="nf-login-hero">{esc(HERO)}</h1><p class="nf-login-hero-sub">{esc(HERO_SUB)}</p>')
    with hero_col:
        html_block(f'<div class="nf-login-foot">{esc(FOOTER_LEFT)}</div>')
    with card_col, st.container(key="login_card"):
        api_ok = client.sample or health.get("status") in ("ok", "degraded")
        tone = "idle" if client.sample else "ok" if health.get("status") == "ok" else "wait" if api_ok else "fail"
        pill = "Sample data" if client.sample else "API online" if api_ok else "API offline"
        title = "Try the demo" if client.sample else "Try the live demo"
        copy = ("Browse workflows, approvals and the copilot on sample data. No API is connected here, so the "
                "figures are illustrative and decisions are off." if client.sample else
                "Browse live workflows, approvals and the copilot on real data. "
                "The demo is read-only: decisions stay with the assigned manager.")
        html_block(f'<div class="nf-login-card-head"><div class="nf-login-card-title">{title}</div>'
                   f'<span class="nf-pill {tone}"><span class="nf-dot {tone}"></span>{pill}</span></div>'
                   f'<p class="nf-login-copy">{copy}</p>')
        demo_token = os.environ.get("NEXUSFLOW_DEMO_TOKEN", "")
        error_key = "login_error"
        if st.button("Explore the demo", key="login_demo", type="primary", use_container_width=True,
                     disabled=not (client.sample or (api_ok and demo_token))):
            st.session_state[error_key] = _sign_in(client, demo_token, "demo")
        if not (demo_token or client.sample):
            html_block('<p class="nf-login-hint">Demo entry is off: start the console with frontend/run.sh.</p>')

        if client.sample:          # nothing to verify a token against, so none is ever accepted
            return _footer(card_col)
        html_block('<div class="nf-login-divider"><span>or sign in as a reviewer</span></div>')
        with st.form("login_token_form", border=False):
            token = st.text_input("Reviewer token", key="reviewer_token", type="password",
                                  placeholder="Paste your access token", autocomplete="off")
            if st.form_submit_button("Sign in", use_container_width=True, disabled=not api_ok):
                st.session_state[error_key] = _sign_in(client, token.strip(), "token") if token.strip() \
                    else "Paste a token first."
        if st.session_state.get(error_key):
            st.error(md(st.session_state[error_key]))
        if not api_ok:
            st.warning(md(f"The API at {client.base_url} isn't answering. Start it with: "
                          "cd backend && .venv/bin/uvicorn app.main:app --port 8010"))
    _footer(card_col)


def _footer(column) -> None:
    with column:
        html_block(f'<div class="nf-login-foot">{esc(FOOTER_RIGHT)}</div>')


def sign_out() -> None:
    for key in ("auth", "login_error", "client"):
        st.session_state.pop(key, None)
    st.cache_data.clear()
