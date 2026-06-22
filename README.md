# Enterprise Agents

**A multi-agent enterprise sales platform.** Specialist agents coordinate over a shared tool layer to turn customer risk data into reviewed, manager-approved outreach. Identity-scoped Postgres access control is the security boundary.

> *"Analyze customer X — what's the risk and what's the play?"* → a structured action plan in under sixty seconds, with a human approval step before any outreach is sent.

<!-- TODO: 15-second GIF of the RLS demo goes here -->

---

## Why this exists

Enterprise sales teams spend four to eight hours stitching together churn signals, customer history, product fit, and rep assignment for a single high-risk account. This system collapses that workflow into one question and returns a manager-reviewable action plan — with row-level access control enforced at the database, capability separation enforced at the agent boundary, and every run captured to an observability pipeline.

The interesting parts aren't the agents themselves. They're:

- **Row-level security as a product invariant.** A North-territory rep cannot see South customers, even via prompt injection — the database filters before the agent sees a row.
- **Capability separation by construction.** The analyst agent has no tool to send outreach. Not by rule — by missing capability.
- **Database-enforced human-in-the-loop.** Drafts land with `status='pending'`. Only the manager role can flip them to approved. A sales rep cannot approve their own draft.
- **End-to-end observability.** Every run — routing decision, tool call, latency, token cost — is captured to Langfuse via OpenTelemetry.
- **Declarative eval set.** YAML cases with assertions, run via `python evals/run.py`, written to a markdown report. Includes adversarial prompt-injection and role-escalation cases.

Full design rationale, tradeoffs, and what was deliberately not built: see [ARCHITECTURE.md](./ARCHITECTURE.md).

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
                                │ MCP over HTTP, JWT propagated
                                ▼
       ┌────────────────────────────────────────────────────┐
       │   Six MCP servers — shared tool layer              │
       └────────────────────────────┬───────────────────────┘
                                    │ Caller identity set per request
                                    ▼
       ┌────────────────────────────────────────────────────┐
       │   Supabase Postgres                                │
       │   RBAC + Row-Level Security + Audit log            │
       └────────────────────────────────────────────────────┘
```

---

## Quickstart

You'll need: Python 3.11+, an OpenAI API key, and a Supabase project (free tier is fine).

```bash
# 1. Install
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
ln -s .venv venv   # stack.sh expects ./venv

# 2. Configure
cp .env.example .env
# Edit .env — paste your OPENAI_API_KEY, SUPABASE_DB_URL, SUPABASE_SECRET_KEY
# (optional) LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY for tracing

# 3. Seed the database (idempotent)
./venv/bin/python data/seed_postgres.py    # customers, transactions, sales reps
./venv/bin/python data/seed_rbac.py        # users, roles, sample drafts
./venv/bin/python scripts/apply_rls.py     # row-level security policies

# 4. Start the stack (10 processes — 4 agents + 6 MCP servers)
./scripts/stack.sh start

# 5. Ask a question as Alice (North-territory sales rep)
./venv/bin/python orchestrator/main.py \
  --user alice@northsales.com \
  "Find my highest-risk customer and prepare outreach for manager review."

# 6. Switch users to see RLS in action
./venv/bin/python orchestrator/main.py \
  --user maria@manager.com \
  "How many customers do we have across all regions?"

