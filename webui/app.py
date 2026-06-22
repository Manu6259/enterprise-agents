"""Local web console for the enterprise-agents stack.

A thin FastAPI app for screen-share demos and recording. It does NOT
re-implement anything — it drives the same in-process
``AgentOrchestrator`` the CLI and eval runner use, mints a JWT for the
selected user exactly like ``orchestrator/main.py``, and streams the
orchestrator's progress to the browser over Server-Sent Events.

Run (stack must already be up — ./scripts/stack.sh start):
    ./venv/bin/python webui/app.py
    # then open http://127.0.0.1:8800

Why SSE and not WebSockets: the data flow is one-directional (server →
browser progress events) and SSE is a few lines with no extra deps.

Why a global run-lock: the orchestrator prints progress to stdout; we
tee stdout into a per-run queue to stream it. One run at a time keeps
those streams from interleaving — fine for a single-presenter demo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys

# ── Path + env bootstrap ──────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# Observability must init BEFORE LangChain/LangGraph are imported so the
# auto-instrumentation patches them — same ordering as the CLI entry point.
import observability  # noqa: E402

observability.setup("webui")

from auth import mint_jwt  # noqa: E402
from db import connect  # noqa: E402
from orchestrator.orchestrator import AgentOrchestrator  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402

app = FastAPI(title="Enterprise Agents — Live Console")

# Only one orchestrator run at a time (see module docstring).
_run_lock = asyncio.Lock()


# ── Identity ──────────────────────────────────────────────────────────
def _list_users() -> list[dict[str, str]]:
    """Return the seeded users for the identity selector."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT email, role, territory FROM users "
            "ORDER BY (role <> 'admin'), (role <> 'manager'), email"
        )
        return [
            {
                "email": r["email"],
                "role": r["role"],
                "territory": r["territory"] or "all regions",
            }
            for r in cur.fetchall()
        ]


def _resolve_user(email: str) -> tuple[str, str, str, str | None]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, role, territory FROM users WHERE email = %s",
            (email,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"User {email!r} not found")
    return str(row["id"]), row["email"], row["role"], row["territory"]


# ── stdout → queue tee (lets us stream orchestrator progress) ─────────
class _QueueTee:
    """File-like object that mirrors writes to the real stdout AND pushes
    completed lines onto an asyncio.Queue for the SSE stream."""

    def __init__(self, queue: asyncio.Queue, real) -> None:
        self._queue = queue
        self._real = real
        self._buf = ""

    def write(self, text: str) -> int:
        self._real.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            # Only forward the orchestrator's structured markers — keeps
            # the activity panel clean (skips multi-line context dumps).
            if line.startswith("["):
                self._queue.put_nowait(line)
        return len(text)

    def flush(self) -> None:
        self._real.flush()


def _classify(line: str) -> dict[str, str]:
    """Turn a raw orchestrator log line into a typed UI event."""
    if line.startswith("[DISCOVERY]"):
        return {"kind": "discovery", "text": line}
    if line.startswith("[ROUTING] Plan:"):
        return {"kind": "plan", "text": line.split("Plan:", 1)[1].strip()}
    if line.startswith("[ROUTING] Reasoning:"):
        return {"kind": "reasoning", "text": line.split("Reasoning:", 1)[1].strip()}
    if line.startswith("[EXECUTE") and "→" in line:
        return {"kind": "agent_start", "text": line.split("→", 1)[1].strip()}
    if line.startswith("[EXECUTE") and "←" in line:
        return {"kind": "agent_done", "text": line.split("←", 1)[1].strip()}
    return {"kind": "log", "text": line}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


# ── Routes ────────────────────────────────────────────────────────────
@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_THIS_DIR, "static", "index.html"))


@app.get("/api/users")
def users() -> list[dict[str, str]]:
    return _list_users()


@app.get("/api/run")
async def run(user: str, q: str) -> StreamingResponse:
    """Stream one orchestrator run as Server-Sent Events."""
    user_id, email, role, territory = _resolve_user(user)
    jwt_token = mint_jwt(user_id, email, role)

    async def event_stream():
        if _run_lock.locked():
            yield _sse({"kind": "error", "text": "Another run is in progress."})
            return

        async with _run_lock:
            yield _sse({
                "kind": "start",
                "email": email,
                "role": role,
                "territory": territory or "all regions",
                "question": q,
            })

            queue: asyncio.Queue = asyncio.Queue()
            answer = ""
            error = None

            async def _do_run():
                nonlocal answer, error
                tee = _QueueTee(queue, sys.stdout)
                try:
                    with contextlib.redirect_stdout(tee):
                        async with AgentOrchestrator(jwt_token=jwt_token) as orch:
                            await orch.discover_agents()
                            if not orch.agents:
                                error = "No agents available — is the stack running?"
                                return
                            answer = await orch.run(q)
                except Exception as e:  # noqa: BLE001
                    error = f"{type(e).__name__}: {e}"
                finally:
                    queue.put_nowait(None)  # sentinel: run finished

            task = asyncio.create_task(_do_run())

            # Drain progress lines until the sentinel arrives.
            while True:
                line = await queue.get()
                if line is None:
                    break
                yield _sse(_classify(line))

            await task
            if error:
                yield _sse({"kind": "error", "text": error})
            else:
                yield _sse({"kind": "answer", "text": answer})
            yield _sse({"kind": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    print("Enterprise Agents console → http://127.0.0.1:8800")
    uvicorn.run(app, host="127.0.0.1", port=8800, log_level="warning")
