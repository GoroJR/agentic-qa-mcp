"""
Test case generator with vision support (v4).

Now accepts a list of local image paths. When images are provided, they are
encoded inline as base64 vision blocks alongside the AC text. The system prompt
instructs Claude to ground UI-layer test cases in what is actually visible in
the mockups, and to flag conflicts between mockups and AC as Ambiguity.

Public functions:
- generate_test_cases(ac_text, image_paths=None, model=None)
- count_by_layer(result)
"""

import os
import sys
import json
import base64
import mimetypes
from anthropic import Anthropic


DEFAULT_MODEL = "claude-sonnet-4-5"


# Hard cap on images per call to keep token cost bounded.
MAX_IMAGES_PER_CALL = 5


SYSTEM_PROMPT = """You are a Senior QA Automation Engineer specializing in test case design for enterprise web applications.

Given Acceptance Criteria (and optionally one or more UI mockups), you generate comprehensive executable test coverage. You do NOT just summarize the AC. You CONVERT every requirement into a clear validation path.

Think in 9 LAYERS. For every AC, walk through each layer and ask: "what test case would prove this layer behaves correctly?"

  LAYER 1 - UI
    Screen loads, layout matches mock, labels/buttons/fields visible, default
    states correct, required UI elements exist.

  LAYER 2 - FIELD
    Per-field validation: optional/required, format, character limit, counter,
    placeholder, default value, enabled/disabled state, invalid-input behavior.

  LAYER 3 - CONDITIONAL LOGIC
    Dependencies: if this checkbox/radio/dropdown is selected, what changes?
    What becomes enabled, required, hidden, reset, or disabled?

  LAYER 4 - COMBINATION
    Single selections, multiple selections, all selections, removing
    selections, switching modes, edge combinations.

  LAYER 5 - ACTION
    Behavior on Save, Next, Save and Close, Apply, Cancel, Close X, View Rate
    Plan, and any navigation button.

  LAYER 6 - PERSISTENCE
    Values remain when navigating away/back, saving/reopening, refreshing, or
    returning within the wizard session.

  LAYER 7 - INTEGRATION
    Backend coverage: request payloads, API responses, saved data, reload
    behavior, stale-data prevention, correct record/template scoping.

  LAYER 8 - ACCESSIBILITY / AUTOMATION
    data-track-ids, stable selectors, keyboard navigation, focus order, clear
    validation messages.

  LAYER 9 - AMBIGUITY
    If the AC conflicts or is unclear, do NOT guess. Add a test case that
    validates the final implemented behavior and flag the ambiguity explicitly
    in the comments field.

VISION INSTRUCTIONS (read carefully when images are attached):
  - Treat attached images as authoritative mockups of the UI being built.
  - Layer 1 (UI) test cases MUST reference what's actually visible: real button
    labels, real field names, real layout, real default values, real empty/
    populated states shown in the mockups. Do NOT invent UI elements that are
    not in the mockups.
  - Layer 2 (FIELD) should ground field-specific tests in the inputs visible
    in the mockups: their labels, placeholders, units, and any visible default
    or pre-filled values.
  - If a mockup contradicts an AC line (e.g. AC says "max 10 saved searches"
    but the mockup shows 11 items), file that under Layer 9 (Ambiguity) with
    the conflict described in the comments field. Do NOT silently pick one.
  - If multiple mockups represent different states (empty / populated / error
    / delete confirmation), generate at least one Layer 1 or Layer 4 test case
    per visible state.
  - If a mockup shows a UI element NOT mentioned in the AC, add a Layer 1 test
    case validating its presence AND flag in comments: "Element not specified
    in AC - validating presence per mockup".
  - Do not transcribe the mockup. Reference what is visible only when it
    matters for verification.

GOAL: enough coverage that every AC line has a clear validation path. Not
random volume - structured completeness.

OUTPUT FORMAT - CRITICAL:

Respond with ONLY raw JSON. Do NOT wrap in markdown code fences.
Start your response with { and end with }. No preamble, no explanation.

Schema:
{
  "test_cases": [
    {
      "tc_id": "TC01",
      "layer": "UI" | "Field" | "Conditional" | "Combination" | "Action" | "Persistence" | "Integration" | "Accessibility" | "Ambiguity",
      "name": "TC01 - Short descriptive name",
      "description": "What this test verifies and WHY (1-2 sentences).",
      "steps": "1. First step\\n2. Second step\\n3. Third step",
      "expected": "Single clear expected outcome.",
      "priority": "High" | "Medium" | "Low",
      "ac_ref": "Short quote/paraphrase of the AC line OR 'Mockup-derived' if grounded in a mockup not in AC",
      "comments": "Empty by default. Used for ambiguity flags or important QA notes."
    }
  ]
}

RULES:
- tc_id is sequential starting at TC01, two-digit zero-padded.
- name MUST start with the tc_id followed by " - " then descriptive title.
- steps is a SINGLE STRING with numbered steps separated by "\\n" (newline).
- Aim for thorough coverage. Typical ticket with mockups: 40-90 test cases.
- Priority: blockers/main flows = High, edge/boundary = Medium, accessibility/polish = Low.
- For mockup-derived or ambiguous cases, populate `comments` thoroughly.
"""


