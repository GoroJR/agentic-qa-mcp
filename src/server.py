"""
Agentic QA MCP Server.

v3 toolset (7 tools total):

  Existing v2:
   1. fetch_jira_ticket(key)
   2. generate_qa_test_cases(ac_text, ticket_key=...)
   3. export_test_cases(cache_path, ticket_key, summary, fmt)
   4. attach_to_jira(ticket_key, file_path)

  New v3:
   5. extract_ac_from_transcript(transcript_text)
   6. refine_extracted_ac(prior_extraction_json, user_answers)
   7. create_jira_ticket_from_extraction(extraction_json, project_key)

The end-to-end agentic loop becomes:
  transcript -> extraction -> ticket creation -> existing v2 pipeline

CRITICAL: STDIO-based MCP server. NEVER print() to stdout. Always file=sys.stderr.
"""

import os
import sys
import json
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Local modules
from src.jira_client import fetch_ticket, attach_file, create_issue
from src.test_generator import generate_test_cases, count_by_layer
from src.exporter import export_xlsx, export_csv, default_output_path
from src.transcript_extractor import (
    extract_ac,
    refine_ac,
    format_extraction_as_jira_description,
)


# Load .env from project root regardless of where Claude Code launches us from
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"))


# Cache dir for intermediate JSON outputs
_CACHE_DIR = os.path.join(_PROJECT_ROOT, "output", ".cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


mcp = FastMCP("agentic-qa")


def _log(msg: str) -> None:
    """Stderr-only logger (stdout is reserved for JSON-RPC)."""
    print(f"[agentic-qa] {msg}", file=sys.stderr, flush=True)


def _save_cache(prefix: str, data: dict) -> str:
    """Save a dict to a timestamped JSON file under output/.cache, return its path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = prefix.replace("/", "_").replace("\\", "_")
    path = os.path.join(_CACHE_DIR, f"{safe}_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


# ============================================================
#  v2 tools (unchanged behavior)
# ============================================================

@mcp.tool()
def fetch_jira_ticket(ticket_key: str) -> dict[str, Any]:
    """
    Fetch a Jira ticket and return summary, plain-text description (ADF flattened),
    status, and browse URL.

    Args:
        ticket_key: Jira issue key, e.g. "KAN-6".
    """
    _log(f"fetch_jira_ticket({ticket_key})")
    return fetch_ticket(ticket_key)


@mcp.tool()
def generate_qa_test_cases(ac_text: str, ticket_key: str = "ad-hoc") -> dict[str, Any]:
    """
    Generate executable QA test cases using the 9-layer framework
    (UI, Field, Conditional, Combination, Action, Persistence, Integration,
    Accessibility, Ambiguity).

    Caches the full result to disk so export / attach can reuse without re-LLM.

    Args:
        ac_text: Acceptance criteria text (typically the `description` from fetch_jira_ticket).
        ticket_key: Tag for cache filename. Defaults to "ad-hoc".
    """
    _log(f"generate_qa_test_cases(ticket_key={ticket_key})")
    result = generate_test_cases(ac_text)
    counts = count_by_layer(result)
    cache_path = _save_cache(ticket_key, result)
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
    Export cached test cases to .xlsx and/or .csv matching the standard QA template
    (9 columns including Layer + AC Ref).

    Args:
        cache_path: Path returned by generate_qa_test_cases.
        ticket_key: e.g. "KAN-6". Used for filename and Excel header.
        ticket_summary: Optional ticket title to include in the Excel header.
        fmt: "xlsx", "csv", or "both". Defaults to "xlsx".
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
    Upload a file as an attachment on a Jira ticket.

    Args:
        ticket_key: Jira ticket key, e.g. "KAN-6".
        file_path: Full path to the file to upload.
    """
    _log(f"attach_to_jira({ticket_key}, {file_path})")
    return attach_file(ticket_key, file_path)


# ============================================================
#  v3 tools (transcript -> ticket)
# ============================================================

@mcp.tool()
def extract_ac_from_transcript(transcript_text: str) -> dict[str, Any]:
    """
    Parse a meeting transcript (Zoom AI Companion plain text, Google Meet, Teams,
    or any speaker-labeled transcript) and extract structured requirements.

    Returns a dict with: issue_type (Story/Bug/Task), title, user_story,
    acceptance_criteria (each with a confidence flag), open_questions,
    implicit_assumptions, ambiguity_flags, estimated_complexity, out_of_scope.

    The full extraction is cached to disk; downstream tools can either be
    passed the dict directly, or use the cache_path returned.

    Args:
        transcript_text: Plain text of the transcript. Speaker labels and
                         timestamps are fine but not required.
    """
    _log("extract_ac_from_transcript(...)")
    extraction = extract_ac(transcript_text)
    cache_path = _save_cache("transcript_extraction", extraction)
    return {
        "issue_type": extraction.get("issue_type"),
        "title": extraction.get("title"),
        "ac_count": len(extraction.get("acceptance_criteria", [])),
        "open_questions_count": len(extraction.get("open_questions", [])),
        "ambiguity_flags_count": len(extraction.get("ambiguity_flags", [])),
        "complexity": extraction.get("estimated_complexity"),
        "cache_path": cache_path,
        "extraction": extraction,  # full dict, in case caller wants it inline
    }


@mcp.tool()
def refine_extracted_ac(cache_path: str, user_answers: str) -> dict[str, Any]:
    """
    Refine a prior extraction by answering its open_questions or resolving its
    ambiguity_flags.

    Args:
        cache_path: Path returned by extract_ac_from_transcript.
        user_answers: Plain-text answers from the user, free form (typically
                      addressing the open_questions / ambiguities one by one).
    """
    _log(f"refine_extracted_ac(cache={cache_path})")

    if not os.path.exists(cache_path):
        raise FileNotFoundError(f"Cache file not found: {cache_path}")

    with open(cache_path, "r", encoding="utf-8") as f:
        prior = json.load(f)

    refined = refine_ac(prior, user_answers)
    new_cache_path = _save_cache("transcript_extraction_refined", refined)

    return {
        "issue_type": refined.get("issue_type"),
        "title": refined.get("title"),
        "ac_count": len(refined.get("acceptance_criteria", [])),
        "open_questions_count": len(refined.get("open_questions", [])),
        "ambiguity_flags_count": len(refined.get("ambiguity_flags", [])),
        "complexity": refined.get("estimated_complexity"),
        "cache_path": new_cache_path,
        "extraction": refined,
    }


@mcp.tool()
def create_jira_ticket_from_extraction(
    cache_path: str,
    project_key: str = "KAN",
) -> dict[str, Any]:
    """
    Create a Jira ticket from a cached extraction (output of extract_ac_from_transcript
    or refine_extracted_ac).

    The ticket gets:
      - Title = extraction.title
      - Description (ADF) = formatted user story + AC list + open questions + assumptions
      - Issue type = "Tarea" (project default - works on free Atlassian Cloud)
      - Labels = ["type:story" | "type:bug" | "type:task", "source:transcript"]

    Args:
        cache_path: Path to the extraction JSON returned by an extraction tool.
        project_key: Jira project key. Defaults to "KAN".

    Returns:
        {"key": "KAN-7", "id": "...", "url": "https://...browse/KAN-7", "labels": [...]}
    """
    _log(f"create_jira_ticket_from_extraction(cache={cache_path}, project={project_key})")

    if not os.path.exists(cache_path):
        raise FileNotFoundError(f"Cache file not found: {cache_path}")

    with open(cache_path, "r", encoding="utf-8") as f:
        extraction = json.load(f)

    title = extraction.get("title", "Untitled story from transcript")
    description = format_extraction_as_jira_description(extraction)

    # Map detected issue_type -> label, since the project may not have
    # Story/Bug as actual Jira issue types on free tier.
    detected_type = (extraction.get("issue_type") or "Task").lower()
    type_label_map = {
        "story": "type:story",
        "bug": "type:bug",
        "task": "type:task",
        "unknown": "type:unclear",
    }
    type_label = type_label_map.get(detected_type, "type:task")

    result = create_issue(
        project_key=project_key,
        summary=title,
        description_text=description,
        issue_type_name="Tarea",  # KAN project uses Tarea as its base type
        labels=[type_label, "source:transcript", "generated:agentic-qa"],
    )
    result["labels"] = [type_label, "source:transcript", "generated:agentic-qa"]
    result["detected_type"] = extraction.get("issue_type")
    return result


if __name__ == "__main__":
    _log(f"Starting MCP server v3 (model={os.environ.get('CLAUDE_MODEL', 'default')})")
    mcp.run()
