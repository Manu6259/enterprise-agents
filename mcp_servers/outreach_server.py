"""Standalone MCP Server — Sales Outreach Composition.

Supports TWO transports chosen by the MCP_TRANSPORT env var:
  stdio — run as a subprocess (default)
  http  — run as a persistent streamable-HTTP server on OUTREACH_SERVER_PORT

No database — these tools work purely from the inputs the calling
agent provides. Their job is to translate upstream analytics into
structured, rep-ready deliverables (action plans and retention briefs).

Tools exposed:
  1. generate_action_plan          — priority + deadline + opening + bullets
  2. generate_retention_report     — formatted markdown brief for managers

Launch:
    python mcp_servers/outreach_server.py              # stdio
    MCP_TRANSPORT=http python mcp_servers/outreach_server.py
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Allow this standalone script to import config.py from the project root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import MCP_TRANSPORT, OUTREACH_SERVER_PORT  # noqa: E402
import audit  # noqa: E402
from db import connect_as_caller  # noqa: E402
from request_context import wrap_mcp_app  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402
import uvicorn  # noqa: E402


# ── Priority / deadline resolution ────────────────────────────────────
def _priority_and_deadline(
    risk_level: str, value_tier: str
) -> tuple[str, int]:
    """Map (risk_level, value_tier) → (priority label, deadline in days).

    critical + platinum → URGENT,  1 day
    critical + gold     → URGENT,  2 days
    critical + other    → URGENT,  3 days
    high + any          → HIGH,    5 days
    medium + any        → MEDIUM,  14 days
    low + any           → LOW,     30 days
    """
    risk = (risk_level or "").strip().lower()
    tier = (value_tier or "").strip().lower()

    if risk == "critical":
        if tier == "platinum":
            return "URGENT", 1
        if tier == "gold":
            return "URGENT", 2
        return "URGENT", 3
    if risk == "high":
        return "HIGH", 5
    if risk == "medium":
        return "MEDIUM", 14
    return "LOW", 30


def _format_products(items: list[str]) -> str:
    cleaned = [str(p).strip() for p in (items or []) if str(p).strip()]
    if not cleaned:
        return "no specific product recommendations yet"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


# ── Tool 1: generate_action_plan ──────────────────────────────────────
def _generate_action_plan(
    customer_name: str,
    customer_id: int,
    risk_level: str,
    ltv_annual: float,
    value_tier: str,
    recommended_products: list[str],
    assigned_rep: str,
    prior_interactions: int,
    days_since_purchase: int,
) -> dict[str, Any]:
    priority, deadline_days = _priority_and_deadline(risk_level, value_tier)
    product_phrase = _format_products(recommended_products)

    # Opening line blends recency, product fit, and prior rapport.
    if prior_interactions > 0:
        rapport = (
            f"We've spoken {prior_interactions} time"
            f"{'s' if prior_interactions != 1 else ''} before — "
        )
    else:
        rapport = "This is a first direct conversation — "

    if days_since_purchase > 180:
        recency = (
            f"it's been {days_since_purchase} days since your last "
            "purchase, and "
        )
    elif days_since_purchase > 90:
        recency = (
            f"we noticed it's been {days_since_purchase} days since "
            "your last order, and "
        )
    else:
        recency = ""

    suggested_opening = (
        f"Hi {customer_name}, {rapport}{recency}"
        f"I wanted to share a few items I think fit your profile: "
        f"{product_phrase}. Do you have ten minutes this week to talk "
        f"through them?"
    )

    action_summary = [
        f"Priority {priority} — contact within {deadline_days} day"
        f"{'s' if deadline_days != 1 else ''}.",
        f"Assigned rep: {assigned_rep}.",
        f"Lead with: {product_phrase}.",
        f"Estimated annual value at stake: ${ltv_annual:,.2f} "
        f"({value_tier.lower()} tier).",
    ]

    return {
        "customer_id": customer_id,
        "customer_name": customer_name,
        "priority": priority,
        "contact_deadline_days": deadline_days,
        "assigned_rep": assigned_rep,
        "recommended_products": list(recommended_products or []),
        "suggested_opening": suggested_opening,
        "action_summary": action_summary,
    }


# ── Tool 2: generate_retention_report ─────────────────────────────────
def _generate_retention_report(
    customer_name: str,
    risk_score: float,
    ltv_annual: float,
    value_tier: str,
    churn_factors: list[str],
    recommended_products: list[str],
    assigned_rep: str,
    action_plan_summary: list[str],
) -> str:
    # Infer risk_level from the numeric score using the same thresholds
    # as the scoring server, so the brief stays consistent.
    if risk_score > 0.7:
        risk_level = "CRITICAL"
    elif risk_score > 0.5:
        risk_level = "HIGH"
    elif risk_score >= 0.3:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    factors_md = "\n".join(f"- {f}" for f in (churn_factors or [])) or "- (none triggered)"
    products_md = (
        "\n".join(f"- {p}" for p in (recommended_products or []))
        or "- (no product recommendations available)"
    )
    actions_md = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(action_plan_summary or []))
    if not actions_md:
        actions_md = "1. (action plan pending)"

    lines = [
        f"# Retention Brief — {customer_name}",
        "",
        "## Customer summary",
        f"- **Name:** {customer_name}",
        f"- **Value tier:** {value_tier}",
        f"- **Estimated annual LTV:** ${ltv_annual:,.2f}",
        "",
        "## Risk assessment",
        f"- **Churn risk score:** {risk_score:.2f}",
        f"- **Risk level:** {risk_level}",
        "",
        "### Contributing factors",
        factors_md,
        "",
        "## Revenue at stake",
        f"Losing this customer costs ~${ltv_annual:,.2f} per year at "
        f"the {value_tier.lower()} tier.",
        "",
        "## Recommended products to offer",
        products_md,
        "",
        "## Assigned rep",
        f"- **{assigned_rep}** — see action plan for rationale.",
        "",
        "## Action plan",
        actions_md,
        "",
        "_Generated automatically by the outreach server — review before sending._",
    ]
    return "\n".join(lines)


# ── Tool 3: submit_draft (HITL gate) ──────────────────────────────────
def _submit_draft(
    customer_id: int,
    draft_content: str,
    assigned_rep: str,
) -> dict[str, Any]:
    """Persist a pending outreach draft. Nothing is sent — the row sits
    in ``outreach_drafts`` with ``status='pending'`` until a manager
    approves it via the review tool.

    RLS enforces the territory rule: a sales_rep can only insert a draft
    for a customer in their own territory. A manager/admin can draft for
    anyone. Cross-territory attempts are rejected by Postgres, not by
    this Python code.
    """
    if not draft_content or not draft_content.strip():
        raise ValueError("draft_content must be non-empty")

    with connect_as_caller() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO outreach_drafts "
                "(customer_id, drafted_by_agent, draft_content, "
                " assigned_rep, status) "
                "VALUES (%s, %s, %s, %s, 'pending') "
                "RETURNING id, created_at",
                (customer_id, "sales_intelligence",
                 draft_content, assigned_rep),
            )
            row = cur.fetchone()

    return {
        "draft_id": row["id"],
        "customer_id": customer_id,
        "assigned_rep": assigned_rep,
        "status": "pending",
        "created_at": row["created_at"].isoformat(),
        "note": (
            "Draft saved. It is NOT sent. A manager must approve it "
            "via the review queue before any outreach happens."
        ),
    }


# ── MCP server wiring ──────────────────────────────────────────────────
mcp = FastMCP(
    "outreach-server",
    host="127.0.0.1",
    port=OUTREACH_SERVER_PORT,
)


@mcp.tool(
    description=(
        "Generate a structured action plan for a sales rep. Resolves a "
        "priority label (URGENT / HIGH / MEDIUM / LOW) and a "
        "contact_deadline_days value from (risk_level, value_tier): "
        "critical+platinum=24h, critical+gold=48h, high=5d, medium=14d. "
        "Composes a natural-language suggested_opening using recent "
        "purchase history and the recommended products, plus a 4-bullet "
        "action_summary covering priority, rep, lead products, and "
        "revenue at stake. All inputs must be provided by the caller — "
        "this tool does not query any database."
    )
)
def generate_action_plan(
    customer_name: str,
    customer_id: int,
    risk_level: str,
    ltv_annual: float,
    value_tier: str,
    recommended_products: list[str],
    assigned_rep: str,
    prior_interactions: int,
    days_since_purchase: int,
) -> dict[str, Any]:
    return _generate_action_plan(
        customer_name=customer_name,
        customer_id=customer_id,
        risk_level=risk_level,
        ltv_annual=ltv_annual,
        value_tier=value_tier,
        recommended_products=recommended_products,
        assigned_rep=assigned_rep,
        prior_interactions=prior_interactions,
        days_since_purchase=days_since_purchase,
    )


@mcp.tool(
    description=(
        "Render a formatted markdown retention brief that can be sent "
        "directly to a sales manager. Sections: customer summary, risk "
        "assessment with contributing factors, revenue at stake, "
        "recommended products, assigned rep, and a numbered action "
        "plan. Risk level is inferred from the numeric risk_score using "
        "the same thresholds as the scoring server. Returns the full "
        "markdown as a single string."
    )
)
def generate_retention_report(
    customer_name: str,
    risk_score: float,
    ltv_annual: float,
    value_tier: str,
    churn_factors: list[str],
    recommended_products: list[str],
    assigned_rep: str,
    action_plan_summary: list[str],
) -> str:
    return _generate_retention_report(
        customer_name=customer_name,
        risk_score=risk_score,
        ltv_annual=ltv_annual,
        value_tier=value_tier,
        churn_factors=churn_factors,
        recommended_products=recommended_products,
        assigned_rep=assigned_rep,
        action_plan_summary=action_plan_summary,
    )


@mcp.tool(
    description=(
        "Submit an outreach draft to the human-in-the-loop approval "
        "queue. The draft is stored with status='pending' — NOTHING "
        "is sent. A manager must approve it via the review queue "
        "before any outreach is delivered to the customer. "
        "Row-level security enforces that a sales_rep can only draft "
        "for customers in their own territory; cross-territory attempts "
        "are rejected by the database. Returns the new draft_id."
    )
)
def submit_draft(
    customer_id: int,
    draft_content: str,
    assigned_rep: str,
) -> dict[str, Any]:
    return _submit_draft(
        customer_id=customer_id,
        draft_content=draft_content,
        assigned_rep=assigned_rep,
    )


# Record every tool call to agent_audit_log (append-only, RLS-scoped).
audit.instrument(mcp, "outreach-server")


# ── Entry point — transport chosen by MCP_TRANSPORT ────────────────────
if __name__ == "__main__":
    transport = MCP_TRANSPORT.lower()
    if transport == "http":
        # Wrap with auth middleware — submit_draft writes to the DB so
        # the request must carry a verified caller identity.
        app = wrap_mcp_app(mcp.streamable_http_app())
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=OUTREACH_SERVER_PORT,
            log_level="warning",
        )
    else:
        mcp.run(transport="stdio")
