# Startup Guide

Get the stack running end-to-end in ~15 minutes the first time, ~30 seconds after that.

The README has the abridged quickstart. This doc covers the full setup, including the Supabase project, and is the one to follow if you've never run the stack before.

---

## 1. Prerequisites

- **Python 3.11+**
- **macOS or Linux** (shell scripts use bash; Windows users should use WSL)
- **lsof** (pre-installed on macOS, `apt install lsof` on Debian/Ubuntu)
- **An OpenAI API key** — get one at https://platform.openai.com/api-keys (~$5 in credit is plenty)
- **A Supabase project** — free tier is fine. Sign up at https://supabase.com
- **(Optional) A Langfuse account** for tracing — https://langfuse.com

---

## 2. Create your Supabase project

1. Go to https://supabase.com → New project. Pick any region; set a database password and save it.
2. Once the project is provisioned, go to **Project Settings → Database → Connection string → URI → Session Pooler**. Copy that string. It looks like:
   ```
   postgresql://postgres.<ref>:[YOUR-PASSWORD]@aws-1-<region>.pooler.supabase.com:5432/postgres
   ```
   Substitute `[YOUR-PASSWORD]` with the password you set at project creation. This is your `SUPABASE_DATABASE_URL`.
3. Go to **Project Settings → API**. Copy the **publishable** key and the **secret** key. These map to `SUPABASE_PUBLISH_KEY` and `SUPABASE_SECRET_KEY`. The secret key signs JWTs — keep it server-side.

> **Why the Session Pooler and not Direct Connection?** The pooler is on port 5432 and supports the per-request role switching this app relies on. Direct connection works too, but the pooler is what the seed scripts and runtime have been tested against.

---

## 3. Install and configure

```bash
# From the project root (enterprise-agents/)

# Create a virtualenv and install dependencies
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# stack.sh expects ./venv — symlink it
ln -s .venv venv

# Copy the env template and fill it in
cp .env.example .env
chmod 600 .env
# Edit .env with your real OPENAI_API_KEY, SUPABASE_DATABASE_URL,
# SUPABASE_PUBLISH_KEY, SUPABASE_SECRET_KEY. Optionally add Langfuse keys.
```

---

## 4. Seed the database (three steps, in order)

```bash
# Load env so the scripts can read SUPABASE_DATABASE_URL
set -a; source .env; set +a

# A. Schema + sample business data (customers, transactions, sales reps, products)
./venv/bin/python data/seed_postgres.py

# B. Users, roles, and a few sample drafts
./venv/bin/python data/seed_rbac.py

# C. RLS policies + SECURITY DEFINER helpers + sequence grants
./venv/bin/python scripts/apply_rls.py
```

After step C, the database has:
- 6 seeded users (4 sales reps, 1 manager, 1 admin) — see the table in [README.md](./README.md)
- 12 RLS policies covering customers, transactions, interactions, drafts, audit log
- Append-only `agent_audit_log` (no UPDATE or DELETE policy)

All three scripts are idempotent — safe to re-run.

---

## 5. Start the stack

```bash
./scripts/stack.sh start
```

This launches **10 processes**:
- 6 MCP servers on ports 8001–8006
- 4 agent servers on ports 9001–9004

Each is launched in the background; logs go to `logs/<name>.log`. The script polls each port until it binds and stops only when everything is ready.

Check status anytime:
```bash
./scripts/stack.sh status
```

---

## 6. Ask the orchestrator a question

```bash
# As a sales rep — RLS filters to their territory
./venv/bin/python orchestrator/main.py \
  --user alice@northsales.com \
  "Find my highest-risk customer and prepare outreach for manager review."

# As a manager — full visibility, can approve drafts
./venv/bin/python orchestrator/main.py \
  --user maria@manager.com \
  "How many customers do we have across all regions?"

# Interactive REPL (omit the question)
./venv/bin/python orchestrator/main.py --user alice@northsales.com
```

The orchestrator mints a JWT from the email, attaches it to every downstream call, and the MCP servers verify it before doing any work.

> **Try this:** ask the same question as `alice@northsales.com` and then as `maria@manager.com`. Alice sees only North; Maria sees everything. That's row-level security — same SQL, different result, enforced at the database.

---

## 7. Review and approve drafts

When the outreach agent submits a draft, it lands with `status='pending'`. Use the review CLI to approve or reject:

```bash
./venv/bin/python scripts/review_drafts.py --user maria@manager.com
```

Try it as a sales rep — it'll list pending drafts but the database will reject any approval attempt. That's the HITL gate, enforced by RLS.

---

## 8. Run the evals

```bash
./venv/bin/python evals/run.py            # all cases
./venv/bin/python evals/run.py --only alice_scoped_to_north   # single case
```

Writes `evals/report.md`. Ten cases including adversarial prompt injection and role escalation. Exits non-zero if any case fails — CI-friendly.

---

## 9. Stop the stack

```bash
./scripts/stack.sh stop
```

Or `./scripts/stack.sh restart` if you've edited agent or MCP server code (running processes don't pick up changes automatically).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `.env not found` on `stack.sh start` | Copy `.env.example` to `.env` and fill it in. |
| `venv not found` | Run `python3 -m venv .venv && ln -s .venv venv` and re-install. |
| `permission denied for sequence outreach_drafts_id_seq` | RLS is enabled but sequence grants are missing. Re-run `./venv/bin/python scripts/apply_rls.py`. |
| `syntax error at or near "$1"` from Postgres | An older version of `db.py` is using `SET LOCAL`. Pull latest — current code uses `SELECT set_config(...)`. |
| Port already in use | Find with `lsof -iTCP:<port> -sTCP:LISTEN` and kill. Ports used: 8001–8006, 9001–9004. |
| `OPENAI_API_KEY is not set` | Run `set -a; source .env; set +a` before the orchestrator command. |
| Agent crashes immediately | Check `logs/<agent_name>.log` — usually an MCP server isn't reachable. |
| All MCP calls return 401 | You forgot `--user <email>`. The orchestrator runs unauthenticated by default and the middleware is strict. |
| Langfuse traces show no `user_id` | Make sure you started with `--user <email>`. The association properties are set after the JWT is minted. |

---

## What you should see in Langfuse (if configured)

Each orchestrator run produces one trace tagged with `user_id`, `user_role`, and `session_id`. Spans nest: orchestrator → agent `/tasks` call → MCP tool call → DB query. You can filter "all runs by Alice" with one click.

---

## Direct agent access (bypass the orchestrator)

Each agent exposes a `/tasks` endpoint. Useful for isolating one agent during debugging — but you'll need to mint and attach a JWT manually:

```bash
TOKEN=$(./venv/bin/python -c "from auth import mint_jwt; print(mint_jwt('<user-id>', 'alice@northsales.com', 'sales_rep'))")

curl -X POST http://127.0.0.1:9001/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question": "How many customers do I have?"}'
```

And the A2A discovery endpoint (no auth required):

```bash
curl http://127.0.0.1:9001/.well-known/agent.json | python3 -m json.tool
```

---

See [ARCHITECTURE.md](./ARCHITECTURE.md) for design rationale and [README.md](./README.md) for the project overview.
