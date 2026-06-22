"""Sales Outreach Agent — the WRITE side of the HITL pipeline.

This agent does ONE thing: take an action plan produced upstream by the
sales-intelligence-agent (passed in as prior context) and persist it
into the outreach_drafts queue via the ``submit_draft`` MCP tool. Every
draft lands with status='pending' and waits for a manager to approve via
``scripts/review_drafts.py`` before anything is sent.

Why a separate agent (not a rule inside sales-intelligence):
  Capability separation is **architectural**, not prompt-based. The
  router decides whether to invoke this agent based on the user's
  intent. If it doesn't get invoked, no draft is created — there is no
  prompt rule the LLM could "forget" or be tricked into ignoring.

MCP servers consumed: outreach (one server, one needed tool).
"""

from __future__ import annotations

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
    AGENTS,
    MCP_SERVERS,
    MCP_TRANSPORT,
    MODEL_NAME,
    OLLAMA_API_KEY,
    OLLAMA_BASE_URL,
)


AGENT_NAME = "sales_outreach"

from prompts import load_prompt

PROMPT_VERSION = "v1"
SYSTEM_PROMPT = load_prompt("sales_outreach", PROMPT_VERSION)


def _build_mcp_client_config(
    jwt_token: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Registry-driven config; carries JWT via Authorization header."""
    server_names = AGENTS[AGENT_NAME]["servers"]
    is_http = MCP_TRANSPORT.lower() == "http"
    auth_headers = (
        {"Authorization": f"Bearer {jwt_token}"} if jwt_token else None
    )

    stdio_env = {**os.environ, "MCP_TRANSPORT": "stdio"}

    client_config: dict[str, dict[str, Any]] = {}
    for name in server_names:
        spec = MCP_SERVERS[name]
        if is_http:
            entry: dict[str, Any] = {
                "url": spec["url"],
                "transport": "streamable_http",
            }
            if auth_headers:
                entry["headers"] = auth_headers
            client_config[name] = entry
        else:
            client_config[name] = {
                "command": sys.executable,
                "args": [spec["script"]],
                "env": stdio_env,
                "transport": "stdio",
            }
    return client_config


def _hr(char: str = "─", width: int = 70) -> str:
    return char * width


def _print_reasoning(content: str) -> None:
    if not content:
        return
    print(f"\n\033[36m[REASONING]\033[0m {content.strip()}")


def _print_tool_call(name: str, args: dict[str, Any]) -> None:
    import json as _json
    pretty_args = {
        k: (v if not isinstance(v, str) or len(v) < 120 else v[:120] + "…")
        for k, v in args.items()
    }
    print(
        f"\n\033[33m[TOOL CALL]\033[0m {name}("
        f"{_json.dumps(pretty_args, default=str)})"
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


def _format_tool_error(exc: Exception) -> str:
    return (
        f"Tool call failed: {exc}. "
        "Do not retry blindly — read the error, fix the argument, and "
        "call again only if the error is recoverable. If the error is "
        "an RLS violation, report it verbatim and stop."
    )


class SalesOutreachAgent:
    """One-shot drafting agent: prior plan in → pending draft out.

    LLM warm; MCP client + graph rebuilt per request so the JWT is
    threaded into the Authorization header on the outreach MCP call.
    """

    def __init__(self) -> None:
        self._llm: ChatOpenAI | None = None

    async def start(self) -> None:
        self._llm = ChatOpenAI(
            model=MODEL_NAME,
            base_url=OLLAMA_BASE_URL,
            api_key=OLLAMA_API_KEY,
            temperature=0.0,
        )

    async def stop(self) -> None:
        self._llm = None

    async def __aenter__(self) -> "SalesOutreachAgent":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    async def ask(self, question: str, jwt_token: str | None = None) -> str:
        if self._llm is None:
            raise RuntimeError("Agent not started — call start() first.")

        client = MultiServerMCPClient(
            _build_mcp_client_config(jwt_token=jwt_token),
            tool_name_prefix=True,
        )
        tools = await client.get_tools()
        graph = create_react_agent(
            self._llm,
            ToolNode(tools, handle_tool_errors=_format_tool_error),
            prompt=SYSTEM_PROMPT,
            name="sales_outreach_agent",
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
