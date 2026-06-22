-- Enterprise Agents — Postgres schema
--
-- Day 1 scope: tables only. No RLS policies yet (added Day 2).
-- All DROP CASCADE first so the seeder is idempotent.
--
-- Data types deliberately mirror the previous SQLite schema (TEXT dates,
-- DOUBLE PRECISION money) to minimise behaviour drift in the MCP servers.
-- We will tighten types in a later pass once the migration is stable.

-- ── Drop existing (idempotent) ──────────────────────────────────────────
DROP TABLE IF EXISTS sales_rep_interactions CASCADE;
DROP TABLE IF EXISTS transactions CASCADE;
DROP TABLE IF EXISTS outreach_drafts CASCADE;
DROP TABLE IF EXISTS agent_audit_log CASCADE;
DROP TABLE IF EXISTS customers CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS sales CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS roles CASCADE;

-- ── Existing 5 tables (ported from SQLite) ──────────────────────────────
CREATE TABLE sales (
    id             INTEGER PRIMARY KEY,
    date           TEXT,
    product_name   TEXT,
    category       TEXT,
    quantity       INTEGER,
    unit_price     DOUBLE PRECISION,
    total_revenue  DOUBLE PRECISION,
    region         TEXT,
    sales_rep      TEXT
);

CREATE TABLE customers (
    id                  INTEGER PRIMARY KEY,
    name                TEXT,
    email               TEXT UNIQUE,
    region              TEXT,
    signup_date         TEXT,
    total_orders        INTEGER,
    total_spent         DOUBLE PRECISION,
    status              TEXT,
    last_purchase_date  TEXT,
    average_order_value DOUBLE PRECISION
);

CREATE TABLE products (
    id              INTEGER PRIMARY KEY,
    name            TEXT,
    category        TEXT,
    unit_price      DOUBLE PRECISION,
    stock_quantity  INTEGER,
    supplier        TEXT
);

CREATE TABLE transactions (
    id            INTEGER PRIMARY KEY,
    customer_id   INTEGER REFERENCES customers (id) ON DELETE CASCADE,
    date          TEXT,
    amount        DOUBLE PRECISION,
    product_name  TEXT,
    category      TEXT
);

CREATE TABLE sales_rep_interactions (
    id                INTEGER PRIMARY KEY,
    customer_id       INTEGER REFERENCES customers (id) ON DELETE CASCADE,
    sales_rep         TEXT,
    interaction_date  TEXT,
    outcome           TEXT,
    notes             TEXT
);

-- Useful read-path indexes (analytics queries hit these often)
CREATE INDEX idx_transactions_customer_id ON transactions (customer_id);
CREATE INDEX idx_transactions_date ON transactions (date);
CREATE INDEX idx_sales_rep_interactions_customer_id ON sales_rep_interactions (customer_id);
CREATE INDEX idx_customers_region ON customers (region);

-- ── New: RBAC + audit + HITL (skeleton; RLS lands Day 2) ────────────────

CREATE TABLE roles (
    id    SERIAL PRIMARY KEY,
    name  TEXT UNIQUE NOT NULL  -- 'sales_rep' | 'manager' | 'admin'
);

CREATE TABLE users (
    id          UUID PRIMARY KEY,        -- mirrors auth.users.id when wired
    email       TEXT UNIQUE NOT NULL,
    role        TEXT NOT NULL REFERENCES roles (name),
    territory   TEXT,                    -- e.g. 'North', null for managers/admins
    manager_id  UUID REFERENCES users (id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_role ON users (role);
CREATE INDEX idx_users_manager_id ON users (manager_id);

-- Every agent tool call lands here. Agents write, humans (managers/admin) read.
CREATE TABLE agent_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id         UUID REFERENCES users (id),  -- who invoked the agent
    agent_name      TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    tool_args       JSONB,
    result_summary  TEXT,                         -- truncated for storage
    status          TEXT NOT NULL                 -- 'success' | 'error' | 'blocked'
);

CREATE INDEX idx_audit_ts ON agent_audit_log (ts DESC);
CREATE INDEX idx_audit_user_id ON agent_audit_log (user_id);

-- HITL: sales_intelligence writes drafts here; manager approves before send.
CREATE TABLE outreach_drafts (
    id               BIGSERIAL PRIMARY KEY,
    customer_id      INTEGER NOT NULL REFERENCES customers (id),
    drafted_by_agent TEXT NOT NULL,
    draft_content    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'approved' | 'rejected'
    assigned_rep     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by      UUID REFERENCES users (id),
    reviewed_at      TIMESTAMPTZ,
    review_notes     TEXT
);

CREATE INDEX idx_drafts_status ON outreach_drafts (status);
CREATE INDEX idx_drafts_customer_id ON outreach_drafts (customer_id);
