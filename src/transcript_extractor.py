"""
Extract structured Acceptance Criteria from a meeting transcript (Zoom AI Companion,
Google Meet, Teams, etc) using the Claude API.

Public functions:
- extract_ac(transcript_text)  -> structured extraction dict
- refine_ac(prior_extraction, user_answers_text)  -> refined extraction dict
"""

import os
import sys
import json
from anthropic import Anthropic


DEFAULT_MODEL = "claude-sonnet-4-5"


EXTRACTION_SYSTEM_PROMPT = """You are a Senior Product Owner / Business Analyst who turns messy meeting transcripts into clean, testable user stories.

You will receive a raw transcript of a refinement / grooming / discovery call. Your job:

1. Identify what feature, story, or bug the team actually discussed and aligned on.
2. Extract the user story in standard form: "As a <role>, I want <capability> so that <benefit>".
3. Extract every distinct acceptance criterion the team agreed on (or implied).
4. Flag uncertainty: if the team did not actually agree on something, mark it as a low-confidence AC OR move it to open_questions.
5. Detect the issue type from context: Story (new capability), Bug (defect), or Task (refactor / chore).

GOLDEN RULES:
- Do NOT invent requirements that were not discussed.
- Do NOT collapse two distinct ACs into one.
- If two team members said conflicting things and didn't resolve, that goes in `ambiguity_flags`, not in the AC list.
- If a requirement was mentioned but not actually agreed on, that's an `open_question`.
- Use the language the team used. If they said "rate plan", say "rate plan", not "pricing model".

OUTPUT FORMAT - CRITICAL:

Respond with ONLY raw JSON. No markdown fences. Start with { and end with }.

Schema:
{
  "issue_type": "Story" | "Bug" | "Task",
  "issue_type_reasoning": "Brief why - e.g. 'Team discussed defect in existing cancellation flow' or 'Net-new search filter capability'",
  "title": "Short imperative title, max 80 chars, mirrors Jira convention",
  "user_story": "As a <role>, I want <capability> so that <benefit>. (Single sentence.)",
  "acceptance_criteria": [
    {
      "text": "Single concrete testable statement, in the team's own language",
      "confidence": "high" | "medium" | "low",
      "speaker_attribution": "Who said it, e.g. 'Maria (PM)' or 'group consensus' or 'unattributed'"
    }
  ],
  "open_questions": [
    "Question 1 the team did not resolve",
    "Question 2 ..."
  ],
  "implicit_assumptions": [
    "Assumption 1 the team seemed to take for granted but never stated",
    "..."
  ],
  "ambiguity_flags": [
    "Specific conflict, e.g. 'Maria said 24h cutoff, Tom said 48h - not resolved'"
  ],
  "estimated_complexity": "S" | "M" | "L" | "XL",
  "out_of_scope": [
    "Things explicitly called out as not in this story"
  ]
}

CONFIDENCE GUIDELINES:
- high: team explicitly agreed, multiple speakers confirmed, no objections
- medium: one speaker proposed, no objection but no explicit confirmation either
- low: implied or assumed, derived from context not directly stated

If the transcript is too short or too off-topic to extract anything meaningful, return:
{
  "issue_type": "Unknown",
  "title": "Unable to extract - transcript insufficient",
  "user_story": "",
  "acceptance_criteria": [],
  "open_questions": ["What was the goal of this meeting?"],
  "implicit_assumptions": [],
  "ambiguity_flags": ["Transcript does not contain clear requirements discussion"],
  "estimated_complexity": "S",
  "out_of_scope": []
}
"""


REFINEMENT_SYSTEM_PROMPT = """You are the same Senior Product Owner / Business Analyst as before. You previously extracted requirements from a meeting transcript and flagged some open questions and ambiguities.

The user has now provided answers to those open questions. Your job:

1. Re-extract the requirements, incorporating the new answers.
2. Promote any low-confidence ACs to high confidence if the new answers clarify them.
3. Resolve ambiguity_flags where possible. Move resolved ones into acceptance_criteria.
4. Remove answered open_questions from the open_questions list.
5. If new questions surface from the answers, add them to open_questions.

OUTPUT FORMAT: Same JSON schema as the original extraction. No markdown fences. Start with { and end with }.
"""


def extract_ac(transcript_text: str, model: str | None = None) -> dict:
    """
    Run the Claude extraction on a raw transcript.

    Args:
        transcript_text: Plain text transcript. Speaker labels and timestamps optional.
        model: Override model, else CLAUDE_MODEL env var, else DEFAULT_MODEL.

    Returns:
        Structured extraction dict matching the schema in the system prompt.

    Raises:
        ValueError if transcript is empty.
        RuntimeError if Claude returns invalid JSON.
    """
    if not transcript_text or not transcript_text.strip():
        raise ValueError("transcript_text is empty")

    chosen_model = model or os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL

    client = Anthropic()

    response = client.messages.create(
        model=chosen_model,
        max_tokens=8000,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Extract the structured requirements from this transcript.\n\n"
                    f"---TRANSCRIPT START---\n{transcript_text}\n---TRANSCRIPT END---"
                ),
            }
        ],
    )

    raw_text = response.content[0].text.strip()

    # Defensive markdown-fence stripping (same lesson from v1/v2)
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1])

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude did not return valid JSON. Error: {e}\n\nRaw:\n{raw_text[:2000]}"
        )

    # Defensive defaults
    required_keys = ("issue_type", "title", "user_story", "acceptance_criteria",
                     "open_questions", "implicit_assumptions", "ambiguity_flags",
                     "estimated_complexity", "out_of_scope")
    for key in required_keys:
        result.setdefault(key, [] if key.endswith("s") or key.endswith("flags") or key.endswith("scope") else "")

    if not isinstance(result.get("acceptance_criteria"), list):
        result["acceptance_criteria"] = []

    return result


