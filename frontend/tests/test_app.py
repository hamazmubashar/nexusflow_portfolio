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


@pytest.fixture(scope="module")
def token():
    out = subprocess.run([str(BACKEND / ".venv/bin/python"), "-m", "app.core.security", "issue-token", "EMP-0082",
                          "MANAGER"], cwd=BACKEND, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def app(monkeypatch, token: str | None, url: str = API) -> AppTest:
    monkeypatch.setenv("NEXUSFLOW_API_URL", url)
    monkeypatch.setenv("NEXUSFLOW_API_TOKEN", token or "")
    st.cache_resource.clear()
    st.cache_data.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    return at.run()


def page_text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


@live_api
def test_overview_shows_live_kpis(monkeypatch, token):
    at = app(monkeypatch, token)
    assert not at.exception, at.exception
    text = page_text(at)
    for label in ("Total ARR", "Active accounts", "Pending approvals", "SLA compliance", "Active agent runs"):
        assert label in text
    assert "$12.4M" in text and ">120<" in text
    assert "Parker Kim" in text and 'class="nf-badge"' in text


@live_api
def test_every_page_renders(monkeypatch, token):
    at = app(monkeypatch, token)
    for page, expect in (("Approvals", ("No approvals waiting", "Seat expansion approval")),
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
    at.button[-1].click().run()  # the form's "Ask copilot"
    assert not at.exception, at.exception
    assert "passages, ranked by combined semantic and keyword match" in page_text(at)
    assert "SOP-01" in page_text(at)


@live_api
def test_missing_token_shows_sign_in_guidance(monkeypatch):
    at = app(monkeypatch, token=None)
    assert [e.value for e in at.error] and "AUTHENTICATION_REQUIRED" in at.error[0].value
    assert "run.sh" in at.error[0].value


def test_api_down_is_explained(monkeypatch):
    at = app(monkeypatch, token="x", url="http://127.0.0.1:9")
    assert "Can't reach the API" in at.error[0].value
