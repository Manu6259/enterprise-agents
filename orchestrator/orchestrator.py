"""Multi-agent orchestrator for the enterprise-agents project.

Reads A2A agent cards from every URL in ``config.AGENT_REGISTRY``,
uses the local Ollama LLM to pick which agents to call, runs them
sequentially over HTTP while passing context forward, and asks the
LLM to synthesise the final combined answer.

The orchestrator never talks to MCP servers directly — it only talks
to agent HTTP task endpoints.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from config import (
    AGENT_REGISTRY,
    MODEL_NAME,
    OLLAMA_API_KEY,
    OLLAMA_BASE_URL,
)


# ── Prompts ───────────────────────────────────────────────────────────
ROUTING_SYSTEM = """You are a routing controller for a team of specialist agents.
You must read the user question and the descriptions of the available agents,
and decide which agents to call and in what order.

Rules:
- Only name agents from the provided list.
- Prefer the minimum number of agents needed to answer the question.
- Order matters: earlier agents feed context to later agents.
  * Business/data overview questions → data-analysis-agent first.
  * Customer-risk or segmentation questions → customer-intelligence-agent.
  * "What do we do about it" / action-plan questions → sales-intelligence-agent.
- CAPABILITY SEPARATION — read-vs-write:
  * sales-intelligence-agent is READ-ONLY. It produces plans as text.
    It does NOT persist or send anything.
  * sales-outreach-agent is the WRITE side. It takes an upstream plan
    and submits it to the human approval queue (status='pending').
  * Add sales-outreach-agent to the chain ONLY when the user's wording
    implies acting on the customer: "submit", "prepare outreach",
    "queue for sending", "draft an email and submit", "create a
    pending draft", etc. When in doubt, do NOT add it — false-positive
    drafts pollute the manager's review queue.
  * Pure analytical wording — "analyse", "what's the risk", "explain",
    "should we contact" (asking opinion) — MUST NOT trigger
    sales-outreach-agent.
  * When you do include sales-outreach-agent, it MUST come AFTER
    sales-intelligence-agent in the order so it has a plan to act on.
- Output STRICT JSON only — no prose, no markdown, no code fences, no comments.

Respond with this exact JSON shape:
{"agents": ["agent-name-1", "agent-name-2"], "reasoning": "one short sentence", "sequential": true}
"""

ASSEMBLY_SYSTEM = """You are a senior analyst consolidating findings from specialist agents.
Given the user question and each agent's raw contribution, produce ONE coherent
final answer.

Rules:
- Do not invent data — only use facts from the agent contributions below.
- Structure the answer with clearly labelled sections, one per contributing agent,
  in the order they ran. Use the agent's display name as the section heading.
- Finish with a short "Combined Takeaway" section (2-4 sentences) that stitches
  the findings together and answers the original question directly.
- If an agent's contribution was empty or failed, note that briefly under its
  section rather than omitting it.
"""

HANDOFF_SYSTEM = """You are a hand-off summariser for a multi-agent pipeline.
Given one analyst's narrative output, distill it into a compact fact sheet so
that the next analyst downstream can act on it without re-reading the prose.

Rules:
- Output 3-6 terse bullet points ONLY. No preamble. No closing summary.
- Preserve every number, ID, name, percentage, and status label exactly.
- Omit generic advice, prose recommendations, and filler phrases.
- Each bullet should be one line, factual, under 25 words.
- If the input contains no useful facts, output a single bullet: "- (no actionable findings produced)"
"""


# ── JSON extraction (tolerant of markdown fences / preamble) ──────────
def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    candidates: list[str] = [text.strip()]

    # Markdown code fence, with or without a language tag.
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if m:
        candidates.append(m.group(1).strip())

    # First {...} block — greedy to max depth.
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidates.append(m.group(0))

    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


# ── Main class ────────────────────────────────────────────────────────
class AgentOrchestrator:
    """Discovers agents, routes questions, runs agents in sequence,
    and synthesises the final answer.
    """

    def __init__(self, jwt_token: str | None = None) -> None:
        self._llm = ChatOpenAI(
            model=MODEL_NAME,
            base_url=OLLAMA_BASE_URL,
            api_key=OLLAMA_API_KEY,
            temperature=0.0,
        )
        # Keyed by card["name"], e.g. "data-analysis-agent"
        self.agents: dict[str, dict[str, Any]] = {}
        # When a JWT is provided, every agent /tasks call goes out with
        # an Authorization: Bearer header. Discovery (GET agent_card)
        # stays unauthenticated — the card is public metadata.
        self._jwt = jwt_token
        self._client = httpx.AsyncClient(timeout=600.0)

    # ── Lifecycle ────────────────────────────────────────────────────
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AgentOrchestrator":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ── 1. Discovery ─────────────────────────────────────────────────
    async def discover_agents(self) -> dict[str, dict[str, Any]]:
        """Fetch every card from AGENT_REGISTRY. Skip unreachable ones."""
        self.agents = {}
        unavailable: list[str] = []

        for url in AGENT_REGISTRY:
            try:
                resp = await self._client.get(url, timeout=5.0)
                resp.raise_for_status()
                card = resp.json()
                if not isinstance(card, dict) or "name" not in card:
                    unavailable.append(url)
                    continue
                self.agents[card["name"]] = card
            except Exception:
                unavailable.append(url)

        print(f"[DISCOVERY] Available agents ({len(self.agents)}):")
        for name, card in self.agents.items():
            print(f"  ✓ {card.get('display_name', name):30s}  {card.get('url', '')}")
        if unavailable:
            print(f"[DISCOVERY] Unavailable ({len(unavailable)}):")
            for url in unavailable:
                print(f"  ✗ {url}")

        return self.agents

    # ── 2. Routing ───────────────────────────────────────────────────
    async def route(self, question: str) -> list[dict[str, Any]]:
        """Ask the LLM to pick and order the agents for this question."""
        if not self.agents:
            return []

        catalogue_lines: list[str] = []
        for card in self.agents.values():
            accepts = ", ".join(card.get("capabilities", {}).get("accepts") or [])
            catalogue_lines.append(
                f"- name: {card['name']}\n"
                f"  display_name: {card.get('display_name', card['name'])}\n"
                f"  description: {card.get('description', '').strip()}\n"
                f"  accepts: [{accepts}]"
            )
        catalogue = "\n".join(catalogue_lines)

        user_prompt = (
            f"USER QUESTION:\n{question}\n\n"
            f"AVAILABLE AGENTS:\n{catalogue}\n\n"
            "Return only the JSON plan."
        )

        response = await self._llm.ainvoke(
            [SystemMessage(content=ROUTING_SYSTEM), HumanMessage(content=user_prompt)]
        )
        raw = response.content if isinstance(response.content, str) else ""

        plan = _extract_json(raw) or {}
        chosen = plan.get("agents") or []
        reasoning = plan.get("reasoning", "")

        # Validate — keep only names the orchestrator actually discovered,
        # preserving order, de-duplicating.
        valid_ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in chosen:
            if isinstance(name, str) and name in self.agents and name not in seen:
                valid_ordered.append(self.agents[name])
                seen.add(name)

        # Fallback: if the LLM returned nothing usable, run every available
        # agent in registry order so the user still gets an answer.
        if not valid_ordered:
            print(
                "[ROUTING] LLM did not return a valid plan — falling back "
                "to all available agents."
            )
            valid_ordered = list(self.agents.values())
            reasoning = "Fallback — LLM routing was unparseable."

        print(f"[ROUTING] Plan: {[c['name'] for c in valid_ordered]}")
        if reasoning:
            print(f"[ROUTING] Reasoning: {reasoning}")

        return valid_ordered

    # ── 3a. Hand-off summariser (A1) ─────────────────────────────────
    async def _summarise(self, display_name: str, answer: str) -> str:
        """Distill one agent's narrative into a compact fact sheet.

        The summary is what the NEXT agent downstream sees as context.
        The agent's full answer is still retained in ``results`` for the
        final assembly step — summarisation only shrinks the hand-off.
        """
        prompt = (
            f"Analyst: {display_name}\n\n"
            f"Analyst output:\n{answer}\n\n"
            "Produce the hand-off fact sheet now."
        )
        try:
            response = await self._llm.ainvoke(
                [
                    SystemMessage(content=HANDOFF_SYSTEM),
                    HumanMessage(content=prompt),
                ]
            )
            text = response.content if isinstance(response.content, str) else ""
            return text.strip() or "- (no actionable findings produced)"
        except Exception as e:
            # If summarisation itself fails, fall back to a hard truncation
            # so the pipeline still passes *something* forward.
            truncated = answer if len(answer) < 600 else answer[:600] + "…"
            return f"(summariser failed: {e}) {truncated}"

    # ── 3. Execution (sequential, with summarised context passing) ───
    async def execute(
        self, question: str, agents: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Call each agent's /tasks endpoint, forwarding a summarised context."""
        results: list[dict[str, Any]] = []
        context_parts: list[str] = []

        for i, card in enumerate(agents, 1):
            display = card.get("display_name", card["name"])
            task_path = (
                card.get("transport", {}).get("task_endpoint") or "/tasks"
            )
            url = card["url"].rstrip("/") + task_path

            context = "\n\n".join(context_parts) if context_parts else None
            payload = {"question": question, "context": context}

            print(f"\n[EXECUTE {i}/{len(agents)}] → {display}  ({url})")
            if context:
                print(
                    f"[EXECUTE {i}/{len(agents)}] context forwarded "
                    f"({len(context)} chars):\n{context}"
                )

            entry: dict[str, Any] = {
                "agent": card["name"],
                "display_name": display,
                "status": "failed",
                "answer": "",
                "error": None,
            }
            headers = (
                {"Authorization": f"Bearer {self._jwt}"} if self._jwt else None
            )
            try:
                resp = await self._client.post(
                    url, json=payload, headers=headers, timeout=600.0
                )
                resp.raise_for_status()
                data = resp.json()
                entry["status"] = data.get("status", "completed")
                entry["answer"] = data.get("answer", "") or ""
                entry["error"] = data.get("error")
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"

            results.append(entry)
            print(
                f"[EXECUTE {i}/{len(agents)}] ← {display} "
                f"status={entry['status']}  "
                f"len(answer)={len(entry['answer'])}"
            )

            # Only feed forward successful, non-empty answers.
            if entry["status"] == "completed" and entry["answer"]:
                # Special case: the WRITE-side outreach agent needs the
                # full plan text (suggested_opening is free-text — the
                # bullet summariser drops it). For every other downstream
                # hop, the compact summary is fine and keeps context
                # windows under control.
                next_card = agents[i] if i < len(agents) else None
                next_is_outreach = (
                    next_card is not None
                    and next_card.get("name") == "sales-outreach-agent"
                )

                if next_is_outreach:
                    print(
                        f"[EXECUTE {i}/{len(agents)}] full answer "
                        f"forwarded to sales-outreach-agent (no summary)"
                    )
                    context_parts.append(
                        f"Findings from {display}:\n{entry['answer']}"
                    )
                else:
                    summary = await self._summarise(display, entry["answer"])
                    entry["summary"] = summary
                    print(
                        f"[EXECUTE {i}/{len(agents)}] summary "
                        f"({len(summary)} chars):\n{summary}"
                    )
                    context_parts.append(
                        f"Findings from {display}:\n{summary}"
                    )

        return results

    # ── 4. Assembly ──────────────────────────────────────────────────
    async def assemble(
        self, question: str, results: list[dict[str, Any]]
    ) -> str:
        """Ask the LLM to merge all agent outputs into one coherent answer."""
        sections: list[str] = []
        for r in results:
            heading = r.get("display_name", r.get("agent", "Agent"))
            if r.get("status") == "completed" and r.get("answer"):
                sections.append(f"### {heading}\n{r['answer']}")
            else:
                err = r.get("error") or "no answer"
                sections.append(f"### {heading}\n(agent failed: {err})")

        prompt = (
            f"ORIGINAL QUESTION:\n{question}\n\n"
            f"AGENT CONTRIBUTIONS (in the order they ran):\n"
            + "\n\n".join(sections)
            + "\n\nProduce the final combined answer now."
        )

        response = await self._llm.ainvoke(
            [SystemMessage(content=ASSEMBLY_SYSTEM), HumanMessage(content=prompt)]
        )
        return response.content if isinstance(response.content, str) else ""

    # ── 5. Top-level run ─────────────────────────────────────────────
    async def run(self, question: str) -> str:
        """Full discover → route → execute → assemble pipeline."""
        if not self.agents:
            await self.discover_agents()
        if not self.agents:
            return "No agents are available. Start the agent servers and retry."

        plan = await self.route(question)
        if not plan:
            return "Routing produced no agents — nothing to run."

        results = await self.execute(question, plan)
        return await self.assemble(question, results)
