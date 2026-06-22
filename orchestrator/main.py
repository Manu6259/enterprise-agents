"""Orchestrator — entry point.

Discovers every agent listed in config.AGENT_REGISTRY, accepts a
user question, routes it through the available agents, and prints
the combined answer.

Usage (from the enterprise-agents project root):
    python orchestrator/main.py
    python orchestrator/main.py "Full business review and action plans"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── sys.path bootstrap ────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Observability must be initialised BEFORE LangChain / LangGraph imports
# so Traceloop's import-time auto-instrumentation actually patches them.
import observability  # noqa: E402

observability.setup("orchestrator")

from auth import mint_jwt  # noqa: E402
from config import LLM_PROVIDER, MODEL_NAME, OLLAMA_BASE_URL  # noqa: E402
from db import connect  # noqa: E402
from orchestrator.orchestrator import AgentOrchestrator  # noqa: E402


# ── Ollama health checks ──────────────────────────────────────────────
def _ollama_root_url() -> str:
    base = OLLAMA_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _check_ollama_running() -> bool:
    try:
        with urlopen(_ollama_root_url(), timeout=3) as resp:
            resp.read()
        return True
    except (URLError, TimeoutError, OSError):
        return False


def _check_model_available(model_name: str) -> bool:
    url = f"{_ollama_root_url()}/api/tags"
    try:
        with urlopen(Request(url), timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        names = {m.get("name", "") for m in (payload.get("models") or [])}
        return (
            model_name in names
            or f"{model_name}:latest" in names
            or any(n.startswith(model_name) for n in names)
        )
    except Exception:
        return False


# ── Console helpers ──────────────────────────────────────────────────
def _hr(char: str = "═", width: int = 70) -> str:
    return char * width


def _print_header(title: str) -> None:
    print(f"\n{_hr('═')}")
    print(f"\033[1m{title}\033[0m")
    print(_hr("═"))


def _banner(orch: AgentOrchestrator) -> None:
    print("=" * 70)
    print("  Enterprise Agents — Orchestrator")
    print("=" * 70)
    print("  I route your question across the available specialist agents")
    print("  and combine their findings into one answer.")
    print()
    print("  Available agents:")
    if not orch.agents:
        print("    (none — start at least one agent server)")
    for card in orch.agents.values():
        print(f"    • {card.get('display_name', card['name'])}")
        desc = (card.get("description") or "").strip()
        if desc:
            short = desc if len(desc) <= 88 else desc[:88] + "…"
            print(f"      {short}")
    print()
    print("  Type 'exit' or 'quit' to leave.")
    print("=" * 70)


# ── Identity ─────────────────────────────────────────────────────────
def _resolve_user(email: str) -> tuple[str, str, str]:
    """Look up a user by email; return (user_id, email, role).

    Exits with a friendly message if the email isn't seeded.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, role FROM users WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
    if row is None:
        print(f"User '{email}' not found in the users table.")
        print("Seeded users: alice@northsales.com, bob@southsales.com, "
              "carol@eastsales.com, david@westsales.com, "
              "maria@manager.com, root@admin.com")
        sys.exit(1)
    return str(row["id"]), row["email"], row["role"]


# ── Runners ──────────────────────────────────────────────────────────
async def _run_once(question: str, jwt_token: str | None) -> None:
    async with AgentOrchestrator(jwt_token=jwt_token) as orch:
        await orch.discover_agents()
        if not orch.agents:
            print(
                "\nNo agents are available. Start the agent servers, e.g.:\n"
                "  python agents/data_analysis/server.py\n"
                "  python agents/customer_intelligence/server.py\n"
                "  python agents/sales_intelligence/server.py"
            )
            sys.exit(1)

        _print_header(f"QUESTION: {question}")
        answer = await orch.run(question)
        _print_header("COMBINED ANSWER")
        print(answer)
        print(_hr("═"))


async def _run_interactive(jwt_token: str | None) -> None:
    async with AgentOrchestrator(jwt_token=jwt_token) as orch:
        await orch.discover_agents()
        _banner(orch)
        if not orch.agents:
            sys.exit(1)

        while True:
            try:
                question = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not question:
                continue
            if question.lower() in {"exit", "quit"}:
                break
            try:
                _print_header(f"QUESTION: {question}")
                answer = await orch.run(question)
                _print_header("COMBINED ANSWER")
                print(answer)
                print(_hr("═"))
            except Exception as e:
                print(f"\n\033[31m[ERROR]\033[0m {type(e).__name__}: {e}")


# ── Entry point ──────────────────────────────────────────────────────
def main() -> None:
    if LLM_PROVIDER == "ollama":
        if not _check_ollama_running():
            print("Ollama is not running. Start it with: ollama serve")
            sys.exit(1)
        if not _check_model_available(MODEL_NAME):
            print(f"Model {MODEL_NAME} not found. Pull it with: ollama pull {MODEL_NAME}")
            sys.exit(1)
    else:
        if not os.getenv("OPENAI_API_KEY"):
            print("LLM_PROVIDER=openai but OPENAI_API_KEY is not set. "
                  "Export it: export OPENAI_API_KEY=sk-...")
            sys.exit(1)
        print(f"Using OpenAI (model: {MODEL_NAME})")

    # ── Tiny CLI parse — supports `--user <email>` anywhere in argv. ──
    args = sys.argv[1:]
    user_email: str | None = None
    if "--user" in args:
        idx = args.index("--user")
        if idx + 1 >= len(args):
            print("Error: --user requires an email argument")
            sys.exit(1)
        user_email = args[idx + 1]
        # Strip --user + value out so the remaining args are the question
        args = args[:idx] + args[idx + 2:]

    jwt_token: str | None = None
    if user_email:
        user_id, email, role = _resolve_user(user_email)
        jwt_token = mint_jwt(user_id, email, role)
        print(f"[AUTH] Acting as: {email} (role={role}, user_id={user_id})")

        # Tag every trace this run produces with the caller's identity
        # so we can filter "all runs by Alice" in Langfuse with one click.
        # session_id groups every span (orchestrator → agents → MCP) for
        # this single CLI invocation under one trace view.
        try:
            import uuid as _uuid

            from traceloop.sdk import Traceloop

            Traceloop.set_association_properties(
                {
                    "user_id": email,
                    "session_id": _uuid.uuid4().hex,
                    "user_role": role,
                }
            )
        except Exception:
            # Tracing is optional — never let it break the run.
            pass
    else:
        print(
            "[AUTH] No --user supplied. The MCP servers enforce strict auth "
            "— every tool call will be rejected with 401 and the agents "
            "will return empty answers. Re-run with --user <email>, e.g.:\n"
            "       --user alice@northsales.com   (sales_rep, North)\n"
            "       --user bob@southsales.com     (sales_rep, South)\n"
            "       --user maria@manager.com      (manager)\n"
            "       --user root@admin.com         (admin)"
        )

    if args:
        question = " ".join(args).strip()
        asyncio.run(_run_once(question, jwt_token))
    else:
        asyncio.run(_run_interactive(jwt_token))


if __name__ == "__main__":
    main()
