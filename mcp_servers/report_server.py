"""Standalone MCP Server — Intelligence Report I/O.

Supports TWO transports chosen by the MCP_TRANSPORT env var:
  stdio — run as a subprocess (default, used by agents in local mode)
  http  — run as a persistent streamable-HTTP server on REPORT_SERVER_PORT

Tools exposed:
  1. write_intelligence_report  — save a .md or .txt report to REPORTS_DIR
  2. list_reports               — enumerate all reports with size + ctime
  3. read_report                — read a saved report (path-traversal guarded)

Launch:
    python mcp_servers/report_server.py              # stdio
    MCP_TRANSPORT=http python mcp_servers/report_server.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any

# Allow this standalone script to import config.py from the project root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import MCP_TRANSPORT, REPORTS_DIR, REPORT_SERVER_PORT  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402


# ── Constants ──────────────────────────────────────────────────────────
_REPORT_EXTENSIONS = {".md", ".txt"}


# ── Helpers ────────────────────────────────────────────────────────────
def _write_intelligence_report(file_name: str, content: str) -> str:
    _, ext = os.path.splitext(file_name)
    if ext.lower() not in _REPORT_EXTENSIONS:
        raise ValueError("Only .md and .txt report formats are permitted.")

    safe_name = os.path.basename(file_name)
    if not safe_name:
        raise ValueError("Report file name is empty.")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    full_path = os.path.join(REPORTS_DIR, safe_name)

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)

    return os.path.abspath(full_path)


def _list_reports() -> list[dict[str, Any]]:
    if not os.path.isdir(REPORTS_DIR):
        return []

    results: list[dict[str, Any]] = []
    for entry in sorted(os.listdir(REPORTS_DIR)):
        full_path = os.path.join(REPORTS_DIR, entry)
        if not os.path.isfile(full_path):
            continue
        _, ext = os.path.splitext(entry)
        if ext.lower() not in _REPORT_EXTENSIONS:
            continue
        stat = os.stat(full_path)
        results.append(
            {
                "file_name": entry,
                "size_kb": round(stat.st_size / 1024, 2),
                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds"),
            }
        )
    return results


def _read_report(file_name: str) -> str:
    # Path traversal guard — reject any ".." anywhere in the input.
    if ".." in file_name.split(os.sep) or ".." in file_name.split("/"):
        raise ValueError("Path traversal not permitted.")

    # Only resolve inside REPORTS_DIR; strip any directory components.
    safe_name = os.path.basename(file_name)
    full_path = os.path.join(REPORTS_DIR, safe_name)

    if not os.path.isfile(full_path):
        raise ValueError(f"Report not found: {safe_name}")

    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ── MCP server wiring ──────────────────────────────────────────────────
mcp = FastMCP(
    "report-server",
    host="127.0.0.1",
    port=REPORT_SERVER_PORT,
)


@mcp.tool(
    description=(
        "Write an intelligence report to the project reports directory. "
        "Only .md and .txt extensions are permitted. The directory is "
        "created automatically if missing. Returns the absolute path of "
        "the written file. Use this to save structured analytical "
        "findings for stakeholders."
    )
)
def write_intelligence_report(file_name: str, content: str) -> str:
    return _write_intelligence_report(file_name, content)


@mcp.tool(
    description=(
        "List every report currently saved in the reports directory. "
        "Returns a list of objects with file_name, size_kb, and created "
        "timestamp. Takes no parameters."
    )
)
def list_reports() -> list[dict[str, Any]]:
    return _list_reports()


@mcp.tool(
    description=(
        "Read a previously generated report by file name. Rejects any "
        "path containing '..'. Returns the full UTF-8 contents of the "
        "report as a string. Use this to revisit or reference prior "
        "findings before writing a new report."
    )
)
def read_report(file_name: str) -> str:
    return _read_report(file_name)


# ── Entry point — transport chosen by MCP_TRANSPORT ────────────────────
if __name__ == "__main__":
    transport = MCP_TRANSPORT.lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
