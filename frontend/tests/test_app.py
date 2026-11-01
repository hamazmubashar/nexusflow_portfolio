"""Operator console tests.

  * client tests: NexusFlowClient against httpx.MockTransport (no network)
  * app tests: the real Streamlit script via AppTest, talking to the live API on :8010 with a
    JWT minted by the backend CLI (skipped when the API isn't up)
"""

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from ui.api import ApiError, NexusFlowClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "backend"
API = os.environ.get("NEXUSFLOW_API_URL", "http://127.0.0.1:8010")


# --------------------------------------------------------------------------- client (offline)


def mock_client(handler) -> NexusFlowClient:
    return NexusFlowClient(base_url="http://api.test", token="t0k", transport=httpx.MockTransport(handler))


def test_problem_details_become_api_errors():
    def handler(request):
        return httpx.Response(409, headers={"content-type": "application/problem+json"},
                              json={"type": "x", "title": "Conflict", "status": 409, "code": "ALREADY_DECIDED",
                                    "detail": "approval is already APPROVED", "instance": "/x"})

    with pytest.raises(ApiError) as err:
        mock_client(handler).pending_approvals()
    assert (err.value.status, err.value.code) == (409, "ALREADY_DECIDED")
    assert "already APPROVED" in str(err.value)


def test_bearer_token_sent_except_on_health():
    seen = {}

    def handler(request):
        seen[request.url.path] = request.headers.get("authorization")
        return httpx.Response(503 if request.url.path == "/health" else 200, json={"status": "down"})

    c = mock_client(handler)
    assert c.health() == {"status": "down"}  # 503 health is data, not an error
    c.me()
    assert seen == {"/health": None, "/api/v1/auth/me": "Bearer t0k"}


def test_approve_or_reject_mints_session_token_then_uses_action_route():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.content and json.loads(request.content)))
        if request.url.path.endswith("/decision-token"):
            return httpx.Response(200, json={"token": "v1.abc.def", "approval_id": "a1", "expires_at": "",
                                             "method": "POST", "action_url": ""})
        return httpx.Response(200, json={"state": "REJECTED", "request_id": "SRQ-1"})

    run = mock_client(handler).approve_or_reject("a1", "REJECT", "EMP-0082", "no budget", "Parker Kim")
    assert run["state"] == "REJECTED"
    assert calls[0][:2] == ("POST", "/api/v1/approvals/a1/decision-token")
    assert calls[1] == ("POST", "/api/v1/approvals/v1.abc.def/action",
                        {"action": "REJECT", "approver_id": "EMP-0082", "comment": "no budget",
                         "approver_signature": "Parker Kim"})


def test_unreachable_api_is_explained():
    c = NexusFlowClient(base_url="http://127.0.0.1:9", token="x", timeout=1)
    with pytest.raises(ApiError) as err:
        c.me()
    assert err.value.code == "API_UNREACHABLE" and "uvicorn" in err.value.detail


# --------------------------------------------------------------------------- app (live API)


def _api_up() -> bool:
    try:
        return httpx.get(f"{API}/health", timeout=2).status_code in (200, 503)
    except httpx.HTTPError:
        return False


live_api = pytest.mark.skipif(not _api_up(), reason=f"API not reachable at {API}")


