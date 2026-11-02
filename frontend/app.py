"""NexusFlow Autopilot operator console (Streamlit).

    ./frontend/run.sh            # mints the read-only demo token and serves on http://localhost:8502

Every visitor starts at the login gate (ui/login.py): "Explore the demo" signs in with the read-only
token run.sh puts in NEXUSFLOW_DEMO_TOKEN; reviewers paste their own JWT. The token is kept in this
browser session's server-side state and sent to the FastAPI backend (NEXUSFLOW_API_URL or BACKEND_URL,
default http://127.0.0.1:8010). No database access and no signing secrets live in the UI.

With no API connected, as on Streamlit Community Cloud, the console runs in labelled sample mode (ui/synthetic.py).
This script never starts a backend. Streamlit's default port applies unless run.sh sets one.
"""

from __future__ import annotations

import streamlit as st

from ui import login, synthetic, views
from ui.api import ApiError, NexusFlowClient
from ui.components import badge, esc, html_block, inject_theme, md, topbar
from views import ai_agents, n8n_workflows

st.set_page_config(page_title="NexusFlow Autopilot", page_icon="◆", layout="wide",
                   initial_sidebar_state="expanded")
inject_theme()

PAGES = {
    "Overview": "Workflows, executions, agents and the human review queue in one view.",
    "Operations": "Every workflow run, its pipeline, and why the agent decided what it did.",
    "n8n Workflows": "Each automation step by step, with measured counts, success rates and latency.",
    "AI Agents": "What each agent can do, the rules that bind it, and what it did recently.",
    "Revenue & SLA": "ARR, accounts and ticket SLA compliance from the live ledger.",
    "Approvals": "Seat expansions that need a person before anything is provisioned.",
    "Copilot": "Answers from SOPs, contracts and correspondence, with every quote checked.",
    "Diagnostics": "Live status of the API and everything it depends on.",
}


@st.cache_resource
def public_client() -> NexusFlowClient:
    """Unauthenticated client: only for /health, before and after sign-in."""
    return NexusFlowClient(token="")


@st.cache_data(ttl=15, show_spinner=False)
def cached_health(base_url: str) -> dict:
    try:
        return public_client().health()
    except ApiError as exc:
        return {"status": "down", "components": {}, "error": str(exc)}


@st.cache_data(ttl=15, show_spinner=False)
def cached_pending_count(token: str) -> int | None:
    try:
        return NexusFlowClient(token=token).pending_approvals()["count"]
    except ApiError:
        return None


health = cached_health(public_client().base_url)
sample_client = synthetic.SampleClient() if synthetic.should_use(health) else None
if sample_client:
    health = sample_client.health()

# ---------------------------------------------------------------- login gate
auth = st.session_state.get("auth")
if auth and auth["mode"] == "sample" and not sample_client:      # the API came back: leave the sample session
    login.sign_out()
    auth = None
if not auth:
    login.render(sample_client or public_client(), health)
    st.stop()

if sample_client:
    st.session_state["client"] = sample_client
elif "client" not in st.session_state or st.session_state["client"].sample:
    st.session_state["client"] = NexusFlowClient(token=auth["token"])
client = st.session_state["client"]
try:
    me = client.me()          # re-checked every run: an expired token sends you back to the gate
except ApiError as exc:
    if exc.code in ("TOKEN_EXPIRED", "INVALID_TOKEN", "AUTHENTICATION_REQUIRED"):
        login.sign_out()
        st.session_state["login_error"] = "Your session ended. Sign in again."
        st.rerun()
    me = auth["me"]

# ---------------------------------------------------------------- navigation (sidebar + top bar, kept in sync)
st.session_state.setdefault("page", "Overview")
st.session_state.setdefault("page_top", st.session_state["page"])


def _from_sidebar():
    st.session_state["page_top"] = st.session_state["page"]


def _from_top():
    if st.session_state.get("page_top"):          # a segmented control can be clicked off; keep the page
        st.session_state["page"] = st.session_state["page_top"]
    else:
        st.session_state["page_top"] = st.session_state["page"]


with st.sidebar:
    html_block('<div class="nf-brand"><div class="nf-brand-mark" aria-hidden="true"></div><div>'
               '<div class="nf-brand-name">NexusFlow</div><div class="nf-brand-sub">Autopilot operations</div></div></div>')
    pending = client.pending_approvals()["count"] if client.sample else cached_pending_count(client.token)
    labels = {p: (f"{p}  ({pending})" if p == "Approvals" and pending else p) for p in PAGES}
    st.radio("Navigate", list(PAGES), format_func=lambda p: labels[p], label_visibility="collapsed",
             key="page", on_change=_from_sidebar)
    meta = ("Read-only demo access" if auth["mode"] in ("demo", "sample") or client.sample
            else f'{me["subject"]}, {", ".join(r.title() for r in me["roles"])}')
    html_block(f'<div class="nf-identity"><div class="nf-identity-name">{esc(me.get("name") or me["subject"])}</div>'
               f'<div class="nf-identity-meta">{esc(meta)}</div></div>')
    if st.button("Sign out", key="sign_out", use_container_width=True):
        login.sign_out()
        st.rerun()

st.segmented_control("Views", list(PAGES), key="page_top", on_change=_from_top, label_visibility="collapsed")
choice = st.session_state["page"]

# ---------------------------------------------------------------- top bar
comps = health.get("components", {})
api_status = "ok" if health.get("status") == "ok" else ("down" if health.get("error") else health.get("status", "down"))
badges = badge("API", api_status, f"API {health.get('status', 'unreachable')}") + "".join(
    badge(name, comps.get(key, {}).get("status", "unknown"), f"{name}: {comps.get(key, {}).get('status', 'unknown')}")
    for key, name in (("database", "PostgreSQL"), ("pgvector", "pgvector"), ("n8n", "n8n"), ("gemini", "Gemini")))
topbar(choice, PAGES[choice], badges)
if client.sample:
    html_block('<div class="nf-sample-banner" role="status"><strong>Sample data.</strong> No API is connected to this '
               "deployment, so these figures are illustrative and decisions are off.</div>")

if health.get("error"):
    st.error(md(f"{health['error']}. Start it with: cd backend && .venv/bin/uvicorn app.main:app --port 8010"))
    st.stop()

if choice == "Overview":
    views.overview(client, me)
elif choice == "Operations":
    views.operations(client)
elif choice == "n8n Workflows":
    n8n_workflows.render(client)
elif choice == "AI Agents":
    ai_agents.render(client)
elif choice == "Revenue & SLA":
    views.revenue(client)
elif choice == "Approvals":
    views.approvals(client, me)
elif choice == "Copilot":
    views.copilot(client)
else:
    views.diagnostics(client, health, me)
