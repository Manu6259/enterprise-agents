"""Standalone MCP Server — Sales Recommendation Engine.

Supports TWO transports chosen by the MCP_TRANSPORT env var:
  stdio — run as a subprocess (default)
  http  — run as a persistent streamable-HTTP server on RECOMMENDATION_SERVER_PORT

All three tools share the same analytics.db used by the other data
agents. Recommendations blend customers, transactions, products, and
sales_rep_interactions.

Tools exposed:
  1. get_similar_customers        — peers of a customer within a segment
  2. get_best_sales_rep           — highest-scoring rep for one customer
  3. get_product_recommendations  — unbought products popular with peers

Launch:
    python mcp_servers/recommendation_server.py              # stdio
    MCP_TRANSPORT=http python mcp_servers/recommendation_server.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import date, datetime
from typing import Any

# Allow this standalone script to import config.py from the project root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import (  # noqa: E402
    MCP_TRANSPORT,
    RECOMMENDATION_SERVER_PORT,
)
import audit  # noqa: E402
from db import connect_as_caller  # noqa: E402
from request_context import wrap_mcp_app  # noqa: E402

import uvicorn  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402


# ── DB helpers ─────────────────────────────────────────────────────────
def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _get_customer(conn, customer_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM customers WHERE id = %s", (customer_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Customer id {customer_id} not found")
    return row


def _top_category_for_customer(conn, customer_id: int) -> str | None:
    row = conn.execute(
        "SELECT category, COUNT(*) AS c FROM transactions "
        "WHERE customer_id = %s "
        "GROUP BY category ORDER BY c DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    return row["category"] if row else None


def _products_bought_by_customer(conn, customer_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT product_name FROM transactions WHERE customer_id = %s",
        (customer_id,),
    ).fetchall()
    return {r["product_name"] for r in rows}


# ── Segment membership (mirrors scoring_server logic) ─────────────────
_VALID_SEGMENTS = {"vip", "active", "at_risk", "inactive", "new"}


def _customer_ids_in_segment(conn, segment: str) -> set[int]:
    """Return customer ids that fall in *segment*.

    Mirrors the priority rules used by scoring_server.segment_all_customers
    so the two views of the customer base stay in sync. Priority order:
    vip > new > active > at_risk > inactive.
    """
    segment = segment.lower()
    if segment not in _VALID_SEGMENTS:
        raise ValueError(
            f"Unknown segment '{segment}'. "
            f"Valid: {sorted(_VALID_SEGMENTS)}"
        )

    customers = [dict(r) for r in conn.execute("SELECT * FROM customers")]
    today = date.today()

    # Compute vip ids once (top 10% by total_spent)
    by_spent = sorted(customers, key=lambda c: c["total_spent"] or 0, reverse=True)
    vip_count = max(1, len(by_spent) // 10)
    vip_ids = {c["id"] for c in by_spent[:vip_count]}

    assigned: dict[int, str] = {}
    for c in customers:
        if c["id"] in vip_ids:
            assigned[c["id"]] = "vip"
            continue
        signup = _parse_date(c["signup_date"])
        if signup is not None and (today - signup).days <= 30:
            assigned[c["id"]] = "new"
            continue
        last = _parse_date(c["last_purchase_date"])
        if last is not None and (today - last).days <= 30:
            assigned[c["id"]] = "active"
            continue
        # "at_risk" would need full churn scoring — approximate via the
        # same signals recency>90, low orders, below-avg AOV, inactive status.
        days_since_last = (today - last).days if last else 10_000
        score = 0.0
        if days_since_last > 180:
            score += 0.4
        elif days_since_last > 90:
            score += 0.3
        if (c["total_orders"] or 0) < 3:
            score += 0.2
        if (c["status"] or "").lower() == "inactive":
            score += 0.2
        if score > 0.6:
            assigned[c["id"]] = "at_risk"
            continue
        if days_since_last > 90:
            assigned[c["id"]] = "inactive"

    return {cid for cid, seg in assigned.items() if seg == segment}


# ── Tool 1: get_similar_customers ─────────────────────────────────────
def _get_similar_customers(customer_id: int, segment: str) -> dict[str, Any]:
    with connect_as_caller() as conn:
        target = _get_customer(conn, customer_id)
        target_spent = float(target["total_spent"] or 0.0)
        target_category = _top_category_for_customer(conn, customer_id)
        segment_ids = _customer_ids_in_segment(conn, segment)

        # 30% band around target's total_spent
        low = target_spent * 0.7
        high = target_spent * 1.3 if target_spent > 0 else float("inf")

        # Candidate peers: same segment, different customer, spent in band
        peers: list[dict[str, Any]] = []
        for cid in segment_ids:
            if cid == customer_id:
                continue
            c = conn.execute(
                "SELECT * FROM customers WHERE id = %s", (cid,)
            ).fetchone()
            spent = float(c["total_spent"] or 0.0)
            if spent < low or spent > high:
                continue
            # Require same top category (if target has one)
            if target_category is not None:
                peer_top = _top_category_for_customer(conn, cid)
                if peer_top != target_category:
                    continue
            peers.append(c)

        target_products = _products_bought_by_customer(conn, customer_id)

        # Count products that peers bought and target has not
        product_counter: Counter[str] = Counter()
        peer_categories: dict[str, str] = {}
        for peer in peers:
            rows = conn.execute(
                "SELECT product_name, category FROM transactions "
                "WHERE customer_id = %s",
                (peer["id"],),
            ).fetchall()
            seen: set[str] = set()
            for r in rows:
                if r["product_name"] in target_products:
                    continue
                if r["product_name"] in seen:
                    continue
                seen.add(r["product_name"])
                product_counter[r["product_name"]] += 1
                peer_categories[r["product_name"]] = r["category"]

        products_peers_bought = [
            {
                "product_name": name,
                "category": peer_categories.get(name),
                "bought_by_similar_count": count,
            }
            for name, count in product_counter.most_common()
        ]

    return {
        "target_customer_id": customer_id,
        "target_customer_name": target["name"],
        "segment": segment,
        "target_top_category": target_category,
        "similar_customers": [
            {"id": p["id"], "name": p["name"]} for p in peers
        ],
        "similar_customer_count": len(peers),
        "products_peers_bought_that_target_has_not": products_peers_bought,
    }


# ── Tool 2: get_best_sales_rep ────────────────────────────────────────
def _get_best_sales_rep(customer_id: int, region: str) -> dict[str, Any]:
    with connect_as_caller() as conn:
        target = _get_customer(conn, customer_id)

        # Gather every prior interaction with *this* customer, per rep.
        own_rows = conn.execute(
            "SELECT sales_rep, outcome FROM sales_rep_interactions "
            "WHERE customer_id = %s",
            (customer_id,),
        ).fetchall()

        if own_rows:
            # Score reps that have talked to this customer before.
            per_rep: dict[str, dict[str, int]] = {}
            for r in own_rows:
                rep = r["sales_rep"]
                slot = per_rep.setdefault(
                    rep, {"prior": 0, "successful": 0}
                )
                slot["prior"] += 1
                if (r["outcome"] or "").lower() == "successful":
                    slot["successful"] += 1

            # Does this rep also have history in the customer's region?
            region_reps = {
                row["sales_rep"]
                for row in conn.execute(
                    "SELECT DISTINCT s.sales_rep "
                    "FROM sales_rep_interactions s "
                    "JOIN customers c ON c.id = s.customer_id "
                    "WHERE c.region = %s",
                    (region,),
                )
            }

            scored: list[dict[str, Any]] = []
            for rep, stats in per_rep.items():
                score = stats["prior"] * 3 + stats["successful"] * 2
                if rep in region_reps:
                    score += 1
                success_rate = (
                    (stats["successful"] / stats["prior"]) * 100
                    if stats["prior"] > 0
                    else 0.0
                )
                scored.append(
                    {
                        "rep": rep,
                        "score": score,
                        "prior": stats["prior"],
                        "successful": stats["successful"],
                        "success_rate": round(success_rate, 1),
                    }
                )

            scored.sort(key=lambda x: (-x["score"], -x["success_rate"]))
            best = scored[0]
            reason_parts = [
                f"{best['prior']} prior interaction(s) with "
                f"{target['name']}"
            ]
            if best["successful"]:
                reason_parts.append(
                    f"{best['successful']} of which were successful"
                )
            if best["rep"] in region_reps:
                reason_parts.append(f"also works the {region} region")

            return {
                "rep_name": best["rep"],
                "prior_interactions": best["prior"],
                "success_rate": best["success_rate"],
                "recommendation_reason": (
                    f"{best['rep']} is the strongest match — "
                    + "; ".join(reason_parts) + "."
                ),
                "source": "prior_history",
            }

        # Fallback: no prior history with this customer — pick the rep
        # with the highest success rate across the target region.
        region_rows = conn.execute(
            "SELECT s.sales_rep AS sales_rep, s.outcome AS outcome "
            "FROM sales_rep_interactions s "
            "JOIN customers c ON c.id = s.customer_id "
            "WHERE c.region = %s",
            (region,),
        ).fetchall()

        per_rep: dict[str, dict[str, int]] = {}
        for r in region_rows:
            rep = r["sales_rep"]
            slot = per_rep.setdefault(rep, {"total": 0, "successful": 0})
            slot["total"] += 1
            if (r["outcome"] or "").lower() == "successful":
                slot["successful"] += 1

        if not per_rep:
            return {
                "rep_name": None,
                "prior_interactions": 0,
                "success_rate": 0.0,
                "recommendation_reason": (
                    f"No interaction history exists for the {region} "
                    "region. Assign any available rep."
                ),
                "source": "no_data",
            }

        ranked = [
            {
                "rep": rep,
                "success_rate": (
                    (s["successful"] / s["total"]) * 100 if s["total"] else 0.0
                ),
                "total": s["total"],
                "successful": s["successful"],
            }
            for rep, s in per_rep.items()
        ]
        ranked.sort(key=lambda x: (-x["success_rate"], -x["total"]))
        best = ranked[0]
        return {
            "rep_name": best["rep"],
            "prior_interactions": 0,
            "success_rate": round(best["success_rate"], 1),
            "recommendation_reason": (
                f"{best['rep']} has the best success rate in {region} "
                f"({round(best['success_rate'], 1)}% over {best['total']} "
                f"interactions). No prior history with {target['name']} "
                "specifically."
            ),
            "source": "region_success_rate",
        }


# ── Tool 3: get_product_recommendations ───────────────────────────────
def _get_product_recommendations(customer_id: int) -> dict[str, Any]:
    with connect_as_caller() as conn:
        target = _get_customer(conn, customer_id)
        target_category = _top_category_for_customer(conn, customer_id)
        target_products = _products_bought_by_customer(conn, customer_id)

        # Similar customers = same top category AND same region.
        candidates = conn.execute(
            "SELECT id FROM customers WHERE id != %s AND region = %s",
            (customer_id, target["region"]),
        ).fetchall()

        similar_ids: list[int] = []
        for row in candidates:
            if _top_category_for_customer(conn, row["id"]) == target_category:
                similar_ids.append(row["id"])

        # Count product purchases across similar customers, excluding
        # anything the target already bought.
        product_info: dict[str, dict[str, Any]] = {}
        for sid in similar_ids:
            rows = conn.execute(
                "SELECT DISTINCT product_name, category "
                "FROM transactions WHERE customer_id = %s",
                (sid,),
            ).fetchall()
            for r in rows:
                if r["product_name"] in target_products:
                    continue
                slot = product_info.setdefault(
                    r["product_name"],
                    {"category": r["category"], "count": 0},
                )
                slot["count"] += 1

        # Look up unit_price from the products catalogue (first match).
        recommendations: list[dict[str, Any]] = []
        total_similar = max(1, len(similar_ids))
        for name, info in product_info.items():
            price_row = conn.execute(
                "SELECT unit_price FROM products WHERE name = %s LIMIT 1",
                (name,),
            ).fetchone()
            share = info["count"] / total_similar
            if share >= 0.5:
                strength = "high"
            elif share >= 0.2:
                strength = "medium"
            else:
                strength = "low"
            recommendations.append(
                {
                    "product_name": name,
                    "category": info["category"],
                    "unit_price": price_row["unit_price"] if price_row else None,
                    "how_many_similar_customers_bought": info["count"],
                    "recommendation_strength": strength,
                }
            )

        recommendations.sort(
            key=lambda r: (-r["how_many_similar_customers_bought"],)
        )

    return {
        "target_customer_id": customer_id,
        "target_customer_name": target["name"],
        "target_region": target["region"],
        "target_top_category": target_category,
        "similar_customer_count": len(similar_ids),
        "recommendations": recommendations,
    }


# ── MCP server wiring ──────────────────────────────────────────────────
mcp = FastMCP(
    "recommendation-server",
    host="127.0.0.1",
    port=RECOMMENDATION_SERVER_PORT,
)


@mcp.tool(
    description=(
        "Find customers similar to a given customer within a specific "
        "segment. Similarity is based on: same segment membership, "
        "same top purchase category (most-bought category from "
        "transactions), and total_spent within ±30% of the target. "
        "Returns the list of similar customer {id, name} pairs, plus "
        "the products those peers bought that the target has not, with "
        "a count of how many peers bought each. Valid segment values: "
        "vip, active, at_risk, inactive, new."
    )
)
def get_similar_customers(customer_id: int, segment: str) -> dict[str, Any]:
    return _get_similar_customers(customer_id, segment)


@mcp.tool(
    description=(
        "Recommend the best sales rep to contact a specific customer. "
        "Scoring: +3 points per prior interaction with this customer, "
        "+2 points per prior successful outcome, +1 point if the rep "
        "also works in the customer's region. If the customer has no "
        "prior interactions at all, falls back to the rep with the "
        "highest success rate across that region. Returns rep_name, "
        "prior_interactions count, success_rate percentage, and a "
        "plain-text recommendation_reason."
    )
)
def get_best_sales_rep(customer_id: int, region: str) -> dict[str, Any]:
    return _get_best_sales_rep(customer_id, region)


@mcp.tool(
    description=(
        "Generate product recommendations for a customer. Finds peers "
        "with the same top purchase category AND same region, collects "
        "every product those peers bought that the target has not, and "
        "ranks by purchase frequency across peers. Returns each "
        "product's name, category, unit_price, the peer-purchase count, "
        "and a recommendation_strength label: high (≥50% of peers), "
        "medium (20-50%), low (<20%)."
    )
)
def get_product_recommendations(customer_id: int) -> dict[str, Any]:
    return _get_product_recommendations(customer_id)


# Record every tool call to agent_audit_log (append-only, RLS-scoped).
audit.instrument(mcp, "recommendation-server")


# ── Entry point — transport chosen by MCP_TRANSPORT ────────────────────
if __name__ == "__main__":
    transport = MCP_TRANSPORT.lower()
    if transport == "http":
        app = wrap_mcp_app(mcp.streamable_http_app())
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=RECOMMENDATION_SERVER_PORT,
            log_level="warning",
        )
    else:
        mcp.run(transport="stdio")
