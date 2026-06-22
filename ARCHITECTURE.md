# Enterprise Agents — Architecture

**A multi-agent enterprise sales platform.** Specialist agents coordinate over a shared tool layer to turn customer risk data into reviewed, manager-approved outreach. Identity-scoped Postgres access control is the security boundary.

---

## The problem this solves

Enterprise sales teams spend four to eight hours stitching together churn signals, customer history, product fit, and rep assignment for a single high-risk account — pulled from disconnected systems and written up by hand. By the time the action plan is ready, the customer has often already churned.

This system collapses that workflow into one question — *"Analyze customer X — what's the risk and what's the play?"* — and returns a structured action plan in under sixty seconds, with a human approval step before any outreach is sent.

---

## System diagram

```
                 ┌────────────────────────────┐
                 │  User (Sales Rep / Mgr)    │
                 │     signs in → JWT          │
                 └──────────────┬─────────────┘
                                │
                                ▼
                 ┌────────────────────────────┐
                 │       Orchestrator         │
                 │  routes · hands off ·      │
                 │  assembles · streams       │
                 └──────┬─────────────────────┘
                        │ JWT in Authorization header
        ┌───────────────┼────────────────┬─────────────────┐
        ▼               ▼                ▼                 ▼
  ┌──────────┐   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ Data     │   │ Customer     │  │ Sales        │  │ Sales        │
  │ Analysis │   │ Intelligence │  │ Intelligence │  │ Outreach     │
  │ (read)   │   │ (read)       │  │ (read)       │  │ (WRITE)      │
  └────┬─────┘   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                │                 │                  │
       └────────────────┴─────────────────┴──────────────────┘
                                │ MCP over HTTP
                                │ JWT propagated
                                ▼
       ┌────────────────────────────────────────────────────┐
       │   Six MCP servers — shared tool layer              │
       │   database · file · scoring · report ·             │
       │   recommendation · outreach                        │
       └────────────────────────────┬───────────────────────┘
                                    │ Caller identity set per request
                                    ▼
       ┌────────────────────────────────────────────────────┐
       │   Supabase Postgres                                │
       │   RBAC + Row-Level Security + Audit log            │
       └────────────────────────────────────────────────────┘
```

---

## Components

| Layer | What it does | Why it is separate |
|---|---|---|
| **Orchestrator** | Routes the question to the right agents, passes their findings forward, assembles the final answer. | Decoupled from agents — the routing rules can change without touching agent code. |
| **Data Analysis** | General SQL and file analysis. Broad business questions. | Generalist read agent. |
| **Customer Intelligence** | Churn risk, lifetime value, segmentation, cohort analysis. | Domain math isolated from generalist analysis. |
| **Sales Intelligence** | Turns customer data into action plans with rep and product recommendations. | Read-only. Produces the plan but never persists it. |
| **Sales Outreach** | Writes the action plan into the approval queue. | Write-side specialist. The only agent with persistence access. |
| **Six MCP servers** | Tool layer — SQL, scoring, recommendations, outreach drafting, file I/O, reporting. | Tools are shared across agents and can be reused by humans or future agents without modification. |
| **Postgres + RLS** | Source of truth. Access control, audit log, approval queue. | The database is the final word on what any caller can see or do. |

---

## Key design decisions

### 1. Access control lives in the database, not in application code

The database enforces who can see which rows. When a North-territory sales rep runs any query — directly, or through an agent — Postgres filters the result to only customers in their territory before returning anything. The agent never has the option to leak data, because the data is never handed to it in the first place.

**Tradeoff** — Policies in the database are harder to unit-test than rules in code, and require Postgres expertise to maintain. The upside is that no agent, no buggy code path, and no prompt injection can bypass them.

---

### 2. Analysis and outreach are handled by two separate agents

The analyst agent reads customer data and produces a plan. The outreach agent takes that plan and submits it to the approval queue. The analyst agent has no access to the tool that creates drafts. Capability separation is enforced at the agent boundary, not by instructing the agent to behave a certain way.

**Tradeoff** — Two agents are more complex to coordinate than one. The benefit is that the boundary between reading and acting is structural — it does not rely on the agent following instructions correctly.

