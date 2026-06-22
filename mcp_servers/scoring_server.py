"""Standalone MCP Server — Customer Intelligence Scoring.

Supports TWO transports chosen by the MCP_TRANSPORT env var:
  stdio — run as a subprocess (default, used by agents in local mode)
  http  — run as a persistent streamable-HTTP server on SCORING_SERVER_PORT

All four tools share the same analytics.db used by the data-analysis
agent. No separate database.

Tools exposed:
  1. calculate_churn_risk       — per-customer 0.0-1.0 risk score + factors
  2. calculate_lifetime_value   — monthly + annual LTV + value tier
  3. segment_all_customers      — exclusive segment assignment across base
  4. get_cohort_analysis        — signup-period cohorts with retention

Launch:
    python mcp_servers/scoring_server.py              # stdio
    MCP_TRANSPORT=http python mcp_servers/scoring_server.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from typing import Any, Literal

# Allow this standalone script to import config.py from the project root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import MCP_TRANSPORT, SCORING_SERVER_PORT  # noqa: E402
import audit  # noqa: E402
from db import connect_as_caller  # noqa: E402
from request_context import wrap_mcp_app  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402
import uvicorn  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _days_since(d: date | None) -> int:
    if d is None:
        # No recorded purchase — treat as very old so churn signal fires
        return 10_000
    return (date.today() - d).days


def _months_between(d_from: date | None, d_to: date | None) -> float:
    if d_from is None or d_to is None:
        return 1.0
    delta_days = max(1, (d_to - d_from).days)
    return max(1.0, delta_days / 30.0)


def _risk_level(score: float) -> str:
    if score > 0.7:
        return "critical"
    if score > 0.5:
        return "high"
    if score >= 0.3:
        return "medium"
    return "low"


def _value_tier(ltv_annual: float) -> str:
    if ltv_annual > 3000:
        return "platinum"
    if ltv_annual > 1500:
        return "gold"
    if ltv_annual > 500:
        return "silver"
    return "bronze"


# ── Core scoring functions (stateless) ─────────────────────────────────
def _get_customer(conn, customer_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM customers WHERE id = %s", (customer_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Customer id {customer_id} not found")
    return row


def _overall_avg_order_value(conn) -> float:
    row = conn.execute(
        "SELECT AVG(average_order_value) AS avg_aov FROM customers "
        "WHERE average_order_value IS NOT NULL"
    ).fetchone()
    return float(row["avg_aov"]) if row and row["avg_aov"] is not None else 0.0


def _calculate_churn_risk(customer_id: int) -> dict[str, Any]:
    with connect_as_caller() as conn:
        customer = _get_customer(conn, customer_id)
        overall_avg = _overall_avg_order_value(conn)

    last_purchase = _parse_date(customer["last_purchase_date"])
    days_since = _days_since(last_purchase)

    score = 0.0
    factors: list[str] = []

    # Recency of last purchase — the >180 rule REPLACES the >90 rule.
    if days_since > 180:
        score += 0.4
        factors.append(
            f"No purchase in {days_since} days (>180) — +0.40"
        )
    elif days_since > 90:
        score += 0.3
        factors.append(
            f"No purchase in {days_since} days (>90) — +0.30"
        )

    if customer["total_orders"] is not None and customer["total_orders"] < 3:
        score += 0.2
        factors.append(
            f"Low order count ({customer['total_orders']} < 3) — +0.20"
        )

    aov = customer["average_order_value"]
    if aov is not None and overall_avg > 0 and aov < overall_avg:
        score += 0.1
        factors.append(
            f"AOV ${aov:.2f} is below overall avg ${overall_avg:.2f} — +0.10"
        )

    if (customer["status"] or "").lower() == "inactive":
        score += 0.2
        factors.append("Customer status is 'inactive' — +0.20")

    score = min(1.0, round(score, 3))

    return {
        "customer_id": customer_id,
        "customer_name": customer["name"],
        "score": score,
        "risk_level": _risk_level(score),
        "factors": factors or ["No risk factors triggered"],
    }


def _calculate_lifetime_value(customer_id: int) -> dict[str, Any]:
    with connect_as_caller() as conn:
        customer = _get_customer(conn, customer_id)

    total_orders = customer["total_orders"] or 0
    total_spent = float(customer["total_spent"] or 0.0)
    signup = _parse_date(customer["signup_date"])

    if total_orders <= 0 or total_spent <= 0:
        return {
            "customer_id": customer_id,
            "customer_name": customer["name"],
            "avg_order_value": 0.0,
            "purchase_frequency_per_month": 0.0,
            "ltv_monthly": 0.0,
            "ltv_annual": 0.0,
            "value_tier": "bronze",
        }

    avg_order_value = total_spent / total_orders
    months = _months_between(signup, date.today())
    frequency = total_orders / months
    ltv_monthly = avg_order_value * frequency
    ltv_annual = ltv_monthly * 12

    return {
        "customer_id": customer_id,
        "customer_name": customer["name"],
        "avg_order_value": round(avg_order_value, 2),
        "purchase_frequency_per_month": round(frequency, 3),
        "ltv_monthly": round(ltv_monthly, 2),
        "ltv_annual": round(ltv_annual, 2),
        "value_tier": _value_tier(ltv_annual),
    }


def _segment_all_customers() -> dict[str, Any]:
    """Assign every customer to exactly one segment by priority.

    Each segment's ``customers`` list contains ``{id, name}`` pairs so
    downstream tools (calculate_churn_risk, calculate_lifetime_value)
    can be invoked with the correct ``customer_id``.
    """
    with connect_as_caller() as conn:
        customers = [dict(r) for r in conn.execute("SELECT * FROM customers")]

        # VIP is defined as the top 10% by total_spent
        sorted_by_spent = sorted(
            customers, key=lambda c: c["total_spent"] or 0, reverse=True
        )
        vip_count = max(1, len(sorted_by_spent) // 10)
        vip_ids = {c["id"] for c in sorted_by_spent[:vip_count]}

    today = date.today()
    segments: dict[str, list[dict[str, Any]]] = {
        "vip": [],
        "new": [],
        "active": [],
        "at_risk": [],
        "inactive": [],
    }

    for c in customers:
        row = {"id": c["id"], "name": c["name"]}

        # Priority order: vip > new > active > at_risk > inactive
        if c["id"] in vip_ids:
            segments["vip"].append(row)
            continue

        signup = _parse_date(c["signup_date"])
        if signup is not None and (today - signup).days <= 30:
            segments["new"].append(row)
            continue

        last = _parse_date(c["last_purchase_date"])
        if last is not None and (today - last).days <= 30:
            segments["active"].append(row)
            continue

        # Churn score is expensive — compute only if we reach this branch.
        churn = _calculate_churn_risk(c["id"])["score"]
        if churn > 0.6:
            segments["at_risk"].append(row)
            continue

        days_since_last = _days_since(last)
        if days_since_last > 90:
            segments["inactive"].append(row)
            continue

        # Anything left is unassigned (not in any named segment).

    return {
        name: {"count": len(rows), "customers": rows}
        for name, rows in segments.items()
    }


def _get_cohort_analysis(period: Literal["monthly", "quarterly"]) -> dict[str, Any]:
    if period not in {"monthly", "quarterly"}:
        raise ValueError("period must be 'monthly' or 'quarterly'")

    with connect_as_caller() as conn:
        customers = [dict(r) for r in conn.execute("SELECT * FROM customers")]

    def _cohort_label(d: date) -> str:
        if period == "monthly":
            return f"{d.year}-{d.month:02d}"
        # quarterly
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}"

    buckets: dict[str, list[dict[str, Any]]] = {}
    for c in customers:
        signup = _parse_date(c["signup_date"])
        if signup is None:
            continue
        buckets.setdefault(_cohort_label(signup), []).append(c)

    cohorts: list[dict[str, Any]] = []
    for label in sorted(buckets.keys()):
        rows = buckets[label]
        count = len(rows)
        avg_spent = sum((r["total_spent"] or 0) for r in rows) / count
        avg_orders = sum((r["total_orders"] or 0) for r in rows) / count
        retained = sum(1 for r in rows if (r["total_orders"] or 0) > 1)
        retention_rate = retained / count if count else 0.0
        cohorts.append(
            {
                "cohort": label,
                "customer_count": count,
                "avg_total_spent": round(avg_spent, 2),
                "avg_total_orders": round(avg_orders, 2),
                "retention_rate": round(retention_rate, 3),
            }
        )

    return {"period": period, "cohorts": cohorts}


# ── MCP server wiring ──────────────────────────────────────────────────
mcp = FastMCP(
    "scoring-server",
    host="127.0.0.1",
    port=SCORING_SERVER_PORT,
)


@mcp.tool(
    description=(
        "Calculate a 0.0-1.0 churn risk score for a single customer. "
        "Score is built from recency (>90 / >180 days since last "
        "purchase), low order count (<3), below-average order value, "
        "and 'inactive' status. Returns the customer name, the "
        "numeric score, a risk_level label (low/medium/high/critical), "
        "and a 'factors' list explaining each contribution."
    )
)
def calculate_churn_risk(customer_id: int) -> dict[str, Any]:
    return _calculate_churn_risk(customer_id)


@mcp.tool(
    description=(
        "Calculate customer lifetime value. Formula: avg_order_value × "
        "purchase_frequency_per_month × 12 → ltv_annual. Assigns a "
        "value tier: platinum (>3000), gold (>1500), silver (>500), "
        "bronze (≤500). Returns all intermediate values plus the tier."
    )
)
def calculate_lifetime_value(customer_id: int) -> dict[str, Any]:
    return _calculate_lifetime_value(customer_id)


@mcp.tool(
    description=(
        "Assign every customer to exactly one of five segments using a "
        "priority order: vip > new > active > at_risk > inactive. VIP = "
        "top 10% by total_spent; new = signup within 30 days; active = "
        "last purchase within 30 days; at_risk = churn score > 0.6; "
        "inactive = last purchase > 90 days ago. Returns a dict with "
        "count and a list of {id, name} objects for each segment — use "
        "the id values when calling calculate_churn_risk or "
        "calculate_lifetime_value."
    )
)
def segment_all_customers() -> dict[str, Any]:
    return _segment_all_customers()


@mcp.tool(
    description=(
        "Group customers by signup period and produce cohort metrics: "
        "customer count, average total spent, average total orders, and "
        "retention rate (share of customers with more than one order). "
        "Parameter 'period' must be 'monthly' or 'quarterly'."
    )
)
def get_cohort_analysis(period: str) -> dict[str, Any]:
    return _get_cohort_analysis(period)  # type: ignore[arg-type]


# Record every tool call to agent_audit_log (append-only, RLS-scoped).
audit.instrument(mcp, "scoring-server")


# ── Entry point — transport chosen by MCP_TRANSPORT ────────────────────
if __name__ == "__main__":
    transport = MCP_TRANSPORT.lower()
    if transport == "http":
        app = wrap_mcp_app(mcp.streamable_http_app())
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=SCORING_SERVER_PORT,
            log_level="warning",
        )
    else:
        mcp.run(transport="stdio")
