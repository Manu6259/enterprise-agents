"""LangGraph ReAct agent that drives the data analyst session.

Connects to two MCP servers (database + file system) via
MultiServerMCPClient. The transport mode is chosen by the MCP_TRANSPORT
env variable surfaced through config.py:

  stdio — MultiServerMCPClient spawns each server as a subprocess
  http  — MultiServerMCPClient connects to already-running HTTP servers

The agent is the same in both modes — only the client connection config
differs. No if/else scattered across the codebase; a single helper
function returns the correct connection dict.

Built on ``langgraph.prebuilt.create_react_agent`` — the library
wires START → agent → tools_condition → tools → agent internally,
binds the tools to the LLM, and handles tool-call / tool-result
message plumbing. We pass a pre-configured ToolNode so we can keep
our custom ``handle_tool_errors`` callable that feeds error text
back to the model for self-correction.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import ToolNode, create_react_agent

from config import (
    AGENT_RECURSION_LIMIT,
    DATABASE_SERVER_SCRIPT,
    DATABASE_SERVER_URL,
    FILE_SERVER_SCRIPT,
    FILE_SERVER_URL,
    MCP_TRANSPORT,
    MODEL_NAME,
    OLLAMA_API_KEY,
    OLLAMA_BASE_URL,
)


from prompts import load_prompt

# Bump to "v2" (and add prompts/data_analysis/v2.md) to A/B-test a change.
PROMPT_VERSION = "v1"
SYSTEM_PROMPT = load_prompt("data_analysis", PROMPT_VERSION)


# ── MCP client config ─────────────────────────────────────────────────
def _build_mcp_client_config(
    jwt_token: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the MultiServerMCPClient connection dict for the current transport.

    If ``jwt_token`` is provided, every HTTP-transport server gets an
    ``Authorization: Bearer <token>`` header so the MCP server can
    extract the caller's identity for RLS enforcement.
    """
    auth_headers = (
        {"Authorization": f"Bearer {jwt_token}"} if jwt_token else None
    )

    if MCP_TRANSPORT.lower() == "http":
        database_cfg: dict[str, Any] = {
            "url": DATABASE_SERVER_URL,
            "transport": "streamable_http",
        }
        file_cfg: dict[str, Any] = {
            "url": FILE_SERVER_URL,
            "transport": "streamable_http",
        }
        if auth_headers:
            database_cfg["headers"] = auth_headers
            file_cfg["headers"] = auth_headers
        return {"database": database_cfg, "file": file_cfg}

    # stdio: MultiServerMCPClient spawns both servers as subprocesses.
    # Inherit the current environment and force MCP_TRANSPORT=stdio so
    # the children start in stdio mode regardless of what the parent had.
    # stdio cannot carry per-request headers; agents using stdio always
    # run unauthenticated (intended for local dev only).
    stdio_env = {**os.environ, "MCP_TRANSPORT": "stdio"}
    return {
        "database": {
            "command": sys.executable,
            "args": [DATABASE_SERVER_SCRIPT],
            "env": stdio_env,
            "transport": "stdio",
        },
        "file": {
            "command": sys.executable,
            "args": [FILE_SERVER_SCRIPT],
            "env": stdio_env,
            "transport": "stdio",
        },
    }


# ── Console formatting ────────────────────────────────────────────────
def _hr(char: str = "─", width: int = 70) -> str:
    return char * width


def _print_reasoning(content: str) -> None:
    if not content:
        return
    print(f"\n\033[36m[REASONING]\033[0m {content.strip()}")


def _print_tool_call(name: str, args: dict[str, Any]) -> None:
    pretty_args = {
        k: (v if not isinstance(v, str) or len(v) < 120 else v[:120] + "…")
        for k, v in args.items()
    }
    print(
        f"\n\033[33m[TOOL CALL]\033[0m {name}("
        f"{json.dumps(pretty_args, default=str)})"
    )


def _print_tool_result(name: str, content: str) -> None:
    display = content if len(content) < 500 else content[:500] + "\n… (truncated)"
    print(f"\033[32m[TOOL RESULT]\033[0m {name} →\n{display}")


def _print_final(content: str) -> None:
    print(f"\n{_hr('═')}")
    print("\033[1m[FINAL ANSWER]\033[0m")
    print(_hr('═'))
    print(content)
    print(_hr('═'))


# ── Core agent ────────────────────────────────────────────────────────
def _format_tool_error(exc: Exception) -> str:
    return (
        f"Tool call failed: {exc}. "
        "Fix the arguments (check types and required fields) "
        "and call the tool again."
    )


class DataAnalystAgent:
    """Wraps a LangGraph agent plus a per-request MCP client.

    The LLM is built once at startup; the MCP client + tool list + graph
    are rebuilt on every ``ask()`` so the caller's JWT can be threaded
    through to each MCP server via the ``Authorization`` header.
    """

    def __init__(self) -> None:
        self._llm: ChatOpenAI | None = None

    async def start(self) -> None:
        # Only the LLM is stateless across requests — keep it warm.
        self._llm = ChatOpenAI(
            model=MODEL_NAME,
            base_url=OLLAMA_BASE_URL,
            api_key=OLLAMA_API_KEY,
            temperature=0.0,
        )

    async def stop(self) -> None:
        self._llm = None

    async def __aenter__(self) -> "DataAnalystAgent":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    async def ask(self, question: str, jwt_token: str | None = None) -> str:
        """Run one question through the agent. Streams events to stdout."""
        if self._llm is None:
            raise RuntimeError("Agent not started — call start() first.")

        # Per-request: build a fresh MCP client + graph with this
        # caller's JWT in the Authorization header so the MCP servers
        # can identify the user for RLS.
        client = MultiServerMCPClient(
            _build_mcp_client_config(jwt_token=jwt_token),
            tool_name_prefix=True,
        )
        tools = await client.get_tools()
        graph = create_react_agent(
            self._llm,
            ToolNode(tools, handle_tool_errors=_format_tool_error),
            prompt=SYSTEM_PROMPT,
            name="data_analysis_agent",
        )

        print(f"\n{_hr('═')}")
        print(f"\033[1m[QUESTION]\033[0m {question}")
        print(_hr('═'))

        input_messages = [HumanMessage(content=question)]
        final_content = ""

        try:
            async for event in graph.astream(
                {"messages": input_messages},
                stream_mode="updates",
                config={"recursion_limit": AGENT_RECURSION_LIMIT},
            ):
                for _node, update in event.items():
                    messages = update.get("messages", []) if isinstance(update, dict) else []
                    for msg in messages:
                        if isinstance(msg, AIMessage):
                            if isinstance(msg.content, str) and msg.content.strip():
                                _print_reasoning(msg.content)
                                final_content = msg.content
                            for tc in (msg.tool_calls or []):
                                _print_tool_call(tc.get("name", "?"), tc.get("args", {}))
                        elif isinstance(msg, ToolMessage):
                            _print_tool_result(
                                msg.name or "tool",
                                msg.content if isinstance(msg.content, str) else str(msg.content),
                            )
        except GraphRecursionError:
            stub = (
                f"(agent hit recursion limit of {AGENT_RECURSION_LIMIT} steps — "
                "returning partial findings)"
            )
            print(f"\n\033[33m[RECURSION LIMIT]\033[0m {stub}")
            final_content = (
                f"{final_content}\n\n{stub}" if final_content else stub
            )

        _print_final(final_content or "(no textual answer produced)")
        return final_content
