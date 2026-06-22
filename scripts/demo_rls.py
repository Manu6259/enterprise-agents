"""Demo: prove RLS filters rows per identity.

Runs the same queries as four different users and prints a side-by-side
comparison. This is the artifact you screen-share in the interview.

Run:
    ./venv/bin/python scripts/demo_rls.py

Expected behaviour:
  Alice (North rep)   → sees only North customers
  Bob   (South rep)   → sees only South customers
  Maria (manager)     → sees all customers
  Root  (admin)       → sees all customers
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import connect  # noqa: E402

# Must match data/seed_rbac.py — same namespace + emails.
_NS = uuid.UUID("d6f3e2c1-1234-5678-9abc-def012345678")


def _uid(email: str) -> str:
    return str(uuid.uuid5(_NS, email))


USERS = [
    ("Alice (sales_rep, North)", _uid("alice@northsales.com")),
    ("Bob   (sales_rep, South)", _uid("bob@southsales.com")),
    ("Maria (manager)",           _uid("maria@manager.com")),
    ("Root  (admin)",             _uid("root@admin.com")),
]


def _customer_visibility() -> None:
    """How many customers does each user see, and from which regions?"""
    print("─" * 70)
    print("TEST 1 — customer visibility (SELECT * FROM customers)")
    print("─" * 70)
    for label, uid in USERS:
        with connect(user_id=uid) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT region, COUNT(*) AS n FROM customers "
                    "GROUP BY region ORDER BY region"
                )
                rows = cur.fetchall()
                total = sum(r["n"] for r in rows)
        regions = ", ".join(f"{r['region']}={r['n']}" for r in rows) or "(none)"
        print(f"  {label:30s} → {total:3d} customers   [{regions}]")


def _cross_territory_write_attempt() -> None:
    """Can Alice draft an outreach for a South customer? (She shouldn't.)"""
    print()
    print("─" * 70)
    print("TEST 2 — Alice tries to draft for a customer in Bob's territory")
    print("─" * 70)

    alice = _uid("alice@northsales.com")

    # Find a SOUTH customer using an owner connection (bypasses RLS)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, region FROM customers "
                "WHERE region = 'South' LIMIT 1"
            )
            south_cust = cur.fetchone()

    if not south_cust:
        print("  (no South customers seeded — skipping)")
        return

    print(f"  Target: customer #{south_cust['id']} ({south_cust['name']}) "
          f"in {south_cust['region']}")

    try:
        with connect(user_id=alice) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO outreach_drafts "
                    "(customer_id, drafted_by_agent, draft_content, "
                    " assigned_rep) "
                    "VALUES (%s, %s, %s, %s)",
                    (south_cust["id"], "sales_intelligence",
                     "Hello!", "alice@northsales.com"),
                )
        print("  ✗ INSERT SUCCEEDED — RLS is not blocking. BUG.")
    except Exception as e:
        # psycopg raises insufficient_privilege / new row violates RLS
        msg = str(e).splitlines()[0]
        print(f"  ✓ INSERT BLOCKED by RLS — {msg}")


def _approval_attempt() -> None:
    """Can a sales_rep approve a draft? (No — only managers can.)"""
    print()
    print("─" * 70)
    print("TEST 3 — Alice drafts (legal), then tries to self-approve (illegal)")
    print("─" * 70)

    alice = _uid("alice@northsales.com")
    maria = _uid("maria@manager.com")

    # Find a NORTH customer (Alice's territory)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM customers "
                "WHERE region = 'North' LIMIT 1"
            )
            cust = cur.fetchone()

    if not cust:
        print("  (no North customers seeded — skipping)")
        return

    # Alice drafts (allowed)
    with connect(user_id=alice) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO outreach_drafts "
                "(customer_id, drafted_by_agent, draft_content, assigned_rep) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (cust["id"], "sales_intelligence",
                 "Demo draft.", "alice@northsales.com"),
            )
            draft_id = cur.fetchone()["id"]
    print(f"  ✓ Alice drafted outreach #{draft_id} for {cust['name']} (North)")

    # Alice tries to self-approve
    try:
        with connect(user_id=alice) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE outreach_drafts SET status='approved' "
                    "WHERE id = %s",
                    (draft_id,),
                )
                # In Postgres an RLS-failing UPDATE silently affects 0 rows
                # rather than erroring. Check rowcount.
                if cur.rowcount == 0:
                    raise PermissionError("RLS denied: 0 rows updated")
        print("  ✗ Alice self-approved — RLS is not blocking. BUG.")
    except Exception as e:
        print(f"  ✓ Alice's self-approve BLOCKED — {str(e).splitlines()[0]}")

    # Maria approves (allowed)
    with connect(user_id=maria) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE outreach_drafts SET status='approved', "
                "reviewed_by=%s, reviewed_at=NOW() WHERE id = %s",
                (maria, draft_id),
            )
            updated = cur.rowcount
    print(f"  ✓ Maria approved (rows updated: {updated})")

    # Clean up the demo draft
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM outreach_drafts WHERE id = %s", (draft_id,))


def main() -> int:
    print("\nRLS demonstration — same queries, different identities.\n")
    _customer_visibility()
    _cross_territory_write_attempt()
    _approval_attempt()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
