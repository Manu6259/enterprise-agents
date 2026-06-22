"""Standalone MCP Server — SQL Database Operations (Postgres).

Supports TWO transports chosen by the MCP_TRANSPORT env var:
  stdio — run as a subprocess (default, used by the agent in local mode)
  http  — run as a persistent streamable-HTTP server on DATABASE_SERVER_PORT

Tool logic is IDENTICAL in both modes. Only the startup wiring changes.

Tools exposed:
  1. list_tables         — list all user tables (information_schema)
  2. describe_table      — column names + data types for one table
  3. execute_query       — run a SELECT query (mutating statements blocked)
  4. get_summary_stats   — analytical summary of a table

Launch:
    python mcp_servers/database_server.py              # stdio
    MCP_TRANSPORT=http python mcp_servers/database_server.py
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

# Allow this standalone script to import config.py from the project root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import DATABASE_SERVER_PORT, MCP_TRANSPORT  # noqa: E402
import audit  # noqa: E402
from db import connect_as_caller  # noqa: E402
from request_context import wrap_mcp_app  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402
import uvicorn  # noqa: E402


# ── SQL safety ─────────────────────────────────────────────────────────
# Server-side enforcement — the last line of defence before any execution.
# Reject any mutating keyword as the first token AND reject statement
# chaining (semicolons) to block "SELECT 1; DROP TABLE ..." style attacks.
_FORBIDDEN_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|MERGE|GRANT|REVOKE|VACUUM|COPY)\b",
    re.IGNORECASE,
)


def _is_select_only(query: str) -> bool:
    """True if *query* is a single SELECT (or WITH ... SELECT) statement."""
    stripped = query.strip().rstrip(";").strip()
    if not stripped:
        return False
    # No statement chaining
    if ";" in stripped:
        return False
    if _FORBIDDEN_RE.match(stripped):
        return False
    first_token = stripped.split(None, 1)[0].upper()
    return first_token in ("SELECT", "WITH")


# ── Identifier safety ─────────────────────────────────────────────────
# We can't parameterise an identifier (table name) in SQL — psycopg has
# `sql.Identifier` for this, but our table-name inputs are validated
# against the live information_schema before use, so the input set is
# constrained to known table names.
_VALID_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_ident(name: str) -> str:
    """Reject anything that isn't a plain Postgres identifier."""
    if not _VALID_IDENT_RE.match(name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return name


# ── DB helpers (Postgres) ──────────────────────────────────────────────
def _list_tables() -> list[str]:
    with connect_as_caller() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
        return [row["table_name"] for row in rows]


def _describe_table(table_name: str) -> list[dict[str, str]]:
    if table_name not in _list_tables():
        raise ValueError(f"Table '{table_name}' does not exist")
    with connect_as_caller() as conn:
        rows = conn.execute(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "ORDER BY ordinal_position",
            (table_name,),
        ).fetchall()
        return [
            {"column_name": r["column_name"], "data_type": r["data_type"]}
            for r in rows
        ]


def _execute_query(query: str) -> list[dict[str, Any]]:
    if not _is_select_only(query):
        raise ValueError(
            "Only single SELECT (or WITH ... SELECT) queries are permitted."
        )
    with connect_as_caller() as conn:
        rows = conn.execute(query).fetchmany(100)
        return [dict(r) for r in rows]


# Postgres numeric data-type names from information_schema.
_NUMERIC_TYPES = {
    "smallint", "integer", "bigint",
    "decimal", "numeric",
    "real", "double precision",
}


def _get_summary_stats(table_name: str) -> dict[str, Any]:
    if table_name not in _list_tables():
        raise ValueError(f"Table '{table_name}' does not exist")
    safe = _safe_ident(table_name)

    with connect_as_caller() as conn:
        total_rows = conn.execute(
            f"SELECT COUNT(*) AS c FROM {safe}"
        ).fetchone()["c"]

        cols = conn.execute(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "ORDER BY ordinal_position",
            (table_name,),
        ).fetchall()
        total_columns = len(cols)

        column_stats: dict[str, dict[str, float | None]] = {}
        for col in cols:
            if col["data_type"].lower() in _NUMERIC_TYPES:
                col_name = _safe_ident(col["column_name"])
                stats_row = conn.execute(
                    f"SELECT MIN({col_name}) AS min_v, "
                    f"MAX({col_name}) AS max_v, "
                    f"AVG({col_name}) AS avg_v "
                    f"FROM {safe}"
                ).fetchone()
                # AVG over an integer column returns a Decimal — cast for JSON.
                avg_v = stats_row["avg_v"]
                column_stats[col["column_name"]] = {
                    "min": stats_row["min_v"],
                    "max": stats_row["max_v"],
                    "average": float(avg_v) if avg_v is not None else None,
                }

        sample_rows = [
            dict(r)
            for r in conn.execute(f"SELECT * FROM {safe} LIMIT 5").fetchall()
        ]

    return {
        "total_rows": total_rows,
        "total_columns": total_columns,
        "column_stats": column_stats,
        "sample_rows": sample_rows,
    }


# ── MCP server wiring ──────────────────────────────────────────────────
mcp = FastMCP(
    "database-server",
    host="127.0.0.1",
    port=DATABASE_SERVER_PORT,
)


@mcp.tool(
    description=(
        "List the names of all tables in the analytics database. "
        "Always call this first to discover what data is available "
        "before writing any query."
    )
)
def list_tables() -> list[str]:
    return _list_tables()


@mcp.tool(
    description=(
        "Return the schema (column names and Postgres data types) of a "
        "specific table. Call this before writing any query that targets "
        "the table, so you know which columns exist."
    )
)
def describe_table(table_name: str) -> list[dict[str, str]]:
    return _describe_table(table_name)


@mcp.tool(
    description=(
        "Execute a read-only SQL query against the analytics database. "
        "ONLY a single SELECT (or WITH ... SELECT) statement is permitted "
        "— INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, GRANT, "
        "REVOKE, VACUUM, COPY and statement chaining (multiple "
        "semicolon-separated statements) are rejected server-side. "
        "Returns at most 100 rows."
    )
)
def execute_query(query: str) -> list[dict[str, Any]]:
    return _execute_query(query)


@mcp.tool(
    description=(
        "Produce an analytical summary of a table: total row count, "
        "column count, min/max/average for each numeric column, and "
        "the first 5 sample rows. Useful as a quick overview before "
        "diving into queries."
    )
)
def get_summary_stats(table_name: str) -> dict[str, Any]:
    return _get_summary_stats(table_name)


# Record every tool call to agent_audit_log (append-only, RLS-scoped).
audit.instrument(mcp, "database-server")


# ── Entry point — transport chosen by MCP_TRANSPORT ────────────────────
if __name__ == "__main__":
    transport = MCP_TRANSPORT.lower()
    if transport == "http":
        # Manually launch uvicorn so we can wrap the ASGI app with our
        # auth middleware. mcp.run(transport="streamable-http") works
        # but doesn't expose a hook for per-request header inspection.
        app = wrap_mcp_app(mcp.streamable_http_app())
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=DATABASE_SERVER_PORT,
            log_level="warning",
        )
    else:
        # stdio cannot carry per-request HTTP headers — RLS context is
        # always anonymous in stdio mode (local dev convenience only).
        mcp.run(transport="stdio")
