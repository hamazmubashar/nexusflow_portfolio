-- NexusFlow Systems synthetic relational schema, PostgreSQL 14+
-- Apply before importing CSV. Money stored in exact NUMERIC, timestamps in UTC.
-- Import all CSVs in ONE transaction (BEGIN; SET CONSTRAINTS ALL DEFERRED;
-- \copy departments FROM ... CSV HEADER; ...; COMMIT;) because department
-- manager IDs reference employees, and employees reference departments.
-- Import order: departments; employees; products;
-- customers; contracts; invoices; payments; support_tickets; assets.
BEGIN;
CREATE TABLE departments (
 department_id varchar(8) PRIMARY KEY CHECK (department_id ~ '^DPT-[0-9]{4}$'),
 name text NOT NULL UNIQUE, headcount integer NOT NULL CHECK (headcount>0),
 manager_id varchar(8) NOT NULL CHECK (manager_id ~ '^EMP-[0-9]{4}$')
);
CREATE TABLE employees (
 employee_id varchar(8) PRIMARY KEY CHECK (employee_id ~ '^EMP-[0-9]{4}$'),
 first_name text NOT NULL, last_name text NOT NULL,
 email text NOT NULL UNIQUE CHECK (email ~ '^[^@[:space:]]+@[^@[:space:]]+$'),
 department_id varchar(8) NOT NULL REFERENCES departments(department_id),
 role text NOT NULL, salary numeric(12,2) NOT NULL CHECK (salary>=0),
 hire_date date NOT NULL, status text NOT NULL CHECK (status IN ('ACTIVE','INACTIVE','ON_LEAVE'))
);
ALTER TABLE departments ADD CONSTRAINT fk_department_manager FOREIGN KEY(manager_id)
 REFERENCES employees(employee_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE products (
 product_id varchar(8) PRIMARY KEY CHECK (product_id ~ '^PRD-[0-9]{4}$'),
 name text NOT NULL UNIQUE, module_code text NOT NULL UNIQUE,
 base_price_per_seat numeric(12,2) NOT NULL CHECK (base_price_per_seat>=0),
 description text NOT NULL
);
CREATE TABLE customers (
 customer_id varchar(9) PRIMARY KEY CHECK (customer_id ~ '^CUST-[0-9]{4}$'),
 company_name text NOT NULL, domain text NOT NULL UNIQUE,
 tier text NOT NULL CHECK (tier IN ('SMB','MID_MARKET','ENTERPRISE')),
 total_seats integer NOT NULL CHECK ((tier='SMB' AND total_seats BETWEEN 50 AND 249)
 OR (tier='MID_MARKET' AND total_seats BETWEEN 250 AND 999)
 OR (tier='ENTERPRISE' AND total_seats BETWEEN 1000 AND 10000)),
 status text NOT NULL CHECK (status IN ('ACTIVE','DELINQUENT','SUSPENDED','CHURNED')),
 created_at timestamptz NOT NULL,
 csm_employee_id varchar(8) NOT NULL REFERENCES employees(employee_id)
);
CREATE TABLE contracts (
 contract_id varchar(8) PRIMARY KEY CHECK (contract_id ~ '^CTR-[0-9]{4}$'),
 customer_id varchar(9) NOT NULL REFERENCES customers(customer_id),
 plan_tier text NOT NULL CHECK (plan_tier IN ('STARTER','GROWTH','ENTERPRISE')),
 start_date date NOT NULL, end_date date NOT NULL CHECK(end_date>start_date),
 annual_recurring_revenue numeric(14,2) NOT NULL CHECK(annual_recurring_revenue>=0),
 auto_renew boolean NOT NULL DEFAULT true,
 status text NOT NULL CHECK(status IN ('DRAFT','PENDING_SIGNATURE','ACTIVE','NONRENEWING','EXPIRED','TERMINATED')),
 UNIQUE(contract_id,customer_id)
);
CREATE UNIQUE INDEX uq_active_contract_per_customer ON contracts(customer_id) WHERE status='ACTIVE';
CREATE TABLE invoices (
 invoice_id varchar(13) PRIMARY KEY CHECK(invoice_id ~ '^INV-20[0-9]{2}-[0-9]{4}$'),
 contract_id varchar(8) NOT NULL, customer_id varchar(9) NOT NULL,
 issue_date date NOT NULL, due_date date NOT NULL CHECK(due_date>=issue_date),
 amount numeric(14,2) NOT NULL CHECK(amount>0),
 status text NOT NULL CHECK(status IN ('DRAFT','ISSUED','PAID','OVERDUE','VOID')),
 FOREIGN KEY(contract_id,customer_id) REFERENCES contracts(contract_id,customer_id)
);
CREATE TABLE payments (
 payment_id varchar(8) PRIMARY KEY CHECK(payment_id ~ '^PAY-[0-9]{4}$'),
 invoice_id varchar(13) NOT NULL REFERENCES invoices(invoice_id),
 payment_date date NOT NULL, amount numeric(14,2) NOT NULL CHECK(amount>0),
 payment_method text NOT NULL CHECK(payment_method IN ('ACH','WIRE','CARD')),
 status text NOT NULL CHECK(status IN ('PENDING','SETTLED','FAILED','REFUNDED'))
);
CREATE TABLE support_tickets (
 ticket_id varchar(7) PRIMARY KEY CHECK(ticket_id ~ '^TK-[0-9]{4}$'),
 customer_id varchar(9) NOT NULL REFERENCES customers(customer_id),
 created_by_email text NOT NULL,
 assigned_employee_id varchar(8) REFERENCES employees(employee_id),
 severity text NOT NULL CHECK(severity IN ('P1','P2','P3','P4')),
 subject text NOT NULL,
 status text NOT NULL CHECK(status IN ('NEW','TRIAGED','IN_PROGRESS','WAITING_CUSTOMER','RESOLVED','CLOSED')),
 created_at timestamptz NOT NULL, resolved_at timestamptz,
 CHECK(resolved_at IS NULL OR resolved_at>=created_at),
 CHECK ((status IN ('RESOLVED','CLOSED')) = (resolved_at IS NOT NULL))
);
CREATE TABLE assets (
 asset_id varchar(8) PRIMARY KEY CHECK(asset_id ~ '^AST-[0-9]{4}$'),
 customer_id varchar(9) NOT NULL REFERENCES customers(customer_id),
 device_name text NOT NULL, os_type text NOT NULL CHECK(os_type IN ('WINDOWS','MACOS','LINUX')),
 serial_number text NOT NULL UNIQUE,
 status text NOT NULL CHECK(status IN ('ACTIVE','RETIRED','LOST')),
 last_sync_at timestamptz NOT NULL,
 UNIQUE(customer_id,device_name)
);
CREATE INDEX idx_employees_department ON employees(department_id);
CREATE INDEX idx_customers_tier ON customers(tier);
CREATE INDEX idx_contracts_customer ON contracts(customer_id);
CREATE INDEX idx_invoices_contract ON invoices(contract_id);
CREATE INDEX idx_invoices_status_due ON invoices(status,due_date);
CREATE INDEX idx_payments_invoice ON payments(invoice_id);
CREATE INDEX idx_tickets_customer_severity ON support_tickets(customer_id,severity);
CREATE INDEX idx_tickets_open ON support_tickets(severity,created_at) WHERE resolved_at IS NULL;
CREATE INDEX idx_assets_customer ON assets(customer_id);
COMMIT;
-- After importing all CSVs, verify:
-- SELECT COUNT(*) FROM customers; -- 120
-- SELECT COUNT(*) FROM employees WHERE status='ACTIVE'; -- 85
-- SELECT SUM(annual_recurring_revenue) FROM contracts WHERE status='ACTIVE'; -- 12400000.00
-- SELECT tier,COUNT(*) FROM customers GROUP BY tier ORDER BY tier;
-- NOTE: Cross-table pricing, manager-department, full-payment, headcount and
-- snapshot-wide ARR checks are validated by generator; enforce with transactions,
-- triggers or controlled ingestion for production mutations. Not a complete SaaS
-- operational schema: approvals, renewal notices and SLA clocks need extra tables.
