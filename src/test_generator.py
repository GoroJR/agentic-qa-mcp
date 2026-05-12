"""
Test case generator using the Claude API with the RMT QA 9-layer framework.

Generates production-grade test cases organized by layer:
  1. UI         - screen load, layout, labels, default states
  2. Field      - per-field validation, format, limits, defaults
  3. Conditional - dependencies, enable/disable, hide/show, required-when
  4. Combination - single/multi/all/remove/switch selections
  5. Action     - Save, Next, Save and Close, Apply, Cancel, Close X, navigation
  6. Persistence - navigate away/back, save/reopen, refresh, session retention
  7. Integration - request payloads, API responses, saved data, reload, scoping
  8. Accessibility - data-track-ids, stable selectors, keyboard nav, focus order
  9. Ambiguity  - conflicting/unclear AC flagged with explicit comments

Output schema is a FLAT list (not bucketed). Each test case carries a `layer`
field so downstream code can group/sort/filter in Excel.
"""

import os
import sys
import json
from anthropic import Anthropic


# Model is swappable via env. Default to Sonnet for demo quality; switch to
# claude-haiku-4-5 in .env for cheap iteration during prompt tuning.
DEFAULT_MODEL = "claude-sonnet-4-5"


SYSTEM_PROMPT = """You are a Senior QA Automation Engineer specializing in test case design for enterprise web applications (the RMT - Rate Management Tool - domain at Choice Hotels is a good mental reference point).

Given Acceptance Criteria, you generate comprehensive executable test coverage. You do NOT just summarize the AC. You CONVERT every requirement into a clear validation path.

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
      "name": "TC01 - Short descriptive name (mirror real-world style: 'TCxx - <what is verified>')",
      "description": "What this test verifies and WHY (1-2 sentences).",
      "steps": "1. First step\\n2. Second step\\n3. Third step",
      "expected": "Single clear expected outcome.",
      "priority": "High" | "Medium" | "Low",
      "ac_ref": "Short quote or paraphrase of the AC line this validates (or 'Implicit' if derived from layer)",
      "comments": "Empty string by default. Used ONLY for ambiguity flags or important QA notes."
    }
  ]
}

RULES:
- tc_id is sequential starting at TC01, two-digit zero-padded (TC01, TC02 ... TC10, TC11).
- name MUST start with the tc_id followed by " - " then the descriptive title.
- steps is a SINGLE STRING with numbered steps separated by "\\n" (newline).
- Aim for thorough coverage. Typical ticket: 30-80 test cases depending on AC complexity.
- Priority guidelines: blockers/main flows = High, edge/boundary/relative-rules = Medium, accessibility/polish/regression-isolation = Low.
- For ambiguous AC, mark layer="Ambiguity" and put the conflict explanation in comments.
"""


def generate_test_cases(ac_text: str, model: str | None = None) -> dict:
    """
    Call Claude with the 9-layer framework and return structured test cases.

    Args:
        ac_text: Plain-text ticket description / acceptance criteria.
        model: Override the model. If None, reads CLAUDE_MODEL from env,
               falling back to DEFAULT_MODEL.

    Returns:
        {"test_cases": [ {tc_id, layer, name, description, steps, expected,
                          priority, ac_ref, comments}, ... ]}

    Raises:
        ValueError if ac_text is empty.
        RuntimeError if Claude's response is not valid JSON.
    """
    if not ac_text or not ac_text.strip():
        raise ValueError("ac_text is empty")

    chosen_model = model or os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL

    # Anthropic client auto-reads ANTHROPIC_API_KEY from env (loaded by server.py)
    client = Anthropic()

    response = client.messages.create(
        model=chosen_model,
        max_tokens=16000,  # 9-layer output can be long; give headroom
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Generate the 9-layer test case set for the following ticket.\n\n"
                    f"---TICKET CONTENT---\n{ac_text}\n---END TICKET---"
                ),
            }
        ],
    )

    raw_text = response.content[0].text.strip()

    # Defensive: strip markdown fences if Claude wraps the JSON despite instructions
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1])

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude did not return valid JSON. Error: {e}\n\nRaw output:\n{raw_text[:2000]}"
        )

    # Defensive: ensure top-level shape
    if "test_cases" not in result or not isinstance(result["test_cases"], list):
        raise RuntimeError(
            f"Response missing 'test_cases' list. Got keys: {list(result.keys())}"
        )

    # Defensive: ensure every TC has the required fields (fill missing with safe defaults)
    required_fields = ("tc_id", "layer", "name", "description", "steps",
                       "expected", "priority", "ac_ref", "comments")
    for tc in result["test_cases"]:
        for field in required_fields:
            tc.setdefault(field, "")

    return result


def count_by_layer(result: dict) -> dict:
    """Return {layer_name: count, ..., 'total': N} for quick summaries."""
    counts: dict = {}
    for tc in result.get("test_cases", []):
        layer = tc.get("layer", "Unknown")
        counts[layer] = counts.get(layer, 0) + 1
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts


# Smoke test: python -m src.test_generator <optional file path>
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            ac = f.read()
    else:
        print("Paste AC. End with Ctrl+Z then Enter (Windows) or Ctrl+D (Unix):", file=sys.stderr)
        ac = sys.stdin.read()

    print(f"[Model: {os.environ.get('CLAUDE_MODEL', DEFAULT_MODEL)}]", file=sys.stderr)
    print("[Generating test cases via Claude (this can take 30-60s)...]", file=sys.stderr)

    result = generate_test_cases(ac)
    counts = count_by_layer(result)

    print(f"\n[OK] Generated {counts['total']} test cases by layer:", file=sys.stderr)
    for layer, n in counts.items():
        if layer != "total":
            print(f"  {layer:14s} {n}", file=sys.stderr)

    # Echo full JSON to stdout for piping/inspection
    print(json.dumps(result, indent=2, ensure_ascii=False))