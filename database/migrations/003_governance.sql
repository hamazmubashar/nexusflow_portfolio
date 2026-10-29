-- Stage 4: Commercial Governance agent state + human-in-the-loop approval queue.
-- Idempotent; requires 001/002 and the Stage 1 relational schema.

-- One persisted agent run per seat request. `state` is the current node of the state
-- machine in app/services/agents/governance_agent.py; `history` is the append-only list of
-- transitions ({from, to, at, reason}) so a run can be audited and resumed after a restart.
CREATE TABLE IF NOT EXISTS governance_runs (
    run_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id   text        NOT NULL UNIQUE CHECK (request_id ~ '^SRQ-[A-Za-z0-9-]{4,40}$'),
    customer_id  varchar(9)  NOT NULL REFERENCES customers(customer_id),
    contract_id  varchar(8)  NOT NULL,
    state        text        NOT NULL CHECK (state IN (
                     'INITIATED', 'ANALYZING_CONTRACT', 'CALCULATING_IMPACT', 'WAITING_FOR_APPROVAL',
                     'APPROVED', 'REJECTED', 'EXECUTED', 'FAILED')),
    input        jsonb       NOT NULL,
    context      jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- contract analysis, impact, decision
    history      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    error        jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_governance_runs_state ON governance_runs (state, updated_at DESC);

-- HITL queue. The approval token is never stored: only its SHA-256, so a database read
-- can't be replayed as an approval. Decisions are single-use: the action endpoint flips
-- status with `WHERE status = 'PENDING' AND expires_at > now()`.
CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                uuid        NOT NULL UNIQUE REFERENCES governance_runs(run_id),
    request_id            text        NOT NULL UNIQUE REFERENCES seat_requests(request_id),
    customer_id           varchar(9)  NOT NULL REFERENCES customers(customer_id),
    contract_id           varchar(8)  NOT NULL,
    proposed_seats        integer     NOT NULL CHECK (proposed_seats > 0),
    plan_tier             text        NOT NULL,
    annual_unit_price     numeric(12,2) NOT NULL,
    monthly_cost_impact   numeric(14,6) NOT NULL,      -- this request: seats * price / 12
    aggregate_monthly_30d numeric(14,6) NOT NULL,      -- threshold basis (SOP-02 §2)
    annual_arr_impact     numeric(14,2) NOT NULL,      -- seats * price
    reasons               jsonb       NOT NULL,        -- why a human is required
    status                text        NOT NULL DEFAULT 'PENDING'
                              CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
    assigned_approver_id  varchar(8)  NOT NULL REFERENCES employees(employee_id),
    token_hash            char(64)    NOT NULL UNIQUE,
    expires_at            timestamptz NOT NULL,
    card                  jsonb       NOT NULL,        -- interactive approval card sent to n8n
    decided_at            timestamptz,
    decided_by            varchar(8)  REFERENCES employees(employee_id),
    decision_comment      text,
    approver_signature    text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (contract_id, customer_id) REFERENCES contracts(contract_id, customer_id),
    CHECK ((status IN ('APPROVED', 'REJECTED')) = (decided_at IS NOT NULL)),
    CHECK ((status IN ('APPROVED', 'REJECTED')) = (decided_by IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_approval_requests_pending ON approval_requests (expires_at) WHERE status = 'PENDING';
