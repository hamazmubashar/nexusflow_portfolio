"""Deployment binding and sample mode: the console must boot and render with no API at all."""

import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from ui import synthetic
from ui.api import ApiError

ROOT = Path(__file__).resolve().parents[1]
DOWN = {"status": "down", "components": {}, "error": "unreachable"}
PAGES = {"Overview": "Workflow orchestration", "Operations": "Active workflow queue", "n8n Workflows": "Seat Expansion Intake",
         "AI Agents": "Governance State Machine", "Revenue & SLA": "Total ARR", "Approvals": "Assigned to",
         "Copilot": "recorded answers", "Diagnostics": "Not set up"}


def test_config_leaves_port_and_address_to_the_host():
    server = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())["server"]
    assert "port" not in server and "address" not in server


def test_local_run_script_keeps_loopback_binding():
    script = (ROOT / "run.sh").read_text()
    assert '--server.port "${PORT:-8502}"' in script and "--server.address 127.0.0.1" in script


def test_console_never_starts_a_backend():
    for path in [ROOT / "app.py", *(ROOT / "ui").glob("*.py"), *(ROOT / "views").glob("*.py")]:
        assert not re.search(r"\bsubprocess\b|\bPopen\b|uvicorn\.run|os\.system", path.read_text()), path


@pytest.mark.parametrize("env, health, expected", [
    ({}, DOWN, True),                                                               # nothing configured
    ({"BACKEND_URL": "http://api.test"}, DOWN, False),                              # local outage stays visible
    ({"NEXUSFLOW_API_URL": "http://api.test"}, DOWN, False),
    ({"BACKEND_URL": "http://api.test", "NEXUSFLOW_ENV": "production"}, DOWN, True),
    ({"NEXUSFLOW_ENV": "production"}, {"status": "ok"}, False),                     # a healthy API always wins
    ({}, {"status": "degraded"}, False),
])
def test_when_sample_mode_turns_on(monkeypatch, env, health, expected):
    for name in ("BACKEND_URL", "NEXUSFLOW_API_URL", "NEXUSFLOW_ENV"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert synthetic.should_use(health) is expected


def test_backend_url_is_used_when_the_older_name_is_unset(monkeypatch):
    from ui.api import NexusFlowClient
    monkeypatch.delenv("NEXUSFLOW_API_URL", raising=False)
    monkeypatch.setenv("BACKEND_URL", "http://api.test/")
    assert NexusFlowClient(token="").base_url == "http://api.test"


def test_sample_data_holds_nothing_shaped_like_a_credential():
    assert not synthetic.CREDENTIAL.search(synthetic.DATA_FILE.read_text())


def test_timestamps_stay_recent_and_approvals_stay_open():
    now = datetime(2031, 5, 1, tzinfo=timezone.utc)
    client = synthetic.SampleClient(now)
    newest = max(datetime.fromisoformat(i["at"]) for i in client.activity()["items"])
    assert timedelta(0) < now - newest < timedelta(hours=1)
    assert all(datetime.fromisoformat(a["expires_at"]) > now for a in client.pending_approvals()["approvals"])


def test_overview_series_covers_every_window_and_is_stable():
    now = datetime(2031, 5, 1, tzinfo=timezone.utc)
    client = synthetic.SampleClient(now)
    for window in ("24h", "7d", "30d"):
        ops = client.operations(window)
        total = sum(p["successful"] + p["failed"] + p["human_review"] for p in ops["series"])
        assert total == ops["kpis"]["total_executions"]["value"] > 0
        assert ops == synthetic.SampleClient(now).operations(window)


@pytest.mark.parametrize("call", [lambda c: c.decision_token("a"), lambda c: c.decide("t", "APPROVE", "E"),
                                  lambda c: c.approve_or_reject("a", "APPROVE", "E"), lambda c: c.ask("Something new?"),
                                  lambda c: c.search("Something new?"), lambda c: c.run("missing")])
def test_writes_and_unrecorded_questions_raise_a_handled_error(call):
    with pytest.raises(ApiError):
        call(synthetic.SampleClient())


def test_recorded_examples_answer_offline():
    client = synthetic.SampleClient()
    for question in synthetic.QUESTIONS:
        assert client.ask(question)["answer"] and "results" in client.search(question)


@pytest.fixture
def cloud(monkeypatch) -> AppTest:
    """The app as Community Cloud sees it in production with an unreachable API."""
    monkeypatch.setenv("NEXUSFLOW_ENV", "production")
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9")
    for name in ("NEXUSFLOW_API_URL", "NEXUSFLOW_API_TOKEN", "NEXUSFLOW_DEMO_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    st.cache_resource.clear()
    st.cache_data.clear()
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()


def enter(at: AppTest) -> AppTest:
    next(b for b in at.button if b.label == "Explore the demo").click().run()
    assert not at.exception, at.exception
    return at


def text(at: AppTest) -> str:
    return "\n".join(m.value for m in [*at.markdown, *at.info, *at.warning, *at.error])


def test_gate_offers_sample_demo_and_takes_no_tokens(cloud):
    assert not cloud.exception, cloud.exception
    body = text(cloud)
    assert "Sample data" in body and "illustrative" in body and "API online" not in body
    assert [b.label for b in cloud.button] == ["Explore the demo"] and not cloud.text_input


def test_every_page_renders_with_a_sample_label_and_no_errors(cloud):
    at = enter(cloud)
    assert "Approvals  (3)" in at.radio(key="page").options
    for page, expected in PAGES.items():
        at.radio(key="page").set_value(page).run()
        assert not at.exception, (page, at.exception)
        body = text(at)
        assert expected in body, page
        assert "Sample data." in body and "No API is connected" in body, page
        assert not at.error, (page, [e.value for e in at.error])


def test_sample_visitor_cannot_decide(cloud):
    at = enter(cloud)
    at.radio(key="page").set_value("Approvals").run()
    assert not any(b.label in ("Approve", "Reject") for b in at.button)


def test_new_copilot_question_explains_itself_instead_of_failing(cloud):
    at = enter(cloud)
    at.radio(key="page").set_value("Copilot").run()
    at.text_area(key="copilot_q").set_value("A question nobody recorded")
    next(b for b in at.button if b.label == "Ask copilot").click().run()
    assert not at.exception, at.exception
    assert any("needs a connected API" in e.value for e in at.error)
