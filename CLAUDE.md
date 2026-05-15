# agentic-qa-mcp — workspace guide

MCP server that compresses requirements-to-tested-feature from ~6 h to ~50 min.
Reads Jira tickets (including attached UI mockups), creates tickets from meeting
transcripts, generates ~50–80 test cases across a 9-layer framework, and ships the
result back to Jira as a styled Excel attachment.

Personal portfolio project. Owner: Erick (GoroJR on GitHub). Sister repo:
`github.com/GoroJR/agentic-qa-journey`. Built on personal time with personal accounts
and synthetic hotel-domain ACs — **not enterprise-cleared**; any enterprise use needs
security review and client approval first.

## Read first

- `README.md` — full architecture, the 9-layer framework, runtime + cost numbers,
  setup steps.

## Stack

- Python (see `requirements.txt`).
- FastMCP (official MCP SDK) over JSON-RPC / STDIO.
- Claude Sonnet 4.5 + vision for AC extraction and vision-grounded test generation.
- Atlassian Cloud REST API v3 for Jira.
- Output: styled `.xlsx` (+ optional `.csv`) matching standard QA delivery template.
- Cost: ~$0.05–0.10 per ticket.

## The 8 tools (exposed via MCP)

1. `extract_ac_from_transcript` — meeting transcript → structured story (title, user
   story, ACs with confidence flags, open questions, implicit assumptions, ambiguity flags).
2. `refine_extracted_ac` — iterate on the structured story before ticket creation.
3. `create_jira_ticket_from_extraction` — creates the live Jira ticket with labels
   `type:story`, `source:transcript`, `generated:agentic-qa`.
4. `fetch_jira_ticket` — read a ticket.
5. `fetch_jira_ticket_with_images` — read a ticket and download attached mockup
   images (PNG/JPG/GIF/WEBP), auto-resizing large ones to bound vision tokens.
6. `generate_qa_test_cases` — Sonnet 4.5 + vision over AC text + mockups → test cases
   across the 9-layer framework, each tagged with `Layer` and `AC Ref` (or
   `Mockup-derived` if grounded in a UI element not in the AC).
7. `export_test_cases` — write the styled `.xlsx` (and optional `.csv`).
8. `attach_to_jira` — upload the Excel back to the ticket via attachments API.

## The 9-layer framework (the core IP)

UI · Field · Conditional Logic · Combination · Action · Persistence · Integration ·
Accessibility/Automation · Ambiguity. The prompt's job is **not** to summarize AC —
it's to convert every requirement into executable coverage across these layers.
Layer 9 (Ambiguity) is special: don't guess; flag for human review.

## Source layout

```
src/
├── server.py                 # FastMCP entrypoint, tool registrations
├── jira_client.py            # Atlassian REST v3 wrapper
├── transcript_extractor.py   # extract_ac_from_transcript / refine_extracted_ac
├── test_generator.py         # generate_qa_test_cases (vision-grounded)
└── exporter.py               # styled Excel + CSV output
output/                       # generated artifacts (gitignored)
sample_transcript.txt         # demo input
extraction.log
```

MCP wiring: `.mcp.json`.

## Credentials

- `.env` (gitignored) — Jira (Atlassian) creds + Anthropic API key.
- Do not commit `.env`, `output/`, `extraction.log`.

## When picking up a session

1. Read `README.md` if architecture/9-layer questions come up.
2. If editing prompts: the 9-layer framework lives in `test_generator.py` — changes
   there affect coverage quality; review carefully.
3. Test against `sample_transcript.txt` before touching live Jira.
4. Remember: this is a **personal** portfolio project, not an enterprise tool.

## Sibling projects (Erick's other workspaces)

- `F:\MushiTCG\` — Pokémon TCG Shopify store (MX).
- `F:\remax-marketing-mcp\` — RE/MAX Patrimonial multi-agent MCP (MX).
