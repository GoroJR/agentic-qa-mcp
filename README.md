# Agentic QA MCP Server

An MCP (Model Context Protocol) server that turns a single Jira ticket into **~50 production-grade QA test cases** organized by my 9-layer testing framework, exports them to an Excel sheet matching the standard QA delivery template, and posts the result back to the ticket as an attachment — autonomously, from a single natural-language prompt in Claude Code or Claude Desktop.

Part of my **Agentic QA Engineer** journey at [github.com/GoroJR/agentic-qa-journey](https://github.com/GoroJR/agentic-qa-journey).

---

## What it does

A QA engineer types this into Claude Code:

> *"Read Jira ticket KAN-6, generate test cases, export to Excel, attach back to the ticket."*

Claude autonomously chains four tool calls:

1. `fetch_jira_ticket(KAN-6)` — pulls the ticket via Jira REST API and flattens Atlassian Document Format (ADF) back to plain text
2. `generate_qa_test_cases(...)` — sends the description to Claude Sonnet 4.5 with the 9-layer prompt; returns ~50 structured test cases
3. `export_test_cases(...)` — writes a styled `.xlsx` (and optionally `.csv`) matching the standard QA template
4. `attach_to_jira(KAN-6, file)` — uploads the Excel back to the ticket via the attachments API

Total runtime: ~60 seconds. Cost: ~$0.05 per ticket with Sonnet 4.5.

---

## The 9-Layer Test Design Framework

The core IP of this project. The prompt instructs Claude not to just summarize AC, but to convert every requirement into executable coverage across:

| Layer | Validates |
| --- | --- |
| **1. UI** | Screen loads, layout matches mock, labels/buttons visible, default states correct |
| **2. Field** | Per-field validation: required/optional, format, character limit, placeholder, default |
| **3. Conditional Logic** | Dependencies: if X is selected, what becomes enabled / required / hidden / reset |
| **4. Combination** | Single / multiple / all / removed / switched selections, edge combinations |
| **5. Action** | Save, Next, Save and Close, Apply, Cancel, Close X, navigation buttons |
| **6. Persistence** | Values retained on navigate away/back, save/reopen, refresh, session continuity |
| **7. Integration** | Request payloads, API responses, saved data, reload, stale-data prevention, scoping |
| **8. Accessibility / Automation** | data-track-ids, stable selectors, keyboard nav, focus order, validation messages |
| **9. Ambiguity** | Unclear or conflicting AC: doesn't guess — flags the ambiguity for human review |

Output Excel includes a color-coded `Layer` column and an `AC Ref` column linking each test case back to the AC line it validates.

---

## Architecture

```
                  ┌─────────────────────────┐
                  │     Claude Code         │  ← natural-language prompt
                  │     (or Claude Desktop) │
                  └────────────┬────────────┘
                               │  MCP / JSON-RPC over STDIO
                               ▼
                  ┌─────────────────────────┐
                  │   agentic-qa MCP server │
                  │      (this repo)        │
                  │                         │
                  │  4 tools exposed:       │
                  │  • fetch_jira_ticket    │
                  │  • generate_qa_test_cases│
                  │  • export_test_cases    │
                  │  • attach_to_jira       │
                  └────┬──────────┬─────────┘
                       │          │
              ┌────────▼──┐   ┌───▼─────────┐
              │ Jira REST │   │  Claude API │
              │ API       │   │ (Sonnet 4.5) │
              └───────────┘   └─────────────┘
```

---

## Tech stack

- **Python 3.13** + the official `mcp` SDK (FastMCP)
- **Claude Sonnet 4.5** for test case generation (swappable via `.env`)
- **Atlassian Cloud REST API v3** with API-token Basic Auth
- **httpx** for HTTP calls, **openpyxl** for styled Excel output
- **python-dotenv** for secrets

---

## Engineering details worth noting

A few real-world LLM/integration lessons baked into this code:

- **ADF flattening** — Jira v3 returns rich content as nested Atlassian Document Format JSON. The `_flatten_adf()` walker recursively reduces it to plain text while preserving bullets and line breaks. Most "Jira integration" tutorials skip this and break on real tickets.
- **Defensive JSON parsing** — even with explicit "no markdown fences" instructions, LLMs occasionally wrap their JSON output in code fences. The parser strips them before `json.loads()`.
- **STDIO discipline** — MCP servers communicate over stdin/stdout using JSON-RPC. A single stray `print()` to stdout corrupts the entire protocol. Every status message in this codebase routes to `sys.stderr` explicitly.
- **Caching the LLM output** — `generate_qa_test_cases` writes its result to a JSON file under `output/.cache/` so the export and attach tools downstream can reuse it without re-paying for another LLM call.
- **Model swappability** — `CLAUDE_MODEL` env var lets you swap between `claude-sonnet-4-5` (~$0.05/ticket, demo quality) and `claude-haiku-4-5` (~$0.001/ticket, prompt-tuning).

---

## Quick start

```bash
git clone https://github.com/GoroJR/agentic-qa-mcp.git
cd agentic-qa-mcp
pip install -r requirements.txt
```

Create a `.env` file in the project root with these 5 values:

```
ANTHROPIC_API_KEY=sk-ant-...
ATLASSIAN_EMAIL=your-email@example.com
ATLASSIAN_API_TOKEN=ATATT...
ATLASSIAN_SITE=yoursite.atlassian.net
CLAUDE_MODEL=claude-sonnet-4-5
```

Get an Atlassian API token at: <https://id.atlassian.com/manage-profile/security/api-tokens>

### Register the MCP server with Claude Code

```bash
claude mcp add agentic-qa --scope project -- python -m src.server
claude mcp list   # should show: agentic-qa ✓ Connected
```

### Run the agentic loop

In Claude Code (or Claude Desktop with `.mcp.json` configured):

> *Read Jira ticket KAN-6 using the agentic-qa tools, generate 9-layer test cases, export to Excel, and attach back to the ticket.*

Claude will chain all four tools, generate ~50 test cases, and post the Excel back to Jira. ~60 seconds end to end.

---

## Repository layout

```
agentic-qa-mcp/
├── src/
│   ├── server.py          # FastMCP entry point - defines the 4 tools
│   ├── jira_client.py     # Jira REST API wrapper + ADF flattener
│   ├── test_generator.py  # Claude API call with the 9-layer prompt
│   └── exporter.py        # Excel / CSV writer matching the QA template
├── .mcp.json              # Claude Code MCP config (project scope)
├── requirements.txt
└── README.md
```

---

## Roadmap

- [x] **v1** — CLI tool: AC text → test case JSON ([agentic-qa-journey](https://github.com/GoroJR/agentic-qa-journey))
- [x] **v2** — MCP server: Jira ticket → 9-layer test cases → Excel → attach back to ticket *(you are here)*
- [ ] **v3** — Zoom transcript → extracted Acceptance Criteria → Jira ticket → test cases (full refinement-to-QA pipeline)
- [ ] **v4** — Vision: read attached Figma screenshots and UI mockups in the ticket
- [ ] **v5** — Jira webhook integration: auto-generate on ticket create/update

---

## Why this matters

Test case design is the highest-leverage task in QA — and one of the most automatable with LLMs when done right. This project demonstrates a working agentic workflow that integrates a real ticket system, a real test design framework, and a real delivery format — not a toy demo. It's the working prototype of the AI Acceptance Criteria + Test Case Generation pilot I am proposing for enterprise QA delivery.
> ⚠️ Built on personal time, with personal accounts, and with synthetic hotel-domain acceptance criteria. Any enterprise use should go through proper security review and client approval.
---

## Author

**Erick Rodrigo Jimenez** — QA Automation Engineer pivoting into Agentic QA
[LinkedIn](https://linkedin.com/in/erickrodrigoja) · [GitHub](https://github.com/GoroJR)