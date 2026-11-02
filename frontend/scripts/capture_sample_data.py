"""Rebuild ui/sample_data.json, the dataset the console shows when no API is connected.

    cd frontend && NEXUSFLOW_API_TOKEN=<operator token> .venv/bin/python scripts/capture_sample_data.py

Reads the local API, which only holds the repo's synthetic seed data. Refuses to write if anything in the
output looks like a credential.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ui.api import ApiError, NexusFlowClient  # noqa: E402
from ui.synthetic import CREDENTIAL, QUESTIONS  # noqa: E402


def main() -> None:
    c = NexusFlowClient(timeout=180)
    runs = c.governance_runs(limit=30)["runs"]
    data = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "dashboard": c.dashboard(activity_limit=12),
        "activity": c.activity(limit=30),
        "metrics": c.metrics(),
        "operations": c.operations("7d"),
        "workflow_runs": c.workflow_runs("30d", limit=20),
        "governance_runs": runs,
        "runs": {r["run_id"]: c.run(r["run_id"]) for r in runs},
        "pending_approvals": c.pending_approvals(),
        "n8n_workflows": c.n8n_workflows("7d"),
        "ai_agents": c.ai_agents("30d"),
        "answers": {}, "searches": {},
    }
    for q in QUESTIONS:
        try:
            data["answers"][q] = c.ask(q)
            data["searches"][q] = c.search(q)
        except ApiError as exc:
            print(f"skipped {q!r}: {exc.code}")
    text = json.dumps(data, indent=1, sort_keys=True)
    found = CREDENTIAL.search(text)
    if found:
        raise SystemExit(f"refusing to write: credential-shaped text at offset {found.start()}")
    (ROOT / "ui" / "sample_data.json").write_text(text + "\n", encoding="utf-8")
    print(f"wrote {len(text) // 1024} KB; pending={data['pending_approvals']['count']}, runs={len(runs)}, "
          f"answers={len(data['answers'])}")


if __name__ == "__main__":
    main()
