"""Centralized configuration for the Enterprise Agents project.

All paths, URLs, transport modes, and model names live here. Never
hardcode these values elsewhere in the codebase — import from here.

This project hosts multiple agents behind a shared fleet of MCP
servers. See ``AGENTS`` and ``MCP_SERVERS`` at the bottom for the
registries that describe the current layout.
"""

import os

from dotenv import load_dotenv

# Load .env at import time. Anything that imports config.py gets env vars
# without each script having to call load_dotenv() itself.
load_dotenv()

# ── LLM provider — local Ollama or cloud OpenAI ───────────────────────
# Set LLM_PROVIDER=openai in the shell to use OpenAI; default is ollama.
# Variable NAMES stay OLLAMA_* for backward compat with existing imports,
# but their VALUES switch based on the provider.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()

if LLM_PROVIDER == "openai":
    OLLAMA_BASE_URL = "https://api.openai.com/v1"
    OLLAMA_API_KEY = os.getenv("OPENAI_API_KEY", "")
    MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
else:
    OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
    OLLAMA_API_KEY = "ollama"  # Placeholder — Ollama does not validate this
    MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen2:7b")

# ── Agent safety cap ──────────────────────────────────────────────────
# Maximum number of LangGraph steps any single agent request can take
# before LangGraph aborts with GraphRecursionError. This is a safety
# fuse — on healthy runs the agent finishes well below this limit.
AGENT_RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "20"))

# ── Paths ──────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
REPORTS_DIR = os.path.join(_PROJECT_ROOT, "reports")

# ── Transport mode ─────────────────────────────────────────────────────
# stdio — servers run as subprocesses spawned by the agent (default)
# http  — servers run as persistent HTTP services on fixed ports
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")

# ── MCP server ports (used when MCP_TRANSPORT=http) ───────────────────
DATABASE_SERVER_PORT = int(os.getenv("DATABASE_SERVER_PORT", "8001"))
FILE_SERVER_PORT = int(os.getenv("FILE_SERVER_PORT", "8002"))
SCORING_SERVER_PORT = int(os.getenv("SCORING_SERVER_PORT", "8003"))
REPORT_SERVER_PORT = int(os.getenv("REPORT_SERVER_PORT", "8004"))
RECOMMENDATION_SERVER_PORT = int(os.getenv("RECOMMENDATION_SERVER_PORT", "8005"))
OUTREACH_SERVER_PORT = int(os.getenv("OUTREACH_SERVER_PORT", "8006"))

# ── MCP server URLs (used by agents when MCP_TRANSPORT=http) ──────────
DATABASE_SERVER_URL = f"http://127.0.0.1:{DATABASE_SERVER_PORT}/mcp"
FILE_SERVER_URL = f"http://127.0.0.1:{FILE_SERVER_PORT}/mcp"
SCORING_SERVER_URL = f"http://127.0.0.1:{SCORING_SERVER_PORT}/mcp"
REPORT_SERVER_URL = f"http://127.0.0.1:{REPORT_SERVER_PORT}/mcp"
RECOMMENDATION_SERVER_URL = f"http://127.0.0.1:{RECOMMENDATION_SERVER_PORT}/mcp"
OUTREACH_SERVER_URL = f"http://127.0.0.1:{OUTREACH_SERVER_PORT}/mcp"

# ── MCP server script paths (used when MCP_TRANSPORT=stdio) ───────────
# Absolute paths — necessary because stdio subprocesses may be spawned
# with a different working directory.
DATABASE_SERVER_SCRIPT = os.path.join(
    _PROJECT_ROOT, "mcp_servers", "database_server.py"
)
FILE_SERVER_SCRIPT = os.path.join(
    _PROJECT_ROOT, "mcp_servers", "file_server.py"
)
SCORING_SERVER_SCRIPT = os.path.join(
    _PROJECT_ROOT, "mcp_servers", "scoring_server.py"
)
REPORT_SERVER_SCRIPT = os.path.join(
    _PROJECT_ROOT, "mcp_servers", "report_server.py"
)
RECOMMENDATION_SERVER_SCRIPT = os.path.join(
    _PROJECT_ROOT, "mcp_servers", "recommendation_server.py"
)
OUTREACH_SERVER_SCRIPT = os.path.join(
    _PROJECT_ROOT, "mcp_servers", "outreach_server.py"
)

