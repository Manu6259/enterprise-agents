"""Tool-call audit logging.

Every MCP tool invocation is recorded to the ``agent_audit_log`` table:
who (the verified caller's user_id), what tool, with what arguments, the
outcome, and a truncated result summary. The table has append-only RLS
(no UPDATE or DELETE policy), so the trail cannot be altered after the
fact — not by a sales rep, not by an agent, not by a compromised prompt.

Wiring: ``instrument(mcp, server_name)`` wraps the FastMCP tool manager's
``call_tool`` once per server. Every tool on that server is then audited
without touching the individual tool functions. The audit write is
best-effort — if it fails, the tool result is still returned (auditing
must never break the request).

The write runs inside the same request context as the tool call, so it
picks up the caller's identity from the contextvar set by
``request_context.AuthContextMiddleware`` and inserts as the
``authenticated`` role — meaning the row lands under RLS, scoped to the
caller, exactly like every other write in the system.
"""

from __future__ import annotations

import json
from typing import Any

_RESULT_SUMMARY_CAP = 500


def _summarise(result: Any) -> str:
    """Render a tool result to a short, storable string."""
    try:
        text = result if isinstance(result, str) else json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    return text[:_RESULT_SUMMARY_CAP]


def _record(
    server_name: str,
    tool_name: str,
    tool_args: dict[str, Any],
    status: str,
    result_summary: str,
) -> None:
    """Insert one audit row scoped to the current caller.

    Best-effort: any failure here is swallowed so it can never break the
    tool call that triggered it. Skipped entirely when there is no
    identified caller (e.g. stdio dev mode) — an anonymous audit row
    carries no security value.
    """
    # Local imports keep this module import-light and avoid circular deps.
    from db import connect_as_caller
    from request_context import get_user_id

    if get_user_id() is None:
        return

    try:
        with connect_as_caller() as conn:
            conn.execute(
                "INSERT INTO agent_audit_log "
                "(user_id, agent_name, tool_name, tool_args, "
                " result_summary, status) "
                "VALUES (current_user_id(), %s, %s, %s, %s, %s)",
                (
                    server_name,
                    tool_name,
                    json.dumps(tool_args, default=str),
                    result_summary,
                    status,
                ),
            )
    except Exception as e:  # noqa: BLE001 — auditing must never raise
        print(f"[AUDIT] failed to record {tool_name}: {type(e).__name__}: {e}",
              flush=True)


def instrument(mcp: Any, server_name: str) -> None:
    """Wrap *mcp*'s tool manager so every tool call is audited.

    Call once, after all tools are registered, before the server starts.
    """
    tm = mcp._tool_manager
    original_call_tool = tm.call_tool

    async def audited_call_tool(name, arguments, *args, **kwargs):
        try:
            result = await original_call_tool(name, arguments, *args, **kwargs)
        except Exception as e:  # tool raised — record the failure, re-raise
            _record(server_name, name, arguments or {}, "error",
                    f"{type(e).__name__}: {e}"[:_RESULT_SUMMARY_CAP])
            raise
        _record(server_name, name, arguments or {}, "success",
                _summarise(result))
        return result

    tm.call_tool = audited_call_tool
