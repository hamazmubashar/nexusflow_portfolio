#!/usr/bin/env bash
# Start the operator console as Parker Kim (EMP-0082, MANAGER) against the API on :8010.
# The JWT is minted by the backend CLI and handed to Streamlit via the environment: the UI never sees a secret.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
employee="${NEXUSFLOW_UI_EMPLOYEE:-EMP-0082}"
roles="${NEXUSFLOW_UI_ROLES:-MANAGER}"
export NEXUSFLOW_API_URL="${NEXUSFLOW_API_URL:-http://127.0.0.1:8010}"
export NEXUSFLOW_API_TOKEN="$(cd "$here/../backend" && .venv/bin/python -m app.core.security issue-token "$employee" $roles 2>/dev/null)"
cd "$here"
exec .venv/bin/streamlit run app.py "$@"
