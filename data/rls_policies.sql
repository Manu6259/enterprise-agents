-- Enterprise Agents — Row-Level Security policies (Day 2)
--
-- Strategy:
--   * Seed scripts and admin tooling connect as `postgres` (the table
--     owner) — owners bypass RLS, so seeders keep working untouched.
--   * Agent-facing code (MCP servers) will switch role to `authenticated`
--     per request (Step 3 of Day 2), at which point RLS applies.
--   * Identity travels via the `app.current_user_id` connection setting.
--   * Helper functions are SECURITY DEFINER so policies can safely
--     consult the `users` table without RLS recursion.
--
-- Idempotent — re-running drops & recreates each policy.

-- ── Helper functions ────────────────────────────────────────────────────
-- Read the user id we stashed on the connection. Returns NULL if unset.
CREATE OR REPLACE FUNCTION current_user_id()
RETURNS UUID
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid
$$;

-- Look up the calling user's role. SECURITY DEFINER → bypasses RLS on
-- the users table, so no recursion when policies call this.
CREATE OR REPLACE FUNCTION current_user_role()
RETURNS TEXT
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT role FROM users WHERE id = current_user_id()
$$;

-- Look up the calling user's territory (NULL for managers/admins).
CREATE OR REPLACE FUNCTION current_user_territory()
RETURNS TEXT
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT territory FROM users WHERE id = current_user_id()
$$;

-- Look up the calling user's email (used by outreach_drafts policies).
CREATE OR REPLACE FUNCTION current_user_email()
RETURNS TEXT
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT email FROM users WHERE id = current_user_id()
$$;

-- Grant the built-in Supabase `authenticated` role permission to call them.
GRANT EXECUTE ON FUNCTION current_user_id()        TO authenticated;
GRANT EXECUTE ON FUNCTION current_user_role()      TO authenticated;
GRANT EXECUTE ON FUNCTION current_user_territory() TO authenticated;
GRANT EXECUTE ON FUNCTION current_user_email()     TO authenticated;

-- ── Enable RLS on every table (owner still bypasses) ───────────────────
ALTER TABLE customers              ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions           ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_rep_interactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE outreach_drafts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_audit_log        ENABLE ROW LEVEL SECURITY;
ALTER TABLE users                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE roles                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE products               ENABLE ROW LEVEL SECURITY;

-- Grant baseline table privileges to `authenticated` — RLS then narrows.
GRANT SELECT ON
    customers, transactions, sales_rep_interactions,
    outreach_drafts, agent_audit_log, users, roles, sales, products
TO authenticated;

GRANT INSERT ON outreach_drafts, agent_audit_log TO authenticated;
GRANT UPDATE ON outreach_drafts                 TO authenticated;

-- BIGSERIAL/SERIAL columns rely on a hidden sequence; INSERT needs USAGE
-- on it. Without these grants, Postgres rejects the INSERT *before* RLS
-- gets a chance to evaluate the policy.
GRANT USAGE, SELECT ON SEQUENCE outreach_drafts_id_seq TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE agent_audit_log_id_seq TO authenticated;

-- ── Policies: customers ─────────────────────────────────────────────────
DROP POLICY IF EXISTS customers_read ON customers;
CREATE POLICY customers_read ON customers FOR SELECT
    USING (
        current_user_role() IN ('manager', 'admin')
        OR (current_user_role() = 'sales_rep'
            AND region = current_user_territory())
    );

-- ── Policies: transactions (visible iff parent customer is visible) ────
DROP POLICY IF EXISTS transactions_read ON transactions;
CREATE POLICY transactions_read ON transactions FOR SELECT
    USING (
        EXISTS (SELECT 1 FROM customers c WHERE c.id = transactions.customer_id)
    );

-- ── Policies: sales_rep_interactions (all reps see all) ────────────────
DROP POLICY IF EXISTS interactions_read ON sales_rep_interactions;
CREATE POLICY interactions_read ON sales_rep_interactions FOR SELECT
    USING (current_user_role() IS NOT NULL);

-- ── Policies: outreach_drafts ──────────────────────────────────────────
-- READ: manager/admin → all. sales_rep → any draft whose customer is in
-- their territory (the rep owns the territory, not the specific
-- assigned_rep name). This matches drafts_insert: if you can draft for
-- a customer, you can read drafts for that customer.
--
-- Subtle but important: an INSERT ... RETURNING re-checks SELECT on the
-- new row. If this policy were tighter than drafts_insert, every INSERT
-- with RETURNING would fail even though the INSERT itself was allowed.
DROP POLICY IF EXISTS drafts_read ON outreach_drafts;
CREATE POLICY drafts_read ON outreach_drafts FOR SELECT
    USING (
        current_user_role() IN ('manager', 'admin')
        OR EXISTS (
            SELECT 1 FROM customers c
            WHERE c.id = outreach_drafts.customer_id
              AND c.region = current_user_territory()
        )
    );

-- INSERT: rep can only draft for customers in their territory;
-- manager/admin can draft for anyone.
DROP POLICY IF EXISTS drafts_insert ON outreach_drafts;
CREATE POLICY drafts_insert ON outreach_drafts FOR INSERT
    WITH CHECK (
        current_user_role() IN ('manager', 'admin')
        OR EXISTS (
            SELECT 1 FROM customers c
            WHERE c.id = outreach_drafts.customer_id
              AND c.region = current_user_territory()
        )
    );

-- UPDATE: only managers/admins (the HITL approval gate).
DROP POLICY IF EXISTS drafts_update ON outreach_drafts;
CREATE POLICY drafts_update ON outreach_drafts FOR UPDATE
    USING (current_user_role() IN ('manager', 'admin'))
    WITH CHECK (current_user_role() IN ('manager', 'admin'));

-- ── Policies: agent_audit_log (append-only; rep sees own, others all) ──
DROP POLICY IF EXISTS audit_read ON agent_audit_log;
CREATE POLICY audit_read ON agent_audit_log FOR SELECT
    USING (
        current_user_role() IN ('manager', 'admin')
        OR user_id = current_user_id()
    );

DROP POLICY IF EXISTS audit_insert ON agent_audit_log;
CREATE POLICY audit_insert ON agent_audit_log FOR INSERT
    WITH CHECK (current_user_id() IS NOT NULL);
-- No UPDATE or DELETE policy → both are denied.

-- ── Policies: users ────────────────────────────────────────────────────
-- Rep: self only. Manager: self + their direct reports. Admin: all.
DROP POLICY IF EXISTS users_read ON users;
CREATE POLICY users_read ON users FOR SELECT
    USING (
        current_user_role() = 'admin'
        OR id = current_user_id()
        OR (current_user_role() = 'manager'
            AND manager_id = current_user_id())
        OR (current_user_role() = 'manager'
            AND id = current_user_id())
    );

-- ── Policies: roles (anyone authenticated can read the lookup) ─────────
DROP POLICY IF EXISTS roles_read ON roles;
CREATE POLICY roles_read ON roles FOR SELECT
    USING (current_user_id() IS NOT NULL);

-- ── Policies: sales, products (reference data; readable by all auth'd) ─
DROP POLICY IF EXISTS sales_read ON sales;
CREATE POLICY sales_read ON sales FOR SELECT
    USING (current_user_id() IS NOT NULL);

DROP POLICY IF EXISTS products_read ON products;
CREATE POLICY products_read ON products FOR SELECT
    USING (current_user_id() IS NOT NULL);