# 7. Teardown
./scripts/stack.sh stop
```

Seeded users:

| Email | Role | Territory |
|---|---|---|
| `alice@northsales.com` | sales_rep | North |
| `bob@southsales.com` | sales_rep | South |
| `carol@eastsales.com` | sales_rep | East |
| `david@westsales.com` | sales_rep | West |
| `maria@manager.com` | manager | (all) |
| `root@admin.com` | admin | (all) |

Detailed setup notes and troubleshooting: [STARTUP.md](./STARTUP.md).

---

## See it in action

**The RLS demo** — the same question returns different data depending on who's asking:

```bash
./venv/bin/python scripts/demo_rls.py
```

Three checks: per-territory visibility, cross-territory writes blocked, sales rep cannot self-approve a draft.

**The manager review CLI** — what a real reviewer would use to approve or reject drafts:

```bash
./venv/bin/python scripts/review_drafts.py --user maria@manager.com
```

**The eval set** — ten declarative cases including adversarial prompt injection. Writes a markdown report:

```bash
./venv/bin/python evals/run.py
# → evals/report.md
```

---

## Project layout

```
enterprise-agents/
├── ARCHITECTURE.md           # Design rationale, tradeoffs, what's not built
├── STARTUP.md                # First-run setup walkthrough
├── README.md                 # You are here
│
├── config.py                 # Single source of truth: agents, MCP servers, ports
├── auth.py                   # JWT mint + verify (HS256)
├── db.py                     # Postgres connection helper, per-request role switching
├── request_context.py        # ContextVar-based identity + Starlette auth middleware
├── observability.py          # Traceloop / Langfuse OTEL setup
│
├── agents/                   # Four specialist agents, each a LangGraph ReAct loop
│   ├── data_analysis/        # Read · SQL and file analysis
│   ├── customer_intelligence/# Read · churn, LTV, segmentation
│   ├── sales_intelligence/   # Read · turns risk into action plans
│   └── sales_outreach/       # WRITE · submits drafts to the approval queue
│
├── mcp_servers/              # Six FastMCP servers, shared tool layer
│   ├── database_server.py    # SELECT-only SQL
│   ├── file_server.py        # Read/write with path-traversal + extension guards
│   ├── scoring_server.py     # Churn risk, LTV, segmentation, cohorts
│   ├── report_server.py      # Structured intelligence reports
│   ├── recommendation_server.py
│   └── outreach_server.py    # submit_draft (write) — only sales_outreach uses it
│
├── orchestrator/
│   ├── orchestrator.py       # Routes, hands off, assembles
│   └── main.py               # CLI entry — `--user <email>` flag mints JWT
│
├── data/
│   ├── schema.sql
│   ├── rls_policies.sql      # 12 RLS policies + 4 SECURITY DEFINER helpers
│   ├── seed_postgres.py      # Customers, transactions, reps
│   └── seed_rbac.py          # Users, roles, drafts
│
├── evals/
│   ├── cases.yaml            # 10 declarative cases
│   └── run.py                # In-process runner; writes report.md
│
└── scripts/
    ├── stack.sh              # start | stop | status | restart
    ├── apply_rls.py
    ├── demo_rls.py           # Three RLS guarantees, demonstrated
    └── review_drafts.py      # Manager approval CLI
```

---

## Security model — what's actually enforced where

| Guarantee | Enforced at | How |
|---|---|---|
| Sales rep sees only their territory's customers | **Database (RLS)** | Policy on `customers` filters by `current_user_territory()` |
| Sales rep cannot approve their own draft | **Database (RLS)** | UPDATE policy on `outreach_drafts` requires `manager` role |
| Audit log is append-only | **Database (RLS)** | No UPDATE or DELETE policy exists |
| Agent cannot send outreach without going through the queue | **Agent boundary** | Analyst agents have no `submit_draft` tool |
| Tool calls fail without valid identity | **Tool server (middleware)** | Starlette middleware verifies JWT signature; 401 on missing/bad |
| Identity travels end to end | **All layers** | JWT in `Authorization` header, propagated orchestrator → agent → MCP → DB |

The agent operates as the caller, never with elevated privileges. Every action is recorded in `agent_audit_log` with user ID, tool, and arguments.

---

## License

MIT. See [LICENSE](./LICENSE).

---

## Credits

Built on [LangGraph](https://github.com/langchain-ai/langgraph), [MCP](https://modelcontextprotocol.io), [Supabase](https://supabase.com), [Langfuse](https://langfuse.com), [FastAPI](https://fastapi.tiangolo.com/), and [Traceloop](https://www.traceloop.com/). Author: Manu Jain.