def _mint(*args) -> str:
    out = subprocess.run([str(BACKEND / ".venv/bin/python"), "-m", "app.core.security", *args],
                         cwd=BACKEND, capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture(scope="module")
def token():
    """Parker Kim's MANAGER token, as a reviewer would paste it."""
    return _mint("issue-token", "EMP-0082", "MANAGER")


@pytest.fixture(scope="module")
def demo_token():
    return _mint("issue-demo-token")


def gate(monkeypatch, url: str = API, demo: str | None = "") -> AppTest:
    """Fresh app at the login gate (nothing signed in)."""
    monkeypatch.setenv("NEXUSFLOW_API_URL", url)
    monkeypatch.delenv("NEXUSFLOW_API_TOKEN", raising=False)
    if demo is None:
        monkeypatch.delenv("NEXUSFLOW_DEMO_TOKEN", raising=False)
    else:
        monkeypatch.setenv("NEXUSFLOW_DEMO_TOKEN", demo)
    st.cache_resource.clear()
    st.cache_data.clear()
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()


def sign_in(at: AppTest, token: str) -> AppTest:
    at.text_input(key="reviewer_token").set_value(token)
    next(b for b in at.button if b.label == "Sign in").click().run()
    return at


def app(monkeypatch, token: str | None, url: str = API) -> AppTest:
    """The console signed in with `token` through the real login gate."""
    at = gate(monkeypatch, url)
    return sign_in(at, token) if token else at


PAGES = ["Overview", "Operations", "Revenue & SLA", "Approvals", "Copilot", "Diagnostics"]


def group(at: AppTest, key: str):
    """A segmented control by key (the page switcher and the time window are both button groups)."""
    return next(g for g in at.get("button_group") if g.key == key)


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def page_text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


@live_api
def test_overview_control_panel_renders_every_widget(monkeypatch, token):
    at = app(monkeypatch, token)
    assert not at.exception, at.exception
    text = page_text(at)
    for label in ("Active workflows", "Total executions", "Success rate", "Time saved", "Active AI agents",
                  "Workflow execution overview", "Recent activity", "Top performing workflows",
                  "Human review queue", "AI agent status"):
        assert label in text, label
    assert 'class="nf-est"' in text            # time saved is labelled as an estimate
    assert len(at.get("plotly_chart")) == 2    # execution chart + agent donut
    assert "Parker Kim" in text and 'class="nf-badge"' in text


@live_api
def test_window_switch(monkeypatch, token):
    at = app(monkeypatch, token)
    group(at, "ops_window").set_value("7d").run()     # the 24h / 7d / 30d segmented control
    assert not at.exception, at.exception
    assert "compared with the 7d before it" in page_text(at)
    assert "6 h buckets" in page_text(at)


@live_api
def test_revenue_page_keeps_business_kpis(monkeypatch, token):
    at = app(monkeypatch, token)
    at.radio(key="page").set_value("Revenue & SLA").run()
    assert not at.exception, at.exception
    text = page_text(at)
    assert "$12.4M" in text and ">120<" in text and "SLA compliance" in text


@live_api
def test_every_page_renders(monkeypatch, token):
    at = app(monkeypatch, token)
    for page, expect in (("Operations", ("Active workflow queue",)), ("Revenue & SLA", ("Total ARR",)),
                         ("Approvals", ("No approvals waiting", "Seat expansion approval")),
                         ("Copilot", ("Ask copilot",)), ("Diagnostics", ("Healthy", "Degraded", "Down"))):
        at.radio(key="page").set_value(page).run()
        assert not at.exception, (page, at.exception)
        haystack = page_text(at) + " ".join(b.label for b in at.button)
        assert any(e in haystack for e in expect), page


@live_api
def test_copilot_sources_mode_round_trip(monkeypatch, token):
    at = app(monkeypatch, token)
    at.radio(key="page").set_value("Copilot").run()
    at.text_area(key="copilot_q").set_value("When does a P1 ticket escalate to the VP of Engineering?")
    at.radio(key="copilot_mode").set_value("Show sources only")
    button(at, "Ask copilot").click().run()
    assert not at.exception, at.exception
    assert "passages, ranked by combined semantic and keyword match" in page_text(at)
    assert "SOP-01" in page_text(at)


# --------------------------------------------------------------------------- login gate


@live_api
def test_gate_is_shown_before_anything_else(monkeypatch):
    at = gate(monkeypatch, demo="x")
    assert not at.exception, at.exception
    text = page_text(at)
    for s in ("NexusFlow Autopilot", "AI Workflows", "Human Control", "Capture requests", "Check with AI",
              "Human review", "Turn every request into a governed decision.", "Try the live demo",
              "Human approval required", "No messages sent automatically", "Designed &amp; developed by Hamaz Mubashar"):
        assert s in text, s
    assert [b.label for b in at.button] == ["Explore the demo", "Sign in"]
    assert at.text_input(key="reviewer_token").proto.type == 1              # PASSWORD: masked input
    assert not at.radio and not at.get("button_group")           # no navigation before sign-in


@live_api
def test_bad_token_keeps_the_gate_closed(monkeypatch):
    at = sign_in(gate(monkeypatch), "not-a-real-token")
    assert "That token isn't valid for this API." in [e.value for e in at.error]
    assert not at.radio


@live_api
def test_demo_entry_unlocks_every_tab_read_only(monkeypatch, demo_token):
    at = gate(monkeypatch, demo=demo_token)
    next(b for b in at.button if b.label == "Explore the demo").click().run()
    assert not at.exception, at.exception
    assert [o.split("  (")[0] for o in at.radio(key="page").options] == PAGES   # "(3)" = pending count
    assert "Read-only demo access" in page_text(at)
    for page in PAGES:
        at.radio(key="page").set_value(page).run()
        assert not at.exception, (page, at.exception)
    at.radio(key="page").set_value("Approvals").run()
    assert not any(b.label == "Approve" for b in at.button)      # the demo can look, not decide


@live_api
def test_reviewer_token_unlocks_decisions(monkeypatch, token):
    at = app(monkeypatch, token)
    assert "EMP-0082, Manager" in page_text(at)
    at.radio(key="page").set_value("Approvals").run()
    pending = "No approvals waiting" not in page_text(at)
    assert any(b.label == "Approve" for b in at.button) == pending


@live_api
def test_top_bar_and_sidebar_stay_in_sync(monkeypatch, token):
    at = app(monkeypatch, token)
    group(at, "page_top").set_value("Diagnostics").run()
    assert at.radio(key="page").value == "Diagnostics" and "Healthy" in page_text(at)
    at.radio(key="page").set_value("Copilot").run()
    assert group(at, "page_top").value == "Copilot"


@live_api
def test_sign_out_returns_to_the_gate(monkeypatch, token):
    at = app(monkeypatch, token)
    next(b for b in at.button if b.label == "Sign out").click().run()
    assert not at.radio and "Try the live demo" in page_text(at)


@live_api
def test_invalid_session_falls_back_to_the_gate(monkeypatch):
    at = gate(monkeypatch)
    at.session_state["auth"] = {"token": "expired-or-revoked", "me": {"subject": "EMP-0082", "roles": []}, "mode": "token"}
    at.run()
    assert not at.radio and "Your session ended. Sign in again." in [e.value for e in at.error]


@live_api
def test_demo_button_disabled_without_demo_token(monkeypatch):
    at = gate(monkeypatch, demo=None)
    assert next(b for b in at.button if b.label == "Explore the demo").disabled
    assert "start the console with frontend/run.sh" in page_text(at)


def test_api_down_is_explained(monkeypatch):
    at = gate(monkeypatch, url="http://127.0.0.1:9")
    assert "API offline" in page_text(at)
    assert any("isn't answering" in w.value for w in at.warning)


@live_api
def test_operations_page_board_and_run_inspector(monkeypatch, token):
    at = app(monkeypatch, token)
    at.radio(key="page").set_value("Operations").run()
    assert not at.exception, at.exception
    text = page_text(at)
    assert "Active workflow queue" in text and "nf-board" in text
    if at.selectbox:   # governance runs exist: the inspector renders the tracker + decision for the chosen run
        assert "Workflow orchestration" in text and "AI decision and reasoning" in text and "Rules evaluated" in text
    else:              # empty database: the page says so instead of rendering empty panels
        assert "No governance runs yet." in text