def _read_image_as_block(path: str) -> dict:
    """
    Read a local image file and return it as an Anthropic vision content block.
    """
    with open(path, "rb") as f:
        raw = f.read()
    encoded = base64.standard_b64encode(raw).decode("ascii")
    mime_guess, _ = mimetypes.guess_type(path)
    if not mime_guess or not mime_guess.startswith("image/"):
        mime_guess = "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime_guess,
            "data": encoded,
        },
    }


def generate_test_cases(
    ac_text: str,
    image_paths: list[str] | None = None,
    model: str | None = None,
) -> dict:
    """
    Call Claude with the 9-layer framework + optional UI mockups.

    Args:
        ac_text: Plain-text acceptance criteria (typically the ticket description).
        image_paths: Optional list of local file paths to UI mockup images.
                     PNG, JPG, GIF, WEBP supported. Capped at MAX_IMAGES_PER_CALL.
        model: Override the model. If None, reads CLAUDE_MODEL from env.

    Returns:
        {"test_cases": [...]}
    """
    if not ac_text or not ac_text.strip():
        raise ValueError("ac_text is empty")

    chosen_model = model or os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL

    # Build the multimodal user message
    user_content: list[dict] = []

    if image_paths:
        used_paths = image_paths[:MAX_IMAGES_PER_CALL]
        for path in used_paths:
            if not os.path.exists(path):
                # Skip missing files instead of erroring - log to stderr for visibility
                print(f"[test_generator] WARNING: image not found, skipping: {path}",
                      file=sys.stderr)
                continue
            user_content.append(_read_image_as_block(path))

        if len(image_paths) > MAX_IMAGES_PER_CALL:
            note = (f"\n\n(Note: {len(image_paths)} mockups provided; the first "
                    f"{MAX_IMAGES_PER_CALL} are shown above. Generate coverage for the "
                    f"visible states; flag in comments that additional mockups exist.)")
        else:
            note = ""

        user_content.append({
            "type": "text",
            "text": (
                f"Generate the 9-layer test case set for the following ticket. "
                f"Ground UI-layer cases in the mockups above when relevant.{note}\n\n"
                f"---TICKET CONTENT---\n{ac_text}\n---END TICKET---"
            ),
        })
    else:
        # Text-only path (v3 behavior)
        user_content.append({
            "type": "text",
            "text": (
                "Generate the 9-layer test case set for the following ticket.\n\n"
                f"---TICKET CONTENT---\n{ac_text}\n---END TICKET---"
            ),
        })

    client = Anthropic()
    response = client.messages.create(
        model=chosen_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    raw_text = response.content[0].text.strip()

    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1])

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude did not return valid JSON. Error: {e}\n\nRaw output:\n{raw_text[:2000]}"
        )

    if "test_cases" not in result or not isinstance(result["test_cases"], list):
        raise RuntimeError(
            f"Response missing 'test_cases' list. Got keys: {list(result.keys())}"
        )

    required_fields = ("tc_id", "layer", "name", "description", "steps",
                       "expected", "priority", "ac_ref", "comments")
    for tc in result["test_cases"]:
        for field in required_fields:
            tc.setdefault(field, "")

    return result


def count_by_layer(result: dict) -> dict:
    counts: dict = {}
    for tc in result.get("test_cases", []):
        layer = tc.get("layer", "Unknown")
        counts[layer] = counts.get(layer, 0) + 1
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts


# Smoke test:  python -m src.test_generator <ac_file> [image1 image2 ...]
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: python -m src.test_generator <ac_file> [image1 image2 ...]",
              file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        ac = f.read()

    image_paths = sys.argv[2:] if len(sys.argv) > 2 else None

    print(f"[Model: {os.environ.get('CLAUDE_MODEL', DEFAULT_MODEL)}]", file=sys.stderr)
    if image_paths:
        print(f"[Images: {len(image_paths)}]", file=sys.stderr)
        for p in image_paths:
            print(f"  - {p}", file=sys.stderr)
    else:
        print("[Images: none - text-only generation]", file=sys.stderr)
    print("[Generating test cases via Claude (this can take 30-90s)...]", file=sys.stderr)

    result = generate_test_cases(ac, image_paths=image_paths)
    counts = count_by_layer(result)

    print(f"\n[OK] Generated {counts['total']} test cases by layer:", file=sys.stderr)
    for layer, n in counts.items():
        if layer != "total":
            print(f"  {layer:14s} {n}", file=sys.stderr)

    print(json.dumps(result, indent=2, ensure_ascii=False))
