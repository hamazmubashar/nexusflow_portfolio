"""NexusFlow Autopilot operator console (Streamlit).

    ./frontend/run.sh            # issues a MANAGER JWT for EMP-0082 and serves on http://localhost:8502

Talks only to the FastAPI backend (NEXUSFLOW_API_URL, default http://127.0.0.1:8010) with the
JWT in NEXUSFLOW_API_TOKEN. No database access and no secrets live in the UI.
"""

from __future__ import annotations

import streamlit as st

from ui.api import ApiError, NexusFlowClient
from ui.components import badge, esc, html_block, inject_theme, md, topbar
from ui import views

st.set_page_config(page_title="NexusFlow Autopilot", page_icon="◆", layout="wide",
                   initial_sidebar_state="expanded")
inject_theme()

PAGES = {
    "Overview": "Revenue, accounts, SLAs and what the automation is doing right now.",
    "Approvals": "Seat expansions that need a person before anything is provisioned.",
    "Copilot": "Answers from SOPs, contracts and correspondence, with every quote checked.",
    "Diagnostics": "Live status of the API and everything it depends on.",
}


@st.cache_resource
def get_client() -> NexusFlowClient:
    return NexusFlowClient()


@st.cache_data(ttl=15, show_spinner=False)
def cached_health(base_url: str) -> dict:
    try:
        return get_client().health()
    except ApiError as exc:
        return {"status": "down", "components": {}, "error": str(exc)}


@st.cache_data(ttl=15, show_spinner=False)
def cached_pending_count(token: str) -> int | None:
    try:
        return get_client().pending_approvals()["count"]
    except ApiError:
        return None


client = get_client()
health = cached_health(client.base_url)

try:
    me = client.me()
except ApiError as exc:
    me = {}
    auth_error = exc
else:
    auth_error = None

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    html_block('<div class="nf-brand"><div class="nf-brand-mark" aria-hidden="true"></div><div>'
               '<div class="nf-brand-name">NexusFlow</div><div class="nf-brand-sub">Autopilot operations</div></div></div>')
    pending = cached_pending_count(client.token) if me else None
    labels = {p: (f"{p}  ({pending})" if p == "Approvals" and pending else p) for p in PAGES}
    choice = st.radio("Navigate", list(PAGES), format_func=lambda p: labels[p], label_visibility="collapsed",
                      key="page")
    if me:
        html_block(f'<div class="nf-identity"><div class="nf-identity-name">{esc(me.get("name") or me["subject"])}</div>'
                   f'<div class="nf-identity-meta">{esc(me["subject"])}, {esc(" and ".join(r.title() for r in me["roles"]))}</div></div>')

# ---------------------------------------------------------------- top bar
comps = health.get("components", {})
api_status = "ok" if health.get("status") == "ok" else ("down" if health.get("error") else health.get("status", "down"))
badges = badge("API", api_status, f"API {health.get('status', 'unreachable')}") + "".join(
    badge(name, comps.get(key, {}).get("status", "unknown"), f"{name}: {comps.get(key, {}).get('status', 'unknown')}")
    for key, name in (("database", "PostgreSQL"), ("pgvector", "pgvector"), ("n8n", "n8n"), ("gemini", "Gemini")))
topbar(choice, PAGES[choice], badges)

if health.get("error"):
    st.error(md(f"{health['error']}. Start it with: cd backend && .venv/bin/uvicorn app.main:app --port 8010"))
    st.stop()
if auth_error is not None:
    st.error(md(f"Signed out: {auth_error.detail} ({auth_error.code}). Start the console with frontend/run.sh, "
                "which issues a fresh access token."))
    st.stop()

if choice == "Overview":
    views.overview(client)
elif choice == "Approvals":
    views.approvals(client, me)
elif choice == "Copilot":
    views.copilot(client)
else:
    views.diagnostics(client, health, me)
