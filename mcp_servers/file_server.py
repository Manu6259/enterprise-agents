"""Standalone MCP Server — File System Operations.

Supports TWO transports chosen by the MCP_TRANSPORT env var:
  stdio — run as a subprocess (default, used by the agent in local mode)
  http  — run as a persistent streamable-HTTP server on FILE_SERVER_PORT

Tool logic is IDENTICAL in both modes. Only the startup wiring changes.

Tools exposed:
  1. list_files    — list data files in a directory
  2. read_file     — read contents of a file (with path-traversal and size guards)
  3. write_report  — write a .md or .txt report to REPORTS_DIR

Launch:
    python mcp_servers/file_server.py              # stdio
    MCP_TRANSPORT=http python mcp_servers/file_server.py
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

# Allow this standalone script to import config.py from the project root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import DATA_DIR, FILE_SERVER_PORT, MCP_TRANSPORT, REPORTS_DIR  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402


# ── Constants ──────────────────────────────────────────────────────────
_READABLE_EXTENSIONS = {".csv", ".txt", ".json", ".md"}
_REPORT_EXTENSIONS = {".md", ".txt"}
_MAX_READ_BYTES = 50 * 1024  # 50 KB


# ── File helpers ───────────────────────────────────────────────────────
def _list_files(directory: str) -> list[dict[str, Any]]:
    """List files in *directory* filtered to readable data extensions."""
    if not os.path.isdir(directory):
        raise ValueError(f"Directory does not exist: {directory}")

    results: list[dict[str, Any]] = []
    for entry in sorted(os.listdir(directory)):
        full_path = os.path.join(directory, entry)
        if not os.path.isfile(full_path):
            continue
        _, ext = os.path.splitext(entry)
        if ext.lower() not in _READABLE_EXTENSIONS:
            continue
        size_bytes = os.path.getsize(full_path)
        results.append(
            {
                "file_name": entry,
                "size_kb": round(size_bytes / 1024, 2),
            }
        )
    return results


def _read_file(file_path: str) -> str:
    """Read *file_path* with path-traversal and size guards."""
    # Path traversal guard — reject any literal ".." in the path.
    if ".." in file_path.split(os.sep) or ".." in file_path.split("/"):
        raise ValueError("Path traversal not permitted.")

    if not os.path.exists(file_path):
        raise ValueError(f"File not found: {file_path}")
    if not os.path.isfile(file_path):
        raise ValueError(f"Not a regular file: {file_path}")

    size = os.path.getsize(file_path)
    if size > _MAX_READ_BYTES:
        raise ValueError("File too large to read (max 50KB).")

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _write_report(file_name: str, content: str) -> str:
    """Write *content* to REPORTS_DIR/file_name. Returns the absolute path."""
    _, ext = os.path.splitext(file_name)
    if ext.lower() not in _REPORT_EXTENSIONS:
        raise ValueError("Only .md and .txt report formats are permitted.")

    # Strip any directory components to keep writes scoped to REPORTS_DIR
    safe_name = os.path.basename(file_name)
    if not safe_name:
        raise ValueError("Report file name is empty.")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    full_path = os.path.join(REPORTS_DIR, safe_name)

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)

    return os.path.abspath(full_path)


# ── MCP server wiring ──────────────────────────────────────────────────
mcp = FastMCP(
    "file-server",
    host="127.0.0.1",
    port=FILE_SERVER_PORT,
)


@mcp.tool(
    description=(
        "List data files in a directory. Only files with these "
        "extensions are returned: .csv, .txt, .json, .md. Returns each "
        "file name with its size in KB. If no directory is provided, "
        "defaults to the project data directory."
    )
)
def list_files(directory: Optional[str] = None) -> list[dict[str, Any]]:
    return _list_files(directory or DATA_DIR)


@mcp.tool(
    description=(
        "Read and return the full contents of a file as a UTF-8 string. "
        "Rejects any path containing '..' as a path traversal attempt. "
        "Rejects files larger than 50KB."
    )
)
def read_file(file_path: str) -> str:
    return _read_file(file_path)


@mcp.tool(
    description=(
        "Write a report file to the project reports directory. Only "
        ".md and .txt extensions are permitted. The reports directory "
        "is created automatically if it does not exist. Returns the "
        "absolute path of the written file. Use this to save analytical "
        "findings for the user."
    )
)
def write_report(file_name: str, content: str) -> str:
    return _write_report(file_name, content)


# ── Entry point — transport chosen by MCP_TRANSPORT ────────────────────
if __name__ == "__main__":
    transport = MCP_TRANSPORT.lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
