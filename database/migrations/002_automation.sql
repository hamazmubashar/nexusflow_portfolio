-- Stage 3: event audit log + seat-request ledger for the n8n integration.
-- Idempotent; applied on FastAPI startup and by the RAG ingest (all migrations run in order).
-- Requires the Stage 1 relational schema (customers, contracts).

-- Every webhook in or out (FastAPI <-> n8n) is recorded here. (event_id, direction) is
-- unique, so a replayed inbound event is detected and answered from the stored response.
CREATE TABLE IF NOT EXISTS automation_events (
    id           bigserial PRIMARY KEY,
    event_id     uuid        NOT NULL,
    event_type   text        NOT NULL,
    direction    text        NOT NULL CHECK (direction IN ('inbound', 'outbound', 'callback')),
    source       text        NOT NULL,
    subject_id   text,                       -- TK-/CUST-/INV-/SRQ- id the event is about
    status       text        NOT NULL CHECK (status IN (
                     'RECEIVED', 'PROCESSED', 'REJECTED', 'DELIVERED', 'MOCKED',
                     'QUEUED_OFFLINE', 'FAILED')),
    payload      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    response     jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, direction)
);
CREATE INDEX IF NOT EXISTS idx_events_subject ON automation_events (subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_status ON automation_events (event_type, status);

-- SOP-02 §2 needs a stable ledger of seat additions for the rolling 30-day aggregate;
-- the nine-table snapshot has none. Nothing here is ever provisioned automatically.
CREATE TABLE IF NOT EXISTS seat_requests (
    request_id                 text PRIMARY KEY CHECK (request_id ~ '^SRQ-[A-Za-z0-9-]{4,40}$'),
    customer_id                varchar(9)  NOT NULL REFERENCES customers(customer_id),
    contract_id                varchar(8)  NOT NULL,
    added_seats                integer     NOT NULL CHECK (added_seats > 0),
    request_type               text        NOT NULL CHECK (request_type IN
                                   ('PURCHASE', 'RESERVATION', 'TEMPORARY_ACCESS', 'UNSPECIFIED')),
    plan_tier                  text        NOT NULL,
    annual_unit_price          numeric(12,2) NOT NULL,
    monthly_equivalent         numeric(14,6) NOT NULL,      -- this request alone
    aggregate_seats_30d        integer     NOT NULL,        -- incl. this request
    aggregate_monthly_30d      numeric(14,6) NOT NULL,
    requires_finance_approval  boolean     NOT NULL,
    requires_plan_amendment    boolean     NOT NULL,
    status                     text        NOT NULL CHECK (status IN (
                                   'PENDING_PLAN_AMENDMENT', 'PENDING_CLARIFICATION',
                                   'PENDING_FINANCE_APPROVAL', 'PENDING_CUSTOMER_AUTHORIZATION',
                                   'APPROVED', 'ACTIVATED', 'REJECTED', 'EXPIRED')),
    requester_email            text        NOT NULL,
    requested_at               timestamptz NOT NULL,
    payload_hash               char(64)    NOT NULL,        -- idempotency: same id must mean same request
    calculation                jsonb       NOT NULL,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (contract_id, customer_id) REFERENCES contracts(contract_id, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_seat_requests_window ON seat_requests (customer_id, requested_at);
