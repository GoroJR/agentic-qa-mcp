"""
Jira REST API client for the Agentic QA MCP server.

v4: adds image-attachment downloading so the test generator can see what
the UI actually looks like.

Public functions:
- fetch_ticket(key)                       : GET issue (text only, fast)
- fetch_ticket_with_attachments(key)      : GET issue + download image attachments
- create_issue(...)                       : POST a new ticket
- add_comment(key, text)                  : POST a comment
- attach_file(key, path)                  : POST a file attachment
"""

import os
import sys
import tempfile
import httpx


# Images this large or larger get downsized before being sent to the LLM
# to keep token costs under control. Claude vision recommends <= 1568px on
# the longest side for normal screenshots.
MAX_IMAGE_LONG_SIDE_PX = 1568


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
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type", "")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    text_parts = [_flatten_adf(child) for child in node.get("content", [])]
    inner = "".join(text_parts)
    if node_type == "listItem":
        return f"- {inner}\n"
    if node_type in ("paragraph", "heading", "bulletList", "orderedList"):
        return f"{inner}\n"
    return inner


def _text_to_adf(text: str) -> dict:
    """Convert plain text into a minimal ADF doc with paragraphs + bullet lists."""
    blocks = []
    current_paragraph_lines: list[str] = []
    current_bullets: list[str] = []

    def flush_paragraph():
        nonlocal current_paragraph_lines
        if current_paragraph_lines:
            joined = "\n".join(current_paragraph_lines).strip()
            if joined:
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
                    {"type": "listItem", "content": [
                        {"type": "paragraph",
                         "content": [{"type": "text", "text": b}]},
                    ]}
                    for b in current_bullets
                ],
            })
            current_bullets = []

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if not stripped:
            flush_paragraph()
            flush_bullets()
            continue
        if stripped.startswith("- "):
            flush_paragraph()
            current_bullets.append(stripped[2:])
        else:
            flush_bullets()
            current_paragraph_lines.append(line)

    flush_paragraph()
    flush_bullets()

    if not blocks:
        blocks = [{"type": "paragraph",
                   "content": [{"type": "text", "text": text or ""}]}]
    return {"type": "doc", "version": 1, "content": blocks}


def fetch_ticket(key: str) -> dict:
    """Fetch a Jira issue and return text fields. Does NOT download attachments."""
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
    return {
        "key": data.get("key", key),
        "summary": fields.get("summary", ""),
        "description": description_text,
        "status": status_name,
        "url": f"https://{site}/browse/{key}",
    }


def _maybe_resize_image(path: str, max_long_side: int = MAX_IMAGE_LONG_SIDE_PX) -> str:
    """
    Resize an image if its long side exceeds max_long_side, in place.
    Uses PIL if installed; otherwise returns the original path unchanged.
    """
    try:
        from PIL import Image
    except ImportError:
        # Pillow not installed - skip resizing
        return path

    try:
        with Image.open(path) as img:
            w, h = img.size
            long_side = max(w, h)
            if long_side <= max_long_side:
                return path

            # Compute new size preserving aspect ratio
            scale = max_long_side / long_side
            new_size = (int(w * scale), int(h * scale))

            # Preserve transparency for PNGs
            resized = img.resize(new_size, Image.LANCZOS)
            resized.save(path, optimize=True)
            return path
    except Exception:
        # If anything goes wrong, just return the original - we'd rather send
        # a too-big image than fail the whole pipeline.
        return path