def refine_ac(prior_extraction: dict, user_answers_text: str, model: str | None = None) -> dict:
    """
    Refine a prior extraction with user-provided answers to open questions.

    Args:
        prior_extraction: The dict returned by extract_ac().
        user_answers_text: Plain text answers from the user, free form.
        model: Optional model override.

    Returns:
        New extraction dict with same schema, but updated.
    """
    if not user_answers_text or not user_answers_text.strip():
        raise ValueError("user_answers_text is empty")

    chosen_model = model or os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL

    client = Anthropic()

    user_msg = (
        "Here is the prior extraction:\n\n"
        f"```json\n{json.dumps(prior_extraction, indent=2, ensure_ascii=False)}\n```\n\n"
        "Here are my answers to the open questions and ambiguity flags:\n\n"
        f"---ANSWERS START---\n{user_answers_text}\n---ANSWERS END---\n\n"
        "Return the refined extraction as JSON."
    )

    response = client.messages.create(
        model=chosen_model,
        max_tokens=8000,
        system=REFINEMENT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw_text = response.content[0].text.strip()

    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1])

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude did not return valid JSON. Error: {e}\n\nRaw:\n{raw_text[:2000]}"
        )

    return result


def format_extraction_as_jira_description(extraction: dict) -> str:
    """
    Render the extraction into plain-text Markdown-ish format suitable for the
    Jira ticket Description field.

    Used by create_jira_ticket so the ticket body looks like a real human ticket,
    not a JSON dump.
    """
    parts = []

    if extraction.get("user_story"):
        parts.append("User Story:")
        parts.append(extraction["user_story"])
        parts.append("")

    parts.append("Acceptance Criteria:")
    for ac in extraction.get("acceptance_criteria", []):
        conf = ac.get("confidence", "")
        text = ac.get("text", "")
        # Mark low-confidence ACs visually
        if conf == "low":
            parts.append(f"- {text} (confidence: low - confirm with team)")
        elif conf == "medium":
            parts.append(f"- {text} (confidence: medium)")
        else:
            parts.append(f"- {text}")
    parts.append("")

    if extraction.get("open_questions"):
        parts.append("Open Questions:")
        for q in extraction["open_questions"]:
            parts.append(f"- {q}")
        parts.append("")

    if extraction.get("implicit_assumptions"):
        parts.append("Implicit Assumptions:")
        for a in extraction["implicit_assumptions"]:
            parts.append(f"- {a}")
        parts.append("")

    if extraction.get("ambiguity_flags"):
        parts.append("Ambiguity / Conflicts to Resolve:")
        for flag in extraction["ambiguity_flags"]:
            parts.append(f"- {flag}")
        parts.append("")

    if extraction.get("out_of_scope"):
        parts.append("Out of Scope:")
        for o in extraction["out_of_scope"]:
            parts.append(f"- {o}")
        parts.append("")

    complexity = extraction.get("estimated_complexity", "")
    if complexity:
        parts.append(f"Estimated Complexity: {complexity}")

    # Add provenance footer so anyone opening the ticket knows it came from a transcript
    parts.append("")
    parts.append("---")
    parts.append("Generated from meeting transcript via agentic-qa-mcp.")

    return "\n".join(parts)


# Smoke test:  python -m src.transcript_extractor sample_transcript.txt
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            transcript = f.read()
    else:
        print("Paste transcript. End with Ctrl+Z + Enter (Windows) or Ctrl+D (Unix):", file=sys.stderr)
        transcript = sys.stdin.read()

    print(f"[Model: {os.environ.get('CLAUDE_MODEL', DEFAULT_MODEL)}]", file=sys.stderr)
    print("[Extracting AC from transcript via Claude (this can take 20-40s)...]", file=sys.stderr)

    result = extract_ac(transcript)

    print(f"\n[OK] Extracted as: {result.get('issue_type')}  (\"{result.get('title')}\")", file=sys.stderr)
    print(f"  ACs:                  {len(result.get('acceptance_criteria', []))}", file=sys.stderr)
    print(f"  Open questions:       {len(result.get('open_questions', []))}", file=sys.stderr)
    print(f"  Ambiguity flags:      {len(result.get('ambiguity_flags', []))}", file=sys.stderr)
    print(f"  Implicit assumptions: {len(result.get('implicit_assumptions', []))}", file=sys.stderr)
    print(f"  Estimated complexity: {result.get('estimated_complexity')}", file=sys.stderr)
    print("", file=sys.stderr)

    print(json.dumps(result, indent=2, ensure_ascii=False))
