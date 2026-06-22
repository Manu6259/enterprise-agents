"""JWT minting + verification for cross-process identity.

Used to carry the caller's identity from the orchestrator through agents
into MCP tool calls. The token sits in the HTTP ``Authorization: Bearer``
header at every hop — the LLM never sees it.

Design notes:
  * HS256 with a shared secret. Symmetric is fine here because every
    layer (orchestrator, agents, MCP servers) is in the same trust
    boundary and shares the same env. RS256 would matter only if we
    crossed an external trust boundary.
  * The signing secret is the Supabase Secret key from .env. It is
    already an opaque server-only credential, so reusing it avoids
    introducing yet another secret to manage. In production you would
    rotate it (and rotate JWT signing with it).
  * Tokens are short-lived (default 1 hour). Long enough for a single
    multi-agent run; short enough that a leaked token doesn't matter
    by the time anyone notices.
  * We embed user_id + email + role. RLS only needs user_id (it joins
    to the users table for the rest), but carrying role + email lets
    audit logs and error messages stay readable without an extra lookup.
"""

from __future__ import annotations

import time
from typing import Any

import jwt

import config


# Issuer claim — purely cosmetic in our setup, but lets you tell
# our tokens apart from any other JWTs in a debugger.
_ISSUER = "enterprise-agents"

# Default lifetime — a single multi-agent run is seconds; an hour
# leaves plenty of headroom for slow LLM calls without making leaks
# painful.
_DEFAULT_TTL_SECONDS = 3600


def _signing_key() -> str:
    if not config.SUPABASE_SECRET_KEY:
        raise RuntimeError(
            "SUPABASE_SECRET_KEY is not set in .env — required to sign "
            "and verify identity tokens."
        )
    return config.SUPABASE_SECRET_KEY


def mint_jwt(
    user_id: str,
    email: str,
    role: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """Issue a signed token carrying the caller's identity.

    Parameters
    ----------
    user_id
        The user's UUID (matches ``users.id``).
    email, role
        Convenience claims — readable in logs, no extra DB lookup needed.
    ttl_seconds
        How long the token stays valid. Keep short.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": _ISSUER,
        "iat": now,
        "exp": now + ttl_seconds,
        "user_id": str(user_id),
        "email": email,
        "role": role,
    }
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def verify_jwt(token: str) -> dict[str, Any]:
    """Validate signature + expiry; return the claims.

    Raises
    ------
    jwt.ExpiredSignatureError
        Token is past its ``exp``.
    jwt.InvalidIssuerError
        Token's ``iss`` is not ours.
    jwt.InvalidTokenError
        Anything else — bad signature, malformed, missing claims.
    """
    claims = jwt.decode(
        token,
        _signing_key(),
        algorithms=["HS256"],
        issuer=_ISSUER,
        options={"require": ["exp", "iat", "iss", "user_id"]},
    )
    return claims


def extract_bearer(header_value: str | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header.

    Returns ``None`` if the header is missing or malformed — callers
    decide whether that's an error or a fallthrough.
    """
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
