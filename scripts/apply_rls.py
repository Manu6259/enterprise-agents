"""Apply data/rls_policies.sql to Supabase.

Idempotent — the SQL file uses DROP POLICY IF EXISTS / CREATE OR REPLACE
so re-running is safe.

Run:
    ./venv/bin/python scripts/apply_rls.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import connect  # noqa: E402

POLICIES_SQL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "rls_policies.sql",
)


def main() -> int:
    with open(POLICIES_SQL, "r") as f:
        sql = f.read()

    print(f"Applying {POLICIES_SQL} ...")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)

        # Show what's now in place
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename, policyname, cmd "
                "FROM pg_policies WHERE schemaname = 'public' "
                "ORDER BY tablename, policyname"
            )
            policies = cur.fetchall()

    print(f"\nApplied. {len(policies)} policies now active:")
    last_table = None
    for p in policies:
        if p["tablename"] != last_table:
            print(f"  {p['tablename']}")
            last_table = p["tablename"]
        print(f"    - {p['policyname']:25s} ({p['cmd']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
