#!/usr/bin/env bash
# Start the operator console against the API on :8010.
# Every visitor signs in at the login gate:
#   * "Explore the demo" uses a read-only OPERATOR token minted here (NEXUSFLOW_DEMO_TOKEN)
#   * reviewers paste their own token, e.g. Parker Kim (EMP-0082, MANAGER):
#       backend/.venv/bin/python -m app.core.security issue-token EMP-0082 MANAGER
# Tokens are minted by the backend CLI; the UI never sees a signing secret.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export NEXUSFLOW_API_URL="${NEXUSFLOW_API_URL:-http://127.0.0.1:8010}"
export NEXUSFLOW_DEMO_TOKEN="$(cd "$here/../backend" && .venv/bin/python -m app.core.security issue-demo-token 2>/dev/null)"
unset NEXUSFLOW_API_TOKEN
cd "$here"
exec .venv/bin/streamlit run app.py "$@"