# ── MCP server registry ────────────────────────────────────────────────
# All servers available to every agent in this project, keyed by the
# short name used inside the AGENTS registry below.
MCP_SERVERS = {
    "database": {
        "script": DATABASE_SERVER_SCRIPT,
        "port": DATABASE_SERVER_PORT,
        "url": DATABASE_SERVER_URL,
    },
    "file": {
        "script": FILE_SERVER_SCRIPT,
        "port": FILE_SERVER_PORT,
        "url": FILE_SERVER_URL,
    },
    "scoring": {
        "script": SCORING_SERVER_SCRIPT,
        "port": SCORING_SERVER_PORT,
        "url": SCORING_SERVER_URL,
    },
    "report": {
        "script": REPORT_SERVER_SCRIPT,
        "port": REPORT_SERVER_PORT,
        "url": REPORT_SERVER_URL,
    },
    "recommendation": {
        "script": RECOMMENDATION_SERVER_SCRIPT,
        "port": RECOMMENDATION_SERVER_PORT,
        "url": RECOMMENDATION_SERVER_URL,
    },
    "outreach": {
        "script": OUTREACH_SERVER_SCRIPT,
        "port": OUTREACH_SERVER_PORT,
        "url": OUTREACH_SERVER_URL,
    },
}

# ── Agent server ports (HTTP A2A mode) ────────────────────────────────
DATA_ANALYSIS_AGENT_PORT = int(os.getenv("DATA_ANALYSIS_AGENT_PORT", "9001"))
CUSTOMER_INTELLIGENCE_AGENT_PORT = int(
    os.getenv("CUSTOMER_INTELLIGENCE_AGENT_PORT", "9002")
)
SALES_INTELLIGENCE_AGENT_PORT = int(
    os.getenv("SALES_INTELLIGENCE_AGENT_PORT", "9003")
)
SALES_OUTREACH_AGENT_PORT = int(
    os.getenv("SALES_OUTREACH_AGENT_PORT", "9004")
)

# ── Agent base URLs ───────────────────────────────────────────────────
DATA_ANALYSIS_AGENT_URL = f"http://127.0.0.1:{DATA_ANALYSIS_AGENT_PORT}"
CUSTOMER_INTELLIGENCE_AGENT_URL = (
    f"http://127.0.0.1:{CUSTOMER_INTELLIGENCE_AGENT_PORT}"
)
SALES_INTELLIGENCE_AGENT_URL = f"http://127.0.0.1:{SALES_INTELLIGENCE_AGENT_PORT}"
SALES_OUTREACH_AGENT_URL = f"http://127.0.0.1:{SALES_OUTREACH_AGENT_PORT}"

# ── A2A agent-card registry ───────────────────────────────────────────
# Standard A2A discovery path is /.well-known/agent.json on each agent.
AGENT_REGISTRY = [
    f"{DATA_ANALYSIS_AGENT_URL}/.well-known/agent.json",
    f"{CUSTOMER_INTELLIGENCE_AGENT_URL}/.well-known/agent.json",
    f"{SALES_INTELLIGENCE_AGENT_URL}/.well-known/agent.json",
    f"{SALES_OUTREACH_AGENT_URL}/.well-known/agent.json",
]

# ── Agent registry ─────────────────────────────────────────────────────
# Each agent lists the MCP servers it uses and a short description.
# New agents add an entry here + a subpackage under agents/.
AGENTS = {
    "data_analysis": {
        "servers": ["database", "file"],
        "description": "General purpose data analysis over SQL and files",
    },
    "customer_intelligence": {
        "servers": ["database", "scoring", "report"],
        "description": (
            "Customer behaviour analysis, churn risk, segmentation "
            "and intelligence reporting"
        ),
    },
    "sales_intelligence": {
        "servers": ["database", "scoring", "recommendation", "outreach"],
        "description": (
            "Turns customer risk findings into specific sales action "
            "plans with rep assignments and product recommendations. "
            "READ-ONLY — produces plans as structured text; does NOT "
            "persist anything. Pair with sales_outreach when a plan "
            "needs to land in the human approval queue."
        ),
    },
    "sales_outreach": {
        "servers": ["outreach"],
        "description": (
            "WRITE-side specialist. Persists an action plan into the "
            "outreach_drafts queue via submit_draft. Status starts at "
            "'pending' — a manager must approve via the review queue "
            "before anything is sent. Use ONLY when the user explicitly "
            "wants to act on a customer (send, contact, follow up, "
            "prepare outreach). Skip for purely analytical questions."
        ),
    },
}

# ── Supabase / Postgres ───────────────────────────────────────────────
# Session Pooler connection string (port 5432). Required for any code path
# that hits Postgres. See .env.example for how to obtain it.
SUPABASE_DATABASE_URL = os.getenv("SUPABASE_DATABASE_URL", "")

# API keys (new Publishable / Secret format).
# Publishable is safe client-side; Secret is server-only.
SUPABASE_PUBLISH_KEY = os.getenv("SUPABASE_PUBLISH_KEY", "")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
