"""
Jira REST API client for the Agentic QA MCP server.

v3: adds create_issue() so the server can spawn tickets from transcripts.

Exposes:
- fetch_ticket(key)               : GET issue, flatten ADF to plain text
- attach_file(key, path)          : POST a file attachment
- add_comment(key, text)          : POST a plain-text comment
- create_issue(...)               : POST a new ticket with description + labels

Auth: Atlassian Cloud Basic Auth (email + API token).
Reads ATLASSIAN_* env vars (loaded from .env by server.py).
"""

import os
import sys
import httpx


def _base_url() -> str:
    site = os.environ.get("ATLASSIAN_SITE", "").strip()
    if not site:
        raise RuntimeError("ATLASSIAN_SITE is not set in environment.")
    if not site.startswith("http"):
        site = f"https://{site}"
    return f"{site}/rest/api/3"


def _auth() -> tuple[str, str]:
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    if not email or not token:
        raise RuntimeError("ATLASSIAN_EMAIL or ATLASSIAN_API_TOKEN missing.")
    return (email, token)


def _flatten_adf(node: dict) -> str:
    """Walk an Atlassian Document Format tree and emit plain text."""
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"

    text_parts = []
    for child in node.get("content", []):
        text_parts.append(_flatten_adf(child))
    inner = "".join(text_parts)

    if node_type == "listItem":
        return f"- {inner}\n"
    if node_type in ("paragraph", "heading", "bulletList", "orderedList"):
        return f"{inner}\n"

    return inner


def _text_to_adf(text: str) -> dict:
    """
    Convert plain text into a minimal ADF document.

    Splits on blank lines into paragraphs; lines starting with "- " inside
    a paragraph block are converted into a bullet list. Keeps the result
    visually similar to what a human would type.
    """
    blocks = []
    current_paragraph_lines: list[str] = []
    current_bullets: list[str] = []

    def flush_paragraph():
        nonlocal current_paragraph_lines
        if current_paragraph_lines:
            joined = "\n".join(current_paragraph_lines).strip()
            if joined:
                # Embed hardBreaks for in-paragraph newlines
                content = []
                first = True
                for line in joined.split("\n"):
                    if not first:
                        content.append({"type": "hardBreak"})
                    content.append({"type": "text", "text": line})
                    first = False
                blocks.append({"type": "paragraph", "content": content})
            current_paragraph_lines = []

    def flush_bullets():
        nonlocal current_bullets
        if current_bullets:
            blocks.append({
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [{
                            "type": "paragraph",
                            "content": [{"type": "text", "text": b}],
                        }],
                    }
                    for b in current_bullets
                ],
            })
            current_bullets = []

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.lstrip()

        if not stripped:
            # Blank line: end current block
            flush_paragraph()
            flush_bullets()
            continue

        if stripped.startswith("- "):
            # Bullet item: end paragraph if needed
            flush_paragraph()
            current_bullets.append(stripped[2:])
        else:
            # Regular text: end bullets if needed
            flush_bullets()
            current_paragraph_lines.append(line)

    flush_paragraph()
    flush_bullets()

    if not blocks:
        blocks = [{"type": "paragraph", "content": [{"type": "text", "text": text or ""}]}]

    return {
        "type": "doc",
        "version": 1,
        "content": blocks,
    }


def fetch_ticket(key: str) -> dict:
    """Fetch a Jira issue and return a flat dict."""
    url = f"{_base_url()}/issue/{key}"
    headers = {"Accept": "application/json"}

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, auth=_auth(), headers=headers)
        resp.raise_for_status()
        data = resp.json()

    fields = data.get("fields", {})

    description_text = _flatten_adf(fields.get("description") or {}).strip()
    status_name = (fields.get("status") or {}).get("name", "Unknown")

    site = os.environ.get("ATLASSIAN_SITE", "").replace("https://", "").strip()
    browse_url = f"https://{site}/browse/{key}"

    return {
        "key": data.get("key", key),
        "summary": fields.get("summary", ""),
        "description": description_text,
        "status": status_name,
        "url": browse_url,
    }


def add_comment(key: str, text: str) -> dict:
    """POST a plain-text comment to a Jira issue (wrapped in minimal ADF)."""
    url = f"{_base_url()}/issue/{key}/comment"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {"body": _text_to_adf(text)}

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, auth=_auth(), headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    return {"comment_id": data.get("id"), "created": data.get("created")}


def attach_file(key: str, file_path: str) -> dict:
    """Upload a file as an attachment on a Jira issue."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Attachment file not found: {file_path}")

    url = f"{_base_url()}/issue/{key}/attachments"
    headers = {"Accept": "application/json", "X-Atlassian-Token": "no-check"}
    filename = os.path.basename(file_path)

    with open(file_path, "rb") as f:
        files = {"file": (filename, f, "application/octet-stream")}
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, auth=_auth(), headers=headers, files=files)
            resp.raise_for_status()
            data = resp.json()

    if isinstance(data, list) and data:
        a = data[0]
        return {
            "attachment_id": a.get("id"),
            "filename": a.get("filename"),
            "size": a.get("size"),
        }
    return {"attachment_id": None, "filename": filename, "size": None}


def create_issue(
    project_key: str,
    summary: str,
    description_text: str,
    issue_type_name: str = "Tarea",
    labels: list[str] | None = None,
) -> dict:
    """
    Create a new Jira issue.

    Args:
        project_key: e.g. "KAN"
        summary: Ticket title.
        description_text: Plain-text body, will be converted to ADF.
        issue_type_name: Name of the issue type to use. Defaults to "Tarea"
                         (the Spanish "Task" type, which is what the KAN project has).
                         To use other types, pass their localized name as it appears
                         in the project (e.g. "Historia", "Error", "Epic").
        labels: Optional list of label strings. No spaces in labels (Jira rule).
                Used to encode metadata like "type:story", "type:bug".

    Returns:
        {"key": "KAN-7", "id": "10025", "url": "https://...browse/KAN-7"}
    """
    url = f"{_base_url()}/issue"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # Sanitize labels: Jira labels cannot contain spaces
    safe_labels = []
    for lbl in (labels or []):
        if lbl:
            safe_labels.append(lbl.replace(" ", "_"))

    fields: dict = {
        "project": {"key": project_key},
        "summary": summary[:250],  # Jira limit
        "issuetype": {"name": issue_type_name},
        "description": _text_to_adf(description_text),
    }
    if safe_labels:
        fields["labels"] = safe_labels

    payload = {"fields": fields}

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, auth=_auth(), headers=headers, json=payload)
        # Surface Jira's error body if something went wrong
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Jira create_issue failed ({resp.status_code}): {resp.text}"
            )
        data = resp.json()

    site = os.environ.get("ATLASSIAN_SITE", "").replace("https://", "").strip()
    browse_url = f"https://{site}/browse/{data.get('key')}"

    return {
        "key": data.get("key"),
        "id": data.get("id"),
        "url": browse_url,
    }


# Smoke test: python -m src.jira_client KAN-6
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: python -m src.jira_client <TICKET-KEY>", file=sys.stderr)
        sys.exit(1)

    ticket = fetch_ticket(sys.argv[1])
    print(f"Key:     {ticket['key']}", file=sys.stderr)
    print(f"Status:  {ticket['status']}", file=sys.stderr)
    print(f"Summary: {ticket['summary']}", file=sys.stderr)
    print(f"URL:     {ticket['url']}", file=sys.stderr)
    print("---DESCRIPTION---", file=sys.stderr)
    print(ticket["description"], file=sys.stderr)
