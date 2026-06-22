"""Shared Postgres connection helper.

Two modes:

  connect()                 — owner connection (postgres role). Bypasses
                              RLS. Used by seed scripts + admin tooling.

  connect(user_id="...")    — switches to the `authenticated` role and
                              stashes the caller's UUID in
                              `app.current_user_id` so RLS policies can
                              filter on it. Used by MCP tools when an
                              identified user is making the request.

Usage:
    from db import connect

    # Admin (no RLS):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM customers")

    # As Alice (RLS applies):
    with connect(user_id=alice_uuid) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM customers")  # Only North rows
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

import config


@contextmanager
def connect(user_id: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a Postgres connection that returns rows as dicts.

    Parameters
    ----------
    user_id
        If provided, the connection switches to the `authenticated`
        role and sets `app.current_user_id` so RLS policies apply.
        If omitted, the connection stays as the table owner and
        bypasses RLS (intended for seed scripts and admin tooling).
    """
    if not config.SUPABASE_DATABASE_URL:
        raise RuntimeError(
            "SUPABASE_DATABASE_URL is not set. Copy .env.example to .env "
            "and fill in the Session Pooler connection string."
        )
    conn = psycopg.connect(
        config.SUPABASE_DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
    )
    try:
        if user_id is not None:
            # Postgres `SET` doesn't accept parameter placeholders, so we
            # use set_config() (third arg `true` = LOCAL, transaction-
            # scoped — discarded on commit/rollback). Order matters:
            # set the GUC *before* switching role, so the value lands
            # while we're still the owner.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_user_id', %s, true)",
                    (str(user_id),),
                )
                cur.execute("SET LOCAL ROLE authenticated")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def connect_as_caller() -> Iterator[psycopg.Connection]:
    """Open a connection scoped to the *current MCP caller*.

    Reads the per-request user_id stashed by
    ``request_context.AuthContextMiddleware`` and delegates to
    ``connect()``. When called outside a middleware-served request
    (e.g. tests, scripts) the contextvar is None and we get an
    owner-bypass connection — same as plain ``connect()``.

    MCP tool implementations should use this helper rather than
    ``connect()`` so they automatically pick up RLS context.
    """
    # Local import — request_context imports auth which imports config,
    # circular-import-safe even though db is imported widely.
    from request_context import get_user_id

    with connect(user_id=get_user_id()) as conn:
        yield conn
