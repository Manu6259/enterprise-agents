"""Manager review queue for outreach drafts (HITL gate).

Run as a manager:
    ./venv/bin/python scripts/review_drafts.py --user maria@manager.com

Behaviour:
  * Lists every pending draft visible to the caller. RLS handles
    "visible to": a sales_rep only sees their own drafts; a manager
    sees the whole team's; admin sees everything.
  * For each draft the operator chooses: [a]pprove, [r]eject, [s]kip, [q]uit.
  * Approve/reject UPDATE the row with status + reviewed_by + reviewed_at.
    Postgres' RLS policy enforces the HITL rule: only manager/admin can
    flip status. A rep running this script can list their own pending
    drafts but every approve attempt is rejected by the DB.

The point: the approval gate lives in Postgres. Even if this script
were rewritten by a malicious agent, the DB still says no.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import connect  # noqa: E402


def _resolve_user(email: str) -> tuple[str, str, str]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, role FROM users WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
    if row is None:
        print(f"User '{email}' not found.")
        sys.exit(1)
    return str(row["id"]), row["email"], row["role"]


def _list_pending(user_id: str) -> list[dict]:
    with connect(user_id=user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.id, d.customer_id, d.draft_content, "
                "       d.assigned_rep, d.created_at, "
                "       c.name AS customer_name, c.region AS customer_region "
                "  FROM outreach_drafts d "
                "  JOIN customers c ON c.id = d.customer_id "
                " WHERE d.status = 'pending' "
                " ORDER BY d.created_at ASC"
            )
            return cur.fetchall()


def _decide(
    user_id: str,
    draft_id: int,
    new_status: str,
    notes: str | None,
) -> int:
    """UPDATE status. Returns rows updated (0 → RLS blocked)."""
    with connect(user_id=user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE outreach_drafts "
                "   SET status = %s, "
                "       reviewed_by = %s, "
                "       reviewed_at = NOW(), "
                "       review_notes = %s "
                " WHERE id = %s",
                (new_status, user_id, notes, draft_id),
            )
            return cur.rowcount


def _show(draft: dict, idx: int, total: int) -> None:
    print()
    print("─" * 70)
    print(f"Draft #{draft['id']}  ({idx + 1}/{total})")
    print("─" * 70)
    print(f"  Customer:    {draft['customer_name']} "
          f"(id={draft['customer_id']}, region={draft['customer_region']})")
    print(f"  Assigned to: {draft['assigned_rep']}")
    print(f"  Created:     {draft['created_at']}")
    print()
    print("  Draft content:")
    for line in (draft["draft_content"] or "").splitlines() or [""]:
        print(f"    {line}")
    print()


def _prompt() -> tuple[str, str | None]:
    while True:
        choice = input("  [a]pprove / [r]eject / [s]kip / [q]uit > ").strip().lower()
        if choice in {"a", "approve"}:
            note = input("  Optional approval note (Enter to skip): ").strip() or None
            return "approved", note
        if choice in {"r", "reject"}:
            note = input("  Reason for rejection (Enter to skip): ").strip() or None
            return "rejected", note
        if choice in {"s", "skip"}:
            return "skip", None
        if choice in {"q", "quit"}:
            return "quit", None
        print("  Invalid — pick a / r / s / q")


def main() -> int:
    if "--user" not in sys.argv:
        print("Usage: ./venv/bin/python scripts/review_drafts.py "
              "--user <email>")
        print("Recommended: --user maria@manager.com  (only managers "
              "and admins can approve)")
        return 1
    email = sys.argv[sys.argv.index("--user") + 1]
    user_id, email, role = _resolve_user(email)
    print(f"\nReviewer: {email}  (role={role}, user_id={user_id})\n")

    drafts = _list_pending(user_id)
    if not drafts:
        print("No pending drafts visible to you. Nothing to review.")
        return 0

    print(f"{len(drafts)} pending draft(s) in your queue.")

    summary = {"approved": 0, "rejected": 0, "skipped": 0, "blocked": 0}

    for i, d in enumerate(drafts):
        _show(d, i, len(drafts))
        action, note = _prompt()

        if action == "skip":
            summary["skipped"] += 1
            continue
        if action == "quit":
            print("\nStopping. Remaining drafts left pending.")
            break

        updated = _decide(user_id, d["id"], action, note)
        if updated == 0:
            print(f"  ✗ {action.upper()} BLOCKED by RLS — "
                  f"your role ({role}) cannot change draft status.")
            summary["blocked"] += 1
        else:
            print(f"  ✓ Draft #{d['id']} {action}.")
            summary[action] += 1

    print()
    print("─" * 70)
    print("Summary")
    print("─" * 70)
    print(f"  Approved : {summary['approved']}")
    print(f"  Rejected : {summary['rejected']}")
    print(f"  Skipped  : {summary['skipped']}")
    if summary["blocked"]:
        print(f"  Blocked  : {summary['blocked']}  (RLS denied the update)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