---

### 3. No customer-facing action happens without manager approval

The outreach agent writes drafts with status "pending." Only a user with the manager role can change that status to "approved," and this rule is enforced by the database, not by the application. A sales rep cannot approve their own draft, and the agent cannot bypass the approval step.

**Tradeoff** — This adds a manual step before any outreach reaches a customer, which slows the workflow. The reason it is the right tradeoff is that outreach errors damage customer relationships in ways that automated retries cannot fix.

---

### 4. Every agent run is recorded and inspectable

For each interaction, the system captures the question, the routing decision, every tool call, the inputs and outputs of each call, the latency, and the token cost. A manager or an engineer can pull up any past run and review exactly what the agent did. The recording uses an industry-standard format, so the dashboard is replaceable without changing the agents.

**Tradeoff** — This requires running an observability pipeline, which adds operational overhead. The benefit is that agent behavior is auditable rather than opaque, which matters for both trust and debugging.

---

### 5. Prompt changes are validated against a test set before shipping

The agents' instructions live in versioned files. Before a new version replaces an old one, it is run against a set of scripted scenarios — including adversarial ones such as prompt injection attempts. A version is promoted only if it passes the same scenarios the previous one passed and improves on at least one failure.

**Tradeoff** — Test cases for agent behavior are harder to write correctly than tests for deterministic code, and sometimes the test itself is wrong and needs to be corrected. The alternative — editing prompts based on intuition and hoping nothing regresses — is worse.

---

## Security model

Identity travels with every request, end to end:

1. **Identity established at the orchestrator.** In a production deployment, the user signs in through Supabase Auth and the front end attaches the resulting JWT to every request. In this build, the orchestrator simulates that handoff with a `--user <email>` flag — it looks the user up in the database and mints a JWT in the same format Supabase Auth would issue. The rest of the system is unchanged either way.
2. **Token forwarding.** The token is attached to every call the orchestrator makes to an agent, and every call an agent makes to a tool.
3. **Verification at the edge.** Each tool server verifies the token's signature before doing any work. Unsigned or expired tokens are rejected with a 401.
4. **Database enforcement.** The tool opens a database connection scoped to the user's identity. Postgres then filters every query and rejects any write that violates the user's role.

The agent operates as the caller — never with elevated privileges. Every action it takes is recorded in an append-only audit log with the user's ID, the tool called, and the arguments used.

---

## What was deliberately not built

- **Retrieval-augmented generation.** All the data this product uses is structured (customers, transactions, scores). Vector search would be the wrong tool. When unstructured documents (sales playbooks, contracts) enter the picture, retrieval becomes appropriate.
- **Streaming responses.** A polish item. The system runs in under sixty seconds end to end, so users do not wait long for output. Streaming is straightforward to add when the orchestrator's assembly step warrants it.
- **Per-request identity injection at the MCP protocol layer.** The protocol's standard transport does not yet have a clean hook for forwarding caller identity per call. We instead forward the token through HTTP headers and verify at each tool server. This works because both sides of the protocol are under our control.
- **Prompt-as-data infrastructure beyond versioning.** Production teams often run A/B prompt experiments through a separate prompt management service. Our versioned files and eval set cover the core workflow; the service-level layer can be added when more than one engineer is iterating on prompts.

---

## What is next

- **Deterministic flow for the sales intelligence agent.** Its current loop is open-ended (ReAct). The flow it follows in practice is fixed — risk lookup, recommendation, plan, hand off. Converting it to an explicit state graph makes the flow visible and easier to reason about.
- **Retrieval layer when unstructured documents arrive.** Sales playbooks, deal notes, and contracts will eventually need to inform the plan. A retrieval tool plugs into the same MCP layer; no agent rewrite needed.
- **Cost-aware model routing.** A small front-end model could classify the question type and route simple cases to a cheaper model. Currently every step runs on the same model.
- **Continuous evaluation in CI.** The eval set runs manually today. Wiring it into the deploy pipeline means a prompt change that regresses a scenario fails the build, not the customer.

---

*Author: Manu Jain · Built on LangGraph, MCP, Supabase, Langfuse · Source: this repository.*
