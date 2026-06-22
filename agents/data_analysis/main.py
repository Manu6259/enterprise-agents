"""Data Analysis Agent — entry point.

Performs startup health checks (Ollama reachable, model pulled) and,
when running in HTTP mode, verifies both MCP servers are reachable
before launching the agent. Then either answers a single question
(CLI arg) or drops into an interactive REPL.

Usage:
    # From the enterprise-agents project root:
    python agents/data_analysis/main.py
    python agents/data_analysis/main.py "Top 5 products by revenue?"

    # HTTP mode — connect to pre-running MCP servers
    export MCP_TRANSPORT=http
    python agents/data_analysis/main.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── sys.path bootstrap ────────────────────────────────────────────────
# Make the project root importable so ``from config import ...`` works
# regardless of the current working directory.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))  # up two levels
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agents.data_analysis.agent import DataAnalystAgent  # noqa: E402
from config import (  # noqa: E402
    DATABASE_SERVER_PORT,
    DATABASE_SERVER_URL,
    FILE_SERVER_PORT,
    FILE_SERVER_URL,
    LLM_PROVIDER,
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
    """True if the MCP streamable-HTTP endpoint is reachable.

    FastMCP's streamable-HTTP endpoint returns 4xx to a bare GET (no
    MCP handshake), but a connection refused / DNS error implies the
    server is not running. We treat any HTTP response — even errors —
    as proof the server is up.
    """
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
    print("  Data Analysis Agent — LangGraph + Ollama + MCP")
    print(f"  Transport: {MCP_TRANSPORT.upper()}")
    print("=" * 70)
    print("  I can help you explore the analytics database and write reports.")
    print("  Available data:")
    print("    • sales       — 50 transactions across 12 months")
    print("    • customers   — 30 customers (active, inactive, vip)")
    print("    • products    — 20 products across 3 categories")
    print()
    print("  Example questions:")
    print("    1. What are the top 5 products by total revenue?")
    print("    2. Which region has the highest average order value?")
    print("    3. How many customers are active, inactive, and VIP?")
    print("    4. Which sales rep has generated the most revenue?")
    print("    5. Analyse the sales data and write a full report to file.")
    print()
    print("  Type 'exit' or 'quit' to leave.")
    print("=" * 70)


# ── Loops ─────────────────────────────────────────────────────────────
async def _run_once(question: str) -> None:
    try:
        async with DataAnalystAgent() as agent:
            await agent.ask(question)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"\n\033[31m[ERROR]\033[0m {e}")
        sys.exit(1)


async def _run_interactive() -> None:
    _banner()
    try:
        async with DataAnalystAgent() as agent:
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
        # LLM_PROVIDER=openai — verify the key is set, no local checks needed
        if not os.getenv("OPENAI_API_KEY"):
            print("LLM_PROVIDER=openai but OPENAI_API_KEY is not set.")
            print("Export it: export OPENAI_API_KEY=sk-...")
            sys.exit(1)
        print(f"Using OpenAI (model: {MODEL_NAME})")

    # 3. Transport-specific checks
    if MCP_TRANSPORT.lower() == "http":
        if not _check_http_server(DATABASE_SERVER_URL):
            print("Database server not running. Start it with:")
            print("  MCP_TRANSPORT=http python mcp_servers/database_server.py")
            sys.exit(1)
        if not _check_http_server(FILE_SERVER_URL):
            print("File server not running. Start it with:")
            print("  MCP_TRANSPORT=http python mcp_servers/file_server.py")
            sys.exit(1)
        print(
            f"Running in HTTP mode — connected to MCP servers on ports "
            f"{DATABASE_SERVER_PORT} and {FILE_SERVER_PORT}"
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
