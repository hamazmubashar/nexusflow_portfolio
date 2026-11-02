"""The console's sample mode must keep matching the real API contract, or its pages would break unnoticed."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "frontend"))

from ui.synthetic import SampleClient  # noqa: E402

from app.schemas.analytics import (  # noqa: E402
    ActivityResponse, AgentsResponse, DashboardResponse, MetricsResponse, OperationsResponse, WorkflowBoard,
    WorkflowsResponse,
)


@pytest.fixture(scope="module")
def client():
    return SampleClient()


@pytest.mark.parametrize("window", ["24h", "7d", "30d"])
def test_windowed_responses_match_their_schemas(client, window):
    OperationsResponse(**client.operations(window))
    WorkflowBoard(**client.workflow_runs(window))
    WorkflowsResponse(**client.n8n_workflows(window))
    AgentsResponse(**client.ai_agents(window))


def test_other_responses_match_their_schemas(client):
    DashboardResponse(**client.dashboard())
    ActivityResponse(**client.activity())
    MetricsResponse(**client.metrics())
