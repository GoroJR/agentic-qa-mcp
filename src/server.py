"""
Agentic QA MCP Server.

v4 toolset (8 tools total). Vision arrives:
  1. fetch_jira_ticket(key)                       (v2)
  2. fetch_jira_ticket_with_images(key)           (v4 NEW)
  3. generate_qa_test_cases(ac_text, ticket_key, image_paths=None)  (v4 extended)
  4. export_test_cases(...)                       (v2)
  5. attach_to_jira(...)                          (v2)
  6. extract_ac_from_transcript(...)              (v3)
  7. refine_extracted_ac(...)                     (v3)
  8. create_jira_ticket_from_extraction(...)      (v3)

CRITICAL: STDIO server. Never print() to stdout. Always file=sys.stderr.
"""

import os
import sys
import json
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Local modules
from src.jira_client import (
    fetch_ticket,
    fetch_ticket_with_attachments,
    attach_file,
    create_issue,
)
from src.test_generator import generate_test_cases, count_by_layer
from src.exporter import export_xlsx, export_csv, default_output_path
from src.transcript_extractor import (
    extract_ac,
    refine_ac,
    format_extraction_as_jira_description,
)


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"))


_CACHE_DIR = os.path.join(_PROJECT_ROOT, "output", ".cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


mcp = FastMCP("agentic-qa")


def _log(msg: str) -> None:
    print(f"[agentic-qa] {msg}", file=sys.stderr, flush=True)


def _save_cache(prefix: str, data: dict) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = prefix.replace("/", "_").replace("\\", "_")
    path = os.path.join(_CACHE_DIR, f"{safe}_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


# ============================================================
#  v2 + v4 tools (Jira read + test gen)
# ============================================================

@mcp.tool()
def fetch_jira_ticket(ticket_key: str) -> dict[str, Any]:
    """
    Fetch a Jira ticket and return summary, plain-text description (ADF flattened),
    status, and browse URL. Does NOT download attachments - use
    fetch_jira_ticket_with_images for that.
    """
    _log(f"fetch_jira_ticket({ticket_key})")
    return fetch_ticket(ticket_key)


@mcp.tool()
def fetch_jira_ticket_with_images(ticket_key: str) -> dict[str, Any]:
    """
    Same as fetch_jira_ticket, but also downloads image attachments
    (PNG, JPG, GIF, WEBP) to a local cache folder. Useful when the ticket has
    UI mockups, screenshots, or other visual evidence that should inform test
    case generation.

    Pass the returned `images[*].local_path` values to generate_qa_test_cases
    via the `image_paths` argument so the LLM grounds its UI/Field layer
    test cases in what the screens actually show.

    Returns:
        Same fields as fetch_jira_ticket PLUS:
          images: [{"filename", "local_path", "mime_type", "size"}, ...]
          skipped_attachments: [{"filename", "mime_type"}, ...]  (non-image files)
    """
    _log(f"fetch_jira_ticket_with_images({ticket_key})")
    return fetch_ticket_with_attachments(ticket_key)


@mcp.tool()
def generate_qa_test_cases(
    ac_text: str,
    ticket_key: str = "ad-hoc",
    image_paths: list[str] | None = None,
) -> dict[str, Any]:
    """
    Generate executable QA test cases using the 9-layer framework
    (UI, Field, Conditional, Combination, Action, Persistence, Integration,
    Accessibility, Ambiguity).

    v4: now accepts image_paths. When provided, the LLM treats these as UI
    mockups and grounds the UI/Field layer test cases in what's visible.

    Args:
        ac_text: Acceptance criteria text (typically the `description` from
                 fetch_jira_ticket).
        ticket_key: Tag for cache filename. Defaults to "ad-hoc".
        image_paths: Optional list of local image file paths. Capped at 5
                     internally. Get these from fetch_jira_ticket_with_images.

    Returns:
        {
          "total": <int>,
          "by_layer": {...},
          "cache_path": <str>,
          "used_images": <int>  (NEW in v4)
        }
    """
    _log(f"generate_qa_test_cases(ticket_key={ticket_key}, images={len(image_paths) if image_paths else 0})")
    result = generate_test_cases(ac_text, image_paths=image_paths)
    counts = count_by_layer(result)
    cache_path = _save_cache(ticket_key, result)
    return {
        "total": counts.get("total", 0),
        "by_layer": {k: v for k, v in counts.items() if k != "total"},
        "cache_path": cache_path,
        "used_images": len(image_paths) if image_paths else 0,
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
    template (9 columns including Layer + AC Ref).
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
    """Upload a file as an attachment on a Jira ticket."""
    _log(f"attach_to_jira({ticket_key}, {file_path})")
    return attach_file(ticket_key, file_path)


# ============================================================
#  v3 tools (transcript -> ticket)
# ============================================================

@mcp.tool()
def extract_ac_from_transcript(transcript_text: str) -> dict[str, Any]:
    """
    Parse a meeting transcript and extract structured requirements
    (issue_type, title, user_story, acceptance_criteria with confidence,
    open_questions, implicit_assumptions, ambiguity_flags, complexity).
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
        "extraction": extraction,
    }


@mcp.tool()
def refine_extracted_ac(cache_path: str, user_answers: str) -> dict[str, Any]:
    """Refine a prior extraction by answering its open_questions / ambiguity_flags."""
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
    """Create a Jira ticket from a cached extraction."""
    _log(f"create_jira_ticket_from_extraction(cache={cache_path}, project={project_key})")
    if not os.path.exists(cache_path):
        raise FileNotFoundError(f"Cache file not found: {cache_path}")
    with open(cache_path, "r", encoding="utf-8") as f:
        extraction = json.load(f)

    title = extraction.get("title", "Untitled story from transcript")
    description = format_extraction_as_jira_description(extraction)

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
        issue_type_name="Tarea",
        labels=[type_label, "source:transcript", "generated:agentic-qa"],
    )
    result["labels"] = [type_label, "source:transcript", "generated:agentic-qa"]
    result["detected_type"] = extraction.get("issue_type")
    return result


if __name__ == "__main__":
    _log(f"Starting MCP server v4 (model={os.environ.get('CLAUDE_MODEL', 'default')})")
    mcp.run()
