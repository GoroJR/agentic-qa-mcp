# Agentic QA MCP Server

An MCP (Model Context Protocol) server that compresses the requirements-to-tested-feature workflow from ~6 hours to ~50 minutes. It reads Jira tickets (including attached UI mockups), creates new tickets from meeting transcripts, generates production-grade test coverage across a 9-layer framework, and ships the result back to Jira as a styled Excel attachment — autonomously, from a single natural-language prompt in Claude Code or Claude Desktop.

Part of my **Agentic QA Engineer** journey at [github.com/GoroJR/agentic-qa-journey](https://github.com/GoroJR/agentic-qa-journey).

> ⚠️ Built on personal time, with personal accounts, and with synthetic hotel-domain acceptance criteria. Any enterprise use should go through proper security review and client approval.

---

## What it does

A QA engineer types this into Claude Code:

> *"Here's the transcript from the refinement call. Extract the requirements, create the Jira ticket, read any attached mockups, generate test cases, and attach them back."*

Claude autonomously chains 8 tools:

1. `extract_ac_from_transcript` — parses the meeting transcript into a structured story (title, user story, ACs with confidence flags, open questions, implicit assumptions, ambiguity flags)
2. `create_jira_ticket_from_extraction` — creates the live Jira ticket with full description + provenance labels (`type:story`, `source:transcript`, `generated:agentic-qa`)
3. `fetch_jira_ticket_with_images` — pulls the new ticket and downloads any attached mockup images locally (PNG/JPG/GIF/WEBP), auto-resizing large ones to keep vision tokens bounded
4. `generate_qa_test_cases` — calls Claude Sonnet 4.5 with the AC text **and the mockup images**, producing ~50–80 test cases across the 9-layer framework, with UI/Field tests grounded in what the screens actually show
5. `export_test_cases` — writes a styled `.xlsx` (and optionally `.csv`) matching the standard QA delivery template
6. `attach_to_jira` — uploads the Excel back to the ticket via the attachments API

Total runtime: ~3 minutes. Cost: ~$0.05–0.10 per ticket with Sonnet 4.5 + vision.

---

## The 9-Layer Test Design Framework

The core IP of this project. The prompt instructs Claude not to summarize AC, but to convert every requirement into executable coverage across:

| Layer | Validates |
| --- | --- |
| **1. UI** | Screen loads, layout matches mock, labels/buttons/fields visible, default states correct |
| **2. Field** | Per-field validation: required/optional, format, character limit, placeholder, default |
| **3. Conditional Logic** | Dependencies: if X is selected, what becomes enabled / required / hidden / reset |
| **4. Combination** | Single / multiple / all / removed / switched selections, edge combinations |
| **5. Action** | Save, Next, Save and Close, Apply, Cancel, Close X, navigation buttons |
| **6. Persistence** | Values retained on navigate away/back, save/reopen, refresh, session continuity |
| **7. Integration** | Request payloads, API responses, saved data, reload, stale-data prevention, scoping |
| **8. Accessibility / Automation** | data-track-ids, stable selectors, keyboard nav, focus order, validation messages |
| **9. Ambiguity** | Unclear or conflicting AC: doesn't guess — flags the ambiguity for human review |

Output Excel includes a color-coded `Layer` column and an `AC Ref` column linking each test case back to the AC line (or `Mockup-derived` if grounded in a visible UI element not mentioned in the AC).

---

## Architecture

```
                  ┌─────────────────────────┐
                  │     Claude Code         │  ← natural-language prompt
                  │     (or Claude Desktop) │
                  └────────────┬────────────┘
                               │  MCP / JSON-RPC over STDIO
                               ▼
                  ┌──────────────────────────────────┐
                  │   agentic-qa MCP server          │
                  │      (this repo)                 │
                  │                                  │
                  │  8 tools exposed:                │
                  │  • fetch_jira_ticket             │
                  │  • fetch_jira_ticket_with_images │
                  │  • generate_qa_test_cases        │
                  │  • export_test_cases             │
                  │  • attach_to_jira                │
                  │  • extract_ac_from_transcript    │
                  │  • refine_extracted_ac           │
                  │  • create_jira_ticket_from_     │
                  │    extraction                    │
                  └────┬──────────┬──────────────────┘
                       │          │
                       ▼          ▼
                  ┌──────────┐   ┌────────────────────┐
                  │   Jira   │   │  Claude API        │
                  │   API    │   │  (Sonnet 4.5 +     │
                  │          │   │   vision)          │
                  └──────────┘   └────────────────────┘
```

---

## Tech stack

- **Python 3.13** + the official `mcp` SDK (FastMCP)
- **Claude Sonnet 4.5** for AC extraction, test generation, and vision (swappable via `.env`)
- **Atlassian Cloud REST API v3** with API-token Basic Auth
- `httpx` for HTTP, `openpyxl` for styled Excel output, `Pillow` for image resize, `python-dotenv` for secrets

---

## Quick start

```bash
git clone https://github.com/GoroJR/agentic-qa-mcp.git
cd agentic-qa-mcp
pip install -r requirements.txt
```

Create a `.env` in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
ATLASSIAN_EMAIL=your-email@example.com
ATLASSIAN_API_TOKEN=ATATT...
ATLASSIAN_SITE=yoursite.atlassian.net
CLAUDE_MODEL=claude-sonnet-4-5
DEFAULT_EXPORT_DIR=./output
```

Get an Atlassian API token at: <https://id.atlassian.com/manage-profile/security/api-tokens>

Register the MCP server with Claude Code (project scope):

```bash
claude mcp add agentic-qa --scope project -- python -m src.server
claude mcp list   # should show: agentic-qa ✓ Connected
```

Then in Claude Code:

> *Read Jira ticket KAN-7 with images using the agentic-qa tools, generate 9-layer test cases, export to Excel, and attach back to the ticket.*

---

## Engineering details worth noting

A few real-world LLM/integration lessons baked into this code:

- **ADF flattening** — Jira v3 returns rich content as nested Atlassian Document Format JSON. The `_flatten_adf()` walker recursively reduces it to plain text while preserving bullets and line breaks. Most "Jira integration" tutorials skip this and break on real tickets.
- **STDIO discipline** — MCP servers communicate over stdin/stdout using JSON-RPC. A single stray `print()` to stdout corrupts the entire protocol. Every status message in this codebase routes to `sys.stderr` explicitly.
- **Defensive JSON parsing** — even with explicit "no markdown fences" instructions, LLMs occasionally wrap their JSON output in code fences. The parser strips them before `json.loads()`.
- **Caching the LLM output** — `generate_qa_test_cases` writes its result to a JSON file under `output/.cache/` so the export and attach tools downstream can reuse it without re-paying for another LLM call.
- **Image resizing for vision token control** — attached mockups larger than 1568px on the long side are downsized in-place via Pillow before being base64-encoded as vision blocks. Keeps cost predictable on screenshot-heavy tickets.
- **Model swappability** — `CLAUDE_MODEL` env var lets you swap between `claude-sonnet-4-5` (~$0.05/ticket, demo quality) and `claude-haiku-4-5` (~$0.001/ticket, prompt-tuning).

---

## Compatible clients

This MCP server speaks the standard Model Context Protocol — it works with any compliant client:

- ✅ **Claude Code** (tested — production demo, VS Code extension + standalone CLI)
- ✅ **Claude Desktop** (via `claude_desktop_config.json` — same JSON shape as `.mcp.json`)
- ✅ **Cline / Roo Code** (via their MCP settings file)
- ✅ **Cursor** (via `~/.cursor/mcp.json`)

The server is client-agnostic by design. Same code, same tools, different chat surface.

---

## Limitations and quality roadmap

This is a working agent, not a production-ready system. Honest limitations:

**1. No formal evaluation suite yet.**
The tool currently has no automated way to measure hallucination rate, coverage recall, or output stability across runs. I review outputs manually. A proper eval harness is the next major milestone.

**2. Outputs are non-deterministic.**
Same input on Monday may produce a different test set on Friday. The structural template stays consistent (9 layers, schema), but individual test cases vary. This is normal LLM behavior — fixing it requires golden-dataset regression tests, not prompt engineering.

**3. Vision grounding is opportunistic, not enforced.**
The agent tries to base UI/Field tests on visible mockup elements, but there's no automated check that every Mockup-derived test case actually references an element present in the image. A vision-eval scorer is needed.

**4. Single-language dependency.**
The system prompts and example outputs are English-first. Spanish/multilingual transcripts mostly work but haven't been measured. Edge cases like mixed-language refinement calls (a common reality in LATAM consulting) are unvalidated.

**5. No audit logging.**
Tool invocations aren't logged. For any real enterprise use, every call needs a per-user audit trail with inputs, outputs, and timestamps.

**6. No per-user credentials.**
A single `.env` holds all secrets. Multi-user use requires OS keychain integration or a proper secrets backend.

**7. Single Jira project assumption (`KAN`).**
Hardcoded project key in some tools. Easy fix, not yet done.

### Quality roadmap (v5+)

- **v5 — Eval harness.** Build a golden dataset of 20 hand-validated tickets. Run all of them through the pipeline weekly. Track coverage recall, hallucination rate, AC-attribution accuracy, and ambiguity flag precision. This is where I cross from "Agentic QA" into "LLM Quality Engineer" practice — the discipline of measuring AI output quality with structured evals.
- **v6 — LLM-as-judge scoring.** Add a second Claude call that grades each generated test case on rubrics (specificity, AC grounding, no-hallucination, executability). Surface low-scoring cases for human review before export.
- **v7 — Webhook integration.** Jira webhook → FastAPI endpoint → agent runs automatically on ticket create. Slack notification on completion.
- **v8 — qTest / TestRail uploader.** Skip Excel as the delivery format and write directly into test management tools via their APIs.
- **v9 — Multi-user, audit-logged version** suitable for an internal pilot. Per-user credentials, structured audit logs, cost caps.

---

## Why this matters

Test case design is the highest-leverage task in QA — and one of the most automatable with LLMs when done right. This project demonstrates a working agentic workflow that integrates a real ticket system, a real test design framework, real vision-grounded reasoning, and a real delivery format. Not a toy demo.

The compression isn't magic. The flow that used to take a refinement call + 4 hours of test design + review + upload now takes a refinement call + 3 minutes of agent runtime + 45 minutes of human review. The agent doesn't replace the tester — it replaces the *mechanical 95%* of the work, leaving the judgment work to humans.

---

## Roadmap (shipped + planned)

- [x] **v1** — CLI tool: AC text → test case JSON ([agentic-qa-journey](https://github.com/GoroJR/agentic-qa-journey))
- [x] **v2** — MCP server: Jira ticket → 9-layer test cases → Excel → attach back to ticket
- [x] **v3** — Zoom transcript → extracted Acceptance Criteria → Jira ticket created → tests generated → attached
- [x] **v4** — Vision: read attached UI mockups from Jira tickets, ground UI/Field test cases in what's visible
- [ ] **v5** — Eval harness: golden dataset + automated regression scoring (the discipline shift toward LLM Quality Engineering)
- [ ] **v6** — LLM-as-judge scoring of generated tests
- [ ] **v7** — Jira webhook integration for automatic pipeline trigger
- [ ] **v8** — Direct qTest / TestRail uploader
- [ ] **v9** — Multi-user, audit-logged enterprise-ready version

---

## Author

**Erick Rodrigo Jimenez** — QA Automation Engineer pivoting into Agentic QA / LLM Quality Engineering
[LinkedIn](https://linkedin.com/in/erickrodrigoja) · [GitHub](https://github.com/GoroJR)