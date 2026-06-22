# Case Study — Enterprise Agents

**A working multi-agent system that turns customer data into reviewed, manager-approved sales actions — with enterprise access control built in from the start.**

[See the 15-second demo →](https://github.com/Manu6259/enterprise-agents/blob/main/README.md) · [Technical architecture →](https://github.com/Manu6259/enterprise-agents/blob/main/ARCHITECTURE.md) · [Source code →](https://github.com/Manu6259/enterprise-agents)

---

## The problem

When a customer is about to churn, a sales team has a short window to act. But preparing the response is slow. Someone has to pull the customer's history, calculate the risk, check what products fit, decide which rep should own it, and write the outreach — across several disconnected systems, by hand. For a single high-value account this can take half a day. By the time the plan is ready, the customer has often already left.

The obvious fix is to let an AI agent do the assembling. The harder question — the one most demos skip — is how to do that in a real enterprise without creating new risks: agents that see data they shouldn't, send messages no human approved, or act in ways nobody can later audit.

## What I built

A team of specialist AI agents that answer one question — *"What's the risk on this customer, and what's the play?"* — and return a structured action plan in under a minute. One agent analyzes risk, another builds the recommendation, a third drafts the outreach. A coordinator decides which agents to involve and combines their work into a single answer. The agents share a common set of tools (built on the open Model Context Protocol), and the identity of the person asking travels with every step, all the way down to the database.

The system is fully working and runs locally. The demo shows the part that matters most: **the same question, asked by two different sales reps, returns different customers** — because each rep can only see their own territory. That rule is enforced by the database itself, not by asking the agent to behave.

## The decisions that make it enterprise-ready

These are the choices that separate a demo from something a real company could trust:

- **Access control lives in the database.** A sales rep sees only their territory's customers — even through an agent, even if someone tries to trick the agent with a malicious prompt. The filtering happens inside the database (Postgres row-level security), before any data reaches the agent, so there is nothing for the agent to leak.

- **The agent that analyzes cannot send.** The analysis agents have no ability to contact a customer — the tool that creates outreach simply isn't in their toolset. Only a separate, write-only agent can place a draft into the approval queue. The boundary between thinking and acting is structural, not a matter of the agent following instructions.

- **No outreach reaches a customer without a manager's approval.** Every draft lands in a pending queue. Only a manager can change its status to approved, and a sales rep cannot approve their own — again enforced as a database rule, not as application code that could be bypassed.

- **Every action is recorded and cannot be erased.** Each agent step is written to an append-only audit log — one that permits inserts but no updates or deletes, so no one (not a rep, not the agent, not a hijacked prompt) can alter the record after the fact. A manager or auditor can review exactly what happened on any past run.

## What it would take to productize

This is a demonstration build, not a deployed product. Turning it into one is a known, bounded set of steps: connect it to a real sign-in system (the security model is already designed for this), wire the evaluation suite into the deployment pipeline so a bad change fails the build instead of the customer, and add a retrieval layer when unstructured documents — playbooks, contracts, call notes — need to inform the plan. None of these require rethinking the design; the boundaries that make it safe are already in place.

---

*Built by Manu Jain on LangGraph, MCP, Supabase, and Langfuse. The full architecture, security model, and trade-offs are documented in [ARCHITECTURE.md](https://github.com/Manu6259/enterprise-agents/blob/main/ARCHITECTURE.md).*
