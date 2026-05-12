"""
Jira REST API client for the Agentic QA MCP server.

Exposes three helpers used by the MCP tools:
- fetch_ticket(key)        : GET issue, flatten ADF to plain text
- attach_file(key, path)   : POST a file attachment to a ticket
- add_comment(key, text)   : POST a plain-text comment to a ticket

Auth uses Atlassian Cloud's Basic Auth scheme: email + API token.
Reads ATLASSIAN_* values from environment (loaded from .env by server.py).
"""

import os
import sys
import httpx


def _base_url() -> str:
    """Build the Jira API base URL from the ATLASSIAN_SITE env var."""
    site = os.environ.get("ATLASSIAN_SITE", "").strip()
    if not site:
        raise RuntimeError("ATLASSIAN_SITE is not set in environment.")
    # Allow either "rooy15.atlassian.net" or full URL
    if not site.startswith("http"):
        site = f"https://{site}"
    return f"{site}/rest/api/3"


def _auth() -> tuple[str, str]:
    """Read email + token from environment for Basic Auth."""
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    if not email or not token:
        raise RuntimeError("ATLASSIAN_EMAIL or ATLASSIAN_API_TOKEN missing.")
    return (email, token)


def _flatten_adf(node: dict) -> str:
    """
    Atlassian Document Format (ADF) is a nested JSON tree.
    Walk it recursively and collect plain text, preserving line breaks
    for paragraphs, lists, and headings.
    """
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    text_parts = []

    # Leaf: actual text node
    if node_type == "text":
        return node.get("text", "")

    # Hard break inside a paragraph
    if node_type == "hardBreak":
        return "\n"

    # Recurse into children
    children = node.get("content", [])
    for child in children:
        text_parts.append(_flatten_adf(child))

    inner = "".join(text_parts)

    # Block-level types get newlines around them
    block_types = {"paragraph", "heading", "listItem", "bulletList", "orderedList"}
    if node_type in block_types:
        # Bullet/numbered items get a leading "- " for readability
        if node_type == "listItem":
            return f"- {inner}\n"
        return f"{inner}\n"

    return inner


def fetch_ticket(key: str) -> dict:
    """
    Fetch a single Jira issue and return a flat dict:
      {
        "key": "KAN-6",
        "summary": "...",
        "description": "...full flattened text...",
        "status": "Por hacer",
        "url": "https://rooy15.atlassian.net/browse/KAN-6"
      }
    """
    url = f"{_base_url()}/issue/{key}"
    headers = {"Accept": "application/json"}

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, auth=_auth(), headers=headers)
        resp.raise_for_status()
        data = resp.json()

    fields = data.get("fields", {})

    description_node = fields.get("description") or {}
    description_text = _flatten_adf(description_node).strip()

    status_obj = fields.get("status") or {}
    status_name = status_obj.get("name", "Unknown")

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
    """
    POST a plain-text comment to a Jira issue.
    Returns {comment_id, created}.
    """
    url = f"{_base_url()}/issue/{key}/comment"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # Jira v3 requires ADF for comment bodies. Wrap text in a minimal ADF doc.
    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }
    }

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, auth=_auth(), headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    return {
        "comment_id": data.get("id"),
        "created": data.get("created"),
    }


def attach_file(key: str, file_path: str) -> dict:
    """
    Upload a file as an attachment on a Jira issue.
    Returns {attachment_id, filename, size}.

    Note: Jira requires the special X-Atlassian-Token: no-check header
    for file uploads, and the multipart field name MUST be 'file'.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Attachment file not found: {file_path}")

    url = f"{_base_url()}/issue/{key}/attachments"
    headers = {
        "Accept": "application/json",
        "X-Atlassian-Token": "no-check",
    }

    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        files = {"file": (filename, f, "application/octet-stream")}
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, auth=_auth(), headers=headers, files=files)
            resp.raise_for_status()
            data = resp.json()

    # API returns a list of attachment objects (you can upload multiple at once)
    if isinstance(data, list) and data:
        a = data[0]
        return {
            "attachment_id": a.get("id"),
            "filename": a.get("filename"),
            "size": a.get("size"),
        }

    return {"attachment_id": None, "filename": filename, "size": None}


# Smoke test: run `python -m src.jira_client KAN-6` from project root
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