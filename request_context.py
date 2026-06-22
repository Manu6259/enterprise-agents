"""Per-request identity for MCP servers.

Flow:
  1. Agent calls MCP tool over HTTP with ``Authorization: Bearer <jwt>``.
  2. ``AuthContextMiddleware`` runs first, extracts + verifies the JWT,
     and stores the caller's ``user_id`` in a ``ContextVar``.
  3. The MCP tool runs in the same async task — it reads the contextvar
     (via ``get_user_id()``) and passes it to ``db.connect_as_caller``.
  4. Postgres' RLS policies filter on the resulting connection.

Why a ContextVar (not a thread-local):
  Starlette / FastMCP handle requests in an asyncio task per request.
  ContextVar values stick to the task, so concurrent requests stay
  isolated without any explicit per-request state object.
"""

from __future__ import annotations

import contextvars
import sys

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from auth import extract_bearer, verify_jwt


# Per-request slot. Default ``None`` → caller is anonymous → DB calls
# bypass RLS (Day 1 behaviour, preserved when no JWT is sent).
_user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_caller_user_id", default=None
)


def set_user_id(user_id: str | None) -> contextvars.Token:
    """Stash the caller's user_id for this async task. Returns a token
    so ``reset_user_id(token)`` can undo it (used by the middleware).
    """
    return _user_id_var.set(user_id)


def get_user_id() -> str | None:
    """Read the current caller's user_id, or None if unauthenticated."""
    return _user_id_var.get()


def reset_user_id(token: contextvars.Token) -> None:
    """Restore the previous user_id slot (paired with set_user_id)."""
    _user_id_var.reset(token)


class AuthContextMiddleware(BaseHTTPMiddleware):
    """ASGI middleware: per-request JWT verification → contextvar set.

    Behaviour:
      * No Authorization header → contextvar stays None → tool runs
        unauthenticated (bypasses RLS — same as Day 1).
      * Header present + token valid → contextvar set to user_id from
        claims, tool runs with RLS enforcement.
      * Header present + token invalid → 401 response, tool never runs.

    The fail-fast on bad tokens is deliberate. If the caller went to
    the trouble of attaching a token, silently dropping back to
    unauthenticated would mask a real bug — better to surface it.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        token = extract_bearer(request.headers.get("authorization"))

        if token is None:
            # Strict mode: anonymous traffic is rejected at the edge.
            # Seed scripts and admin tooling don't go through MCP — they
            # call db.connect() directly as the owner role, so they are
            # unaffected by this gate.
            return JSONResponse(
                {"error": "Missing Authorization header"},
                status_code=401,
            )

        try:
            claims = verify_jwt(token)
        except Exception as e:
            return JSONResponse(
                {"error": f"Invalid auth token: {type(e).__name__}: {e}"},
                status_code=401,
            )

        user_id = claims.get("user_id")
        ctx_token = set_user_id(user_id)
        # Surface in the MCP server log so we can see identity flowing.
        print(
            f"[AUTH] MCP request from user_id={user_id} "
            f"(email={claims.get('email')}, role={claims.get('role')})",
            file=sys.stderr,
            flush=True,
        )
        try:
            return await call_next(request)
        finally:
            reset_user_id(ctx_token)


def wrap_mcp_app(mcp_app: ASGIApp) -> ASGIApp:
    """Convenience: attach the auth middleware to a FastMCP ASGI app.

    Usage in an MCP server's __main__ block:

        app = mcp.streamable_http_app()
        app = wrap_mcp_app(app)
        uvicorn.run(app, host="127.0.0.1", port=PORT)
    """
    # add_middleware mutates and returns None; we return the app for
    # a fluent one-liner at the call site.
    mcp_app.add_middleware(AuthContextMiddleware)
    return mcp_app
