"""Sales Intelligence Agent — entry point.

Performs startup health checks (Ollama reachable, model pulled) and,
when running in HTTP mode, verifies every MCP server this agent
consumes (database, scoring, recommendation, outreach) is reachable
before launching the agent. Then either answers a single question
(CLI arg) or drops into an interactive REPL.

Usage:
    # From the enterprise-agents project root:
    python agents/sales_intelligence/main.py
    python agents/sales_intelligence/main.py "Who should we save first?"

    # HTTP mode — connect to pre-running MCP servers
    export MCP_TRANSPORT=http
    python agents/sales_intelligence/main.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── sys.path bootstrap ────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agents.sales_intelligence.agent import (  # noqa: E402
    AGENT_NAME,
    SalesIntelligenceAgent,
)
from config import (  # noqa: E402
    AGENTS,
    LLM_PROVIDER,
    MCP_SERVERS,
    MCP_TRANSPORT,
    MODEL_NAME,
    OLLAMA_BASE_URL,
)


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
            import json

            payload = json.loads(resp.read().decode("utf-8"))
        models = payload.get("models", []) or []
        names = {m.get("name", "") for m in models}
        return (
            model_name in names
            or f"{model_name}:latest" in names
            or any(n.startswith(model_name) for n in names)
        )
    except Exception:
        return False


# ── MCP server health checks (HTTP mode only) ─────────────────────────
def _check_http_server(url: str) -> bool:
    try:
        with urlopen(Request(url, method="GET"), timeout=3) as resp:
            resp.read(1)
        return True
    except URLError as e:
        return getattr(e, "code", None) is not None
    except (TimeoutError, OSError):
        return False


# ── Banner ────────────────────────────────────────────────────────────
def _banner() -> None:
    print("=" * 70)
    print("  Sales Intelligence Agent — LangGraph + Ollama + MCP")
    print(f"  Transport: {MCP_TRANSPORT.upper()}")
    print("=" * 70)
    print("  I turn customer risk findings into concrete sales action plans.")
    print()
    print("  Example questions:")
    print("    1. Which at-risk customers should we prioritise and what is")
    print("       the action plan for each?")
    print("    2. Build a complete action plan for customer X.")
    print("    3. What products should we offer customer X, and which rep")
    print("       should make the call?")
    print("    4. Produce action plans for the top 3 platinum customers")
    print("       who are at churn risk.")
    print()
    print("  Type 'exit' or 'quit' to leave.")
    print("=" * 70)


# ── Loops ─────────────────────────────────────────────────────────────
async def _run_once(question: str) -> None:
    try:
        async with SalesIntelligenceAgent() as agent:
            await agent.ask(question)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"\n\033[31m[ERROR]\033[0m {e}")
        sys.exit(1)


async def _run_interactive() -> None:
    _banner()
    try:
        async with SalesIntelligenceAgent() as agent:
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
                    await agent.ask(question)
                except Exception as e:
                    print(f"\n\033[31m[ERROR]\033[0m {e}")
    except (FileNotFoundError, RuntimeError) as e:
        print(f"\n\033[31m[ERROR]\033[0m {e}")
        sys.exit(1)


# ── Entry point ────────────────────────────────────────────────────────
def main() -> None:
    # 1-2. LLM provider health checks
    if LLM_PROVIDER == "ollama":
        if not _check_ollama_running():
            print("Ollama is not running. Start it with:")
            print("  ollama serve")
            sys.exit(1)
        if not _check_model_available(MODEL_NAME):
            print(f"Model {MODEL_NAME} not found. Pull it with:")
            print(f"  ollama pull {MODEL_NAME}")
            sys.exit(1)
    else:
        if not os.getenv("OPENAI_API_KEY"):
            print("LLM_PROVIDER=openai but OPENAI_API_KEY is not set.")
            print("Export it: export OPENAI_API_KEY=sk-...")
            sys.exit(1)
        print(f"Using OpenAI (model: {MODEL_NAME})")

    # 3. Transport-specific checks — iterate AGENTS registry
    if MCP_TRANSPORT.lower() == "http":
        required = AGENTS[AGENT_NAME]["servers"]
        for server_name in required:
            spec = MCP_SERVERS[server_name]
            if not _check_http_server(spec["url"]):
                print(
                    f"{server_name.title()} server not running. Start it with:"
                )
                script_rel = os.path.relpath(spec["script"], _PROJECT_ROOT)
                print(f"  MCP_TRANSPORT=http python {script_rel}")
                sys.exit(1)
        ports = ", ".join(str(MCP_SERVERS[s]["port"]) for s in required)
        print(
            f"Running in HTTP mode — connected to MCP servers on ports {ports}"
        )
    else:
        print("Running in stdio mode — MCP servers managed automatically")

    # 4. One-shot vs interactive
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:]).strip()
        asyncio.run(_run_once(question))
    else:
        asyncio.run(_run_interactive())


if __name__ == "__main__":
    main()
