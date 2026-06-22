"""Seed the RBAC tables: roles + users.

Idempotent — safe to run multiple times. Uses deterministic UUIDs
(uuid5 over email) so user IDs stay stable across seedings; the demo
script and any hardcoded test references keep working.

Run:
    ./venv/bin/python data/seed_rbac.py

Fixture:
  Sales reps (one per territory, each reports to Maria):
    Alice  → North
    Bob    → South
    Carol  → East
    David  → West
  Manager:
    Maria  → null territory (sees all)
  Admin:
    Root   → null territory (full access)
"""

from __future__ import annotations

import os
import sys
import uuid

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from db import connect  # noqa: E402


# Stable namespace so uuid5(email) is reproducible across runs/machines.
_NS = uuid.UUID("d6f3e2c1-1234-5678-9abc-def012345678")


def _uid(email: str) -> str:
    return str(uuid.uuid5(_NS, email))


# Fixture — order matters: manager is inserted before reps so the FK resolves.
MANAGER = {
    "email": "maria@manager.com",
    "role": "manager",
    "territory": None,
    "manager_id": None,
}

ADMIN = {
    "email": "root@admin.com",
    "role": "admin",
    "territory": None,
    "manager_id": None,
}

REPS = [
    {"email": "alice@northsales.com", "role": "sales_rep", "territory": "North"},
    {"email": "bob@southsales.com",   "role": "sales_rep", "territory": "South"},
    {"email": "carol@eastsales.com",  "role": "sales_rep", "territory": "East"},
    {"email": "david@westsales.com",  "role": "sales_rep", "territory": "West"},
]


def _seed_roles(conn) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO roles (name) VALUES (%s) "
            "ON CONFLICT (name) DO NOTHING",
            [("sales_rep",), ("manager",), ("admin",)],
        )


def _seed_users(conn) -> None:
    rows = []

    # Manager + admin first (no manager_id dependency)
    for u in (MANAGER, ADMIN):
        rows.append(
            (_uid(u["email"]), u["email"], u["role"], u["territory"], None)
        )

    manager_uuid = _uid(MANAGER["email"])

    # Reps all report to Maria
    for u in REPS:
        rows.append(
            (_uid(u["email"]), u["email"], u["role"], u["territory"], manager_uuid)
        )

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO users (id, email, role, territory, manager_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                email = EXCLUDED.email,
                role = EXCLUDED.role,
                territory = EXCLUDED.territory,
                manager_id = EXCLUDED.manager_id
            """,
            rows,
        )


def main() -> None:
    with connect() as conn:
        print("Seeding roles ...")
        _seed_roles(conn)
        print("Seeding users ...")
        _seed_users(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT email, role, territory FROM users ORDER BY role, email"
            )
            users = cur.fetchall()

    print("\nSeeded users:")
    for u in users:
        terr = u["territory"] or "—"
        print(f"  {u['role']:10s}  {u['email']:25s}  territory={terr}")
    print(f"\n  Total: {len(users)} users, 3 roles")


if __name__ == "__main__":
    main()