def fetch_ticket_with_attachments(
    key: str,
    download_dir: str | None = None,
    include_mime_prefixes: tuple[str, ...] = ("image/",),
) -> dict:
    """
    Fetch a Jira issue AND download attachments matching the given mime prefixes
    to a local folder. Returns the standard ticket dict plus an `images` list
    of local file paths.

    Args:
        key: Jira issue key.
        download_dir: Where to save downloaded files. Defaults to a per-ticket
                      subfolder under the system temp dir.
        include_mime_prefixes: Tuple of mime-type prefixes to download.
                               Default is just images. Pass e.g. ("image/", "application/pdf")
                               to also include PDFs in the future.

    Returns:
        {
          "key", "summary", "description", "status", "url",   # same as fetch_ticket
          "images": [                                          # NEW
              {"filename": "...", "local_path": "...", "mime_type": "...", "size": ...},
              ...
          ],
          "skipped_attachments": [                             # NEW (non-image files)
              {"filename": "...", "mime_type": "..."},
              ...
          ]
        }
    """
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

    # Prepare download directory
    if download_dir is None:
        download_dir = os.path.join(tempfile.gettempdir(), "agentic-qa", key)
    os.makedirs(download_dir, exist_ok=True)

    images: list[dict] = []
    skipped: list[dict] = []

    attachments = fields.get("attachment") or []

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for att in attachments:
            mime = att.get("mimeType", "")
            filename = att.get("filename", "unknown")
            content_url = att.get("content")

            if not content_url:
                skipped.append({"filename": filename, "mime_type": mime,
                                "reason": "no content url"})
                continue

            if not any(mime.startswith(p) for p in include_mime_prefixes):
                skipped.append({"filename": filename, "mime_type": mime})
                continue

            # Build a local path, prefixing with attachment id for uniqueness
            local_name = f"{att.get('id', 'x')}_{filename}"
            local_path = os.path.join(download_dir, local_name)

            # Don't re-download if cached and same size
            existing_size = os.path.getsize(local_path) if os.path.exists(local_path) else -1
            if existing_size != att.get("size"):
                # Stream the download
                with client.stream("GET", content_url, auth=_auth()) as r:
                    r.raise_for_status()
                    with open(local_path, "wb") as f:
                        for chunk in r.iter_bytes(chunk_size=64 * 1024):
                            f.write(chunk)

            # Resize if huge (no-op if pillow missing)
            _maybe_resize_image(local_path)

            images.append({
                "filename": filename,
                "local_path": local_path,
                "mime_type": mime,
                "size": os.path.getsize(local_path),
            })

    return {
        "key": data.get("key", key),
        "summary": fields.get("summary", ""),
        "description": description_text,
        "status": status_name,
        "url": f"https://{site}/browse/{key}",
        "images": images,
        "skipped_attachments": skipped,
    }


def add_comment(key: str, text: str) -> dict:
    url = f"{_base_url()}/issue/{key}/comment"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {"body": _text_to_adf(text)}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, auth=_auth(), headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return {"comment_id": data.get("id"), "created": data.get("created")}


def attach_file(key: str, file_path: str) -> dict:
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
        return {"attachment_id": a.get("id"), "filename": a.get("filename"),
                "size": a.get("size")}
    return {"attachment_id": None, "filename": filename, "size": None}


def create_issue(
    project_key: str,
    summary: str,
    description_text: str,
    issue_type_name: str = "Tarea",
    labels: list[str] | None = None,
) -> dict:
    url = f"{_base_url()}/issue"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    safe_labels = [lbl.replace(" ", "_") for lbl in (labels or []) if lbl]
    fields: dict = {
        "project": {"key": project_key},
        "summary": summary[:250],
        "issuetype": {"name": issue_type_name},
        "description": _text_to_adf(description_text),
    }
    if safe_labels:
        fields["labels"] = safe_labels
    payload = {"fields": fields}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, auth=_auth(), headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Jira create_issue failed ({resp.status_code}): {resp.text}")
        data = resp.json()
    site = os.environ.get("ATLASSIAN_SITE", "").replace("https://", "").strip()
    return {
        "key": data.get("key"),
        "id": data.get("id"),
        "url": f"https://{site}/browse/{data.get('key')}",
    }


# Smoke tests
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: python -m src.jira_client <TICKET-KEY> [--with-images]", file=sys.stderr)
        sys.exit(1)

    key = sys.argv[1]

    if "--with-images" in sys.argv:
        t = fetch_ticket_with_attachments(key)
        print(f"Key:      {t['key']}", file=sys.stderr)
        print(f"Summary:  {t['summary']}", file=sys.stderr)
        print(f"Status:   {t['status']}", file=sys.stderr)
        print(f"URL:      {t['url']}", file=sys.stderr)
        print(f"\nImages downloaded: {len(t['images'])}", file=sys.stderr)
        for img in t["images"]:
            print(f"  - {img['filename']:30s} -> {img['local_path']}  ({img['size']} bytes)",
                  file=sys.stderr)
        if t["skipped_attachments"]:
            print(f"\nSkipped (non-image) attachments: {len(t['skipped_attachments'])}",
                  file=sys.stderr)
            for s in t["skipped_attachments"]:
                print(f"  - {s.get('filename'):30s} | {s.get('mime_type')}", file=sys.stderr)
    else:
        t = fetch_ticket(key)
        print(f"Key:     {t['key']}", file=sys.stderr)
        print(f"Status:  {t['status']}", file=sys.stderr)
        print(f"Summary: {t['summary']}", file=sys.stderr)
        print(f"URL:     {t['url']}", file=sys.stderr)
        print("---DESCRIPTION---", file=sys.stderr)
        print(t["description"], file=sys.stderr)
