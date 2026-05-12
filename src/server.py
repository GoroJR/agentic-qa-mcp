"""
Agentic QA MCP Server.

Exposes four tools to Claude Desktop so that a QA engineer can say things like
"read KAN-6, generate test cases, export as Excel, and attach to the ticket"
and have Claude execute the full pipeline directly.

Tools:
  1. fetch_jira_ticket(ticket_key)
     -> {key, summary, description, status, url}

  2. generate_qa_test_cases(ac_text, ticket_key=None)
     -> {total, by_layer, file_path_json (cached)}
     Returns counts plus the path to a JSON cache file so other tools can
     consume the result without re-calling the LLM.

  3. export_test_cases(json_path, ticket_key, summary=None, fmt="xlsx"|"csv"|"both")
     -> {xlsx_path?, csv_path?}

  4. attach_to_jira(ticket_key, file_path)
     -> {attachment_id, filename, size}

CRITICAL: Claude Desktop runs MCP servers over STDIO. NEVER print() to stdout
in this file - it corrupts the JSON-RPC protocol. Use `file=sys.stderr` only.
"""

import os
import sys
import json
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Local modules
from src.jira_client import fetch_ticket, attach_file
from src.test_generator import generate_test_cases, count_by_layer
from src.exporter import export_xlsx, export_csv, default_output_path


# Load .env from the project root regardless of where Claude Desktop launches us from
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"))


# Cache directory for intermediate JSON outputs from generate_qa_test_cases.
# Keeping them on disk decouples the tools and avoids re-LLM-calls inside a session.
_CACHE_DIR = os.path.join(_PROJECT_ROOT, "output", ".cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


# Init the FastMCP server. The name appears in Claude Desktop's connected-server list.
mcp = FastMCP("agentic-qa")


def _log(msg: str) -> None:
    """Stderr-only logger. Required because stdout is reserved for JSON-RPC."""
    print(f"[agentic-qa] {msg}", file=sys.stderr, flush=True)


@mcp.tool()
def fetch_jira_ticket(ticket_key: str) -> dict[str, Any]:
    """
    Fetch a Jira ticket and return its summary, description (flattened from
    Atlassian Document Format to plain text), status, and browse URL.

    Args:
        ticket_key: The Jira issue key, e.g. "KAN-6".

    Returns:
        {
          "key": "KAN-6",
          "summary": "...",
          "description": "Full description with AC, steps, notes as plain text",
          "status": "Por hacer",
          "url": "https://<site>/browse/KAN-6"
        }
    """
    _log(f"fetch_jira_ticket({ticket_key})")
    return fetch_ticket(ticket_key)


@mcp.tool()
def generate_qa_test_cases(ac_text: str, ticket_key: str = "ad-hoc") -> dict[str, Any]:
    """
    Generate executable QA test cases using the 9-layer framework
    (UI, Field, Conditional, Combination, Action, Persistence, Integration,
    Accessibility, Ambiguity).

    Internally calls the Claude API and caches the full result to disk so
    follow-up tools (export_test_cases, attach_to_jira) can reuse it without
    re-invoking the LLM.

    Args:
        ac_text: The acceptance criteria / ticket description as plain text.
                 Typically you pass the `description` field from fetch_jira_ticket.
        ticket_key: Optional. Tags the cache file. Defaults to "ad-hoc".

    Returns:
        {
          "total": 54,
          "by_layer": {"UI": 3, "Field": 3, ...},
          "cache_path": "<path to cached JSON for downstream tools>"
        }
    """
    _log(f"generate_qa_test_cases(ticket_key={ticket_key})")
    result = generate_test_cases(ac_text)
    counts = count_by_layer(result)

    # Cache the full result so export/attach tools don't need to re-run the LLM
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_key = ticket_key.replace("/", "_").replace("\\", "_")
    cache_path = os.path.join(_CACHE_DIR, f"{safe_key}_{timestamp}.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return {
        "total": counts.get("total", 0),
        "by_layer": {k: v for k, v in counts.items() if k != "total"},
        "cache_path": cache_path,
    }


@mcp.tool()
def export_test_cases(
    cache_path: str,
    ticket_key: str,
    ticket_summary: str = "",
    fmt: str = "xlsx",
) -> dict[str, Any]:
    """
    Export cached test cases to .xlsx and/or .csv matching the standard QA
    delivery template (9 columns including Layer and AC Ref).

    Args:
        cache_path: Path returned by generate_qa_test_cases.
        ticket_key: Ticket key for filename and Excel header (e.g. "KAN-6").
        ticket_summary: Optional ticket title to include in the Excel header.
        fmt: "xlsx", "csv", or "both". Defaults to "xlsx".

    Returns:
        Paths to the generated file(s):
        {"xlsx_path": "...", "csv_path": "..."}
    """
    _log(f"export_test_cases(cache={cache_path}, fmt={fmt})")

    if not os.path.exists(cache_path):
        raise FileNotFoundError(f"Cache file not found: {cache_path}")

    with open(cache_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    out: dict[str, Any] = {}

    if fmt in ("xlsx", "both"):
        xlsx_path = default_output_path(ticket_key, "xlsx")
        export_xlsx(result, xlsx_path,
                    ticket_key=ticket_key,
                    ticket_summary=ticket_summary or None)
        out["xlsx_path"] = xlsx_path

    if fmt in ("csv", "both"):
        csv_path = default_output_path(ticket_key, "csv")
        export_csv(result, csv_path)
        out["csv_path"] = csv_path

    return out


@mcp.tool()
def attach_to_jira(ticket_key: str, file_path: str) -> dict[str, Any]:
    """
    Upload a file as an attachment on a Jira ticket. Use after
    export_test_cases to deliver the generated Excel directly to the ticket.

    Args:
        ticket_key: Jira ticket key, e.g. "KAN-6".
        file_path: Full path to the file to upload.

    Returns:
        {"attachment_id": "...", "filename": "...", "size": 12345}
    """
    _log(f"attach_to_jira({ticket_key}, {file_path})")
    return attach_file(ticket_key, file_path)


if __name__ == "__main__":
    _log(f"Starting MCP server (model={os.environ.get('CLAUDE_MODEL', 'default')})")
    mcp.run()