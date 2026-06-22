"""Quick sanity check for the JWT layer.

Mints a token for Alice, verifies it round-trips, and proves an
expired/tampered token gets rejected.

Run:
    ./venv/bin/python scripts/test_auth.py
"""

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt  # noqa: E402

from auth import extract_bearer, mint_jwt, verify_jwt  # noqa: E402

# Must match data/seed_rbac.py
_NS = uuid.UUID("d6f3e2c1-1234-5678-9abc-def012345678")
ALICE_ID = str(uuid.uuid5(_NS, "alice@northsales.com"))


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def _fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")


def main() -> int:
    print("\nJWT round-trip check\n")

    # 1) Mint + verify
    token = mint_jwt(ALICE_ID, "alice@northsales.com", "sales_rep")
    print(f"  Token (truncated): {token[:30]}...{token[-10:]}")
    claims = verify_jwt(token)
    assert claims["user_id"] == ALICE_ID, "user_id round-trip failed"
    assert claims["role"] == "sales_rep", "role round-trip failed"
    _ok("Mint + verify works; claims intact")

    # 2) Bearer header parse
    header = f"Bearer {token}"
    extracted = extract_bearer(header)
    assert extracted == token, "extract_bearer returned wrong value"
    _ok("Bearer header parse works")

    assert extract_bearer(None) is None
    assert extract_bearer("Basic abc") is None
    assert extract_bearer("Bearer") is None
    _ok("Bearer parse rejects malformed headers")

    # 3) Tampered signature
    bad = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
    try:
        verify_jwt(bad)
        _fail("Tampered token was accepted — security bug")
        return 1
    except jwt.InvalidTokenError:
        _ok("Tampered token rejected")

    # 4) Expired token
    expired = mint_jwt(ALICE_ID, "alice@northsales.com", "sales_rep",
                       ttl_seconds=1)
    time.sleep(2)
    try:
        verify_jwt(expired)
        _fail("Expired token was accepted — security bug")
        return 1
    except jwt.ExpiredSignatureError:
        _ok("Expired token rejected")

    print("\nAll checks passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
