"""Data Analysis Agent — A2A HTTP server.

Wraps the existing DataAnalystAgent behind two endpoints:

  GET  /.well-known/agent.json   A2A discovery — returns agent_card.json
  POST /tasks                    {"question": "...", "context": "..."?}
                                 → {"agent": "...", "question": "...",
                                    "answer": "...", "status": "...",
                                    "error": null | "..."}

Agent logic in agent.py and CLI in main.py are NOT modified — this
server is an additive HTTP surface that reuses the same agent class.
Ollama, MCP servers, and every guard keep operating exactly as before.

Usage (from enterprise-agents/):
    python agents/data_analysis/server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── sys.path bootstrap ────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Init BEFORE LangChain/LangGraph imports — Traceloop patches them at import.
import observability  # noqa: E402

observability.setup("data_analysis_agent")

from fastapi import FastAPI, Request as FastAPIRequest  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
import uvicorn  # noqa: E402

from agents.data_analysis.agent import DataAnalystAgent  # noqa: E402
from auth import extract_bearer, verify_jwt  # noqa: E402
from config import (  # noqa: E402
    AGENTS,
    DATA_ANALYSIS_AGENT_PORT,
    LLM_PROVIDER,
    MCP_SERVERS,
    MCP_TRANSPORT,
    MODEL_NAME,
    OLLAMA_BASE_URL,
)


# ── Identity for this server ─────────────────────────────────────────
AGENT_KEY = "data_analysis"
CARD_PATH = os.path.join(_THIS_DIR, "agent_card.json")
PORT = DATA_ANALYSIS_AGENT_PORT


# ── Ollama + MCP health checks (same logic as main.py) ────────────────
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


def _check_http_server(url: str) -> bool:
    try:
        with urlopen(Request(url, method="GET"), timeout=3) as resp:
            resp.read(1)
        return True
    except URLError as e:
        return getattr(e, "code", None) is not None
    except (TimeoutError, OSError):
        return False


def _preflight() -> None:
    """Abort with a clear message if the environment is not ready."""
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

    if MCP_TRANSPORT.lower() == "http":
        required = AGENTS[AGENT_KEY]["servers"]
        for server_name in required:
            spec = MCP_SERVERS[server_name]
            if not _check_http_server(spec["url"]):
                rel = os.path.relpath(spec["script"], _PROJECT_ROOT)
                print(f"{server_name.title()} server not running. Start it with:")
                print(f"  MCP_TRANSPORT=http python {rel}")
                sys.exit(1)


# ── Load the agent card once at import ────────────────────────────────
with open(CARD_PATH, "r", encoding="utf-8") as _f:
    AGENT_CARD: dict = json.load(_f)

AGENT_NAME: str = AGENT_CARD["name"]


# ── Request / response models ────────────────────────────────────────
class TaskRequest(BaseModel):
    question: str = Field(..., description="The user's question for the agent.")
    context: Optional[str] = Field(
        default=None,
        description=(
            "Optional prior context. If supplied it is prepended to the "
            "question before the agent runs, so the agent can see "
            "findings produced by an earlier agent."
        ),
    )


class TaskResponse(BaseModel):
    agent: str
    question: str
    answer: str
    status: str  # "completed" or "failed"
    error: Optional[str] = None


# ── Agent lifecycle + request serialisation ──────────────────────────
_agent: Optional[DataAnalystAgent] = None
_inference_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the agent (and its MCP subprocesses) once per process."""
    global _agent
    _preflight()
    _agent = DataAnalystAgent()
    await _agent.start()
    try:
        yield
    finally:
        if _agent is not None:
            await _agent.stop()
            _agent = None


app = FastAPI(title=AGENT_CARD["display_name"], lifespan=lifespan)


# ── Endpoints ────────────────────────────────────────────────────────
@app.get("/.well-known/agent.json")
async def agent_card() -> dict:
    """A2A discovery — return this agent's card verbatim."""
    return AGENT_CARD


@app.post("/tasks", response_model=TaskResponse)
async def run_task(req: TaskRequest, request: FastAPIRequest) -> TaskResponse:
    """Run the agent on the incoming question and return the answer.

    Identity flow:
      * If the caller sent ``Authorization: Bearer <jwt>``, we verify
        the token and forward it to the agent — which propagates it to
        every MCP HTTP call so RLS in Postgres can filter rows.
      * If the header is missing, the agent runs unauthenticated and
        every downstream DB connection bypasses RLS (Day 1 behaviour).
      * If the header is present but the token is bad, we fail fast
        with status='failed' — better to surface auth errors than
        silently fall through to an unauthenticated run.

    Never crashes — every failure path returns status='failed'.
    """
    if _agent is None:
        return TaskResponse(
            agent=AGENT_NAME,
            question=req.question,
            answer="",
            status="failed",
            error="Agent is not initialised",
        )

    jwt_token = extract_bearer(request.headers.get("authorization"))
    if jwt_token is not None:
        try:
            claims = verify_jwt(jwt_token)
            print(f"[AUTH] {AGENT_KEY} acting as "
                  f"{claims.get('email')} (role={claims.get('role')})",
                  flush=True)
        except Exception as e:
            return TaskResponse(
                agent=AGENT_NAME,
                question=req.question,
                answer="",
                status="failed",
                error=f"Invalid auth token: {type(e).__name__}: {e}",
            )

    prompt = (
        f"Prior context:\n{req.context}\n\nQuestion: {req.question}"
        if req.context
        else req.question
    )

    async with _inference_lock:
        try:
            answer = await _agent.ask(prompt, jwt_token=jwt_token)
            return TaskResponse(
                agent=AGENT_NAME,
                question=req.question,
                answer=answer or "",
                status="completed",
            )
        except Exception as e:
            return TaskResponse(
                agent=AGENT_NAME,
                question=req.question,
                answer="",
                status="failed",
                error=f"{type(e).__name__}: {e}",
            )


# ── Entry point ───────────────────────────────────────────────────────
def main() -> None:
    print(f"{AGENT_CARD['display_name']} server running on port {PORT}")
    print(f"Agent card:    http://127.0.0.1:{PORT}/.well-known/agent.json")
    print(f"Task endpoint: http://127.0.0.1:{PORT}/tasks")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
