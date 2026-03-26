---
name: deepresearch-feishu
description: Multi-source autonomous deep research with Feishu live progress and delivery. Uses Claude sub-agents + WebSearch/WebFetch for search (no Gemini). Dispatches parallel research agents, manages state on disk, verifies sources, delivers via Feishu cards and files. Trigger on "deep research", "investigate", "research report", "deep dive", "comprehensive analysis", or when user sends files + asks for analysis.
---

# Deep Research (Feishu Edition)

Multi-source autonomous research. You are the dispatcher — you orchestrate, Claude sub-agents search the web, and you synthesize the final report. All progress is streamed to the user via Feishu.

## Architecture

```
You (Dispatcher / Claude)
  ├─ Phase 1: Clarify → Decompose → Plan → Confirm (via Feishu)
  ├─ Phase 2: Search (parallel Claude sub-agents, WebSearch + WebFetch)
  ├─ Phase 3: Extract → batch-add to state file          ← GATE: state file populated
  ├─ Phase 4: Coverage check → fill gaps                  ← GATE: all angles covered
  ├─ Phase 5: Verify sources + cross-check                ← GATE: sources.py collected
  ├─ Phase 6: Map relationships → narrative skeleton
  ├─ Phase 7: Write report FROM state file with citations  ← GATE: read state file first
  └─ Phase 8: Deliver via Feishu + suggest follow-ups
```

### Division of Labor

| Task | Who | Why |
|------|-----|-----|
| Web search + page fetching | **Claude sub-agent** (WebSearch + WebFetch) | Parallel execution, deep page reading |
| File analysis (PDF/Excel/images) | **Claude sub-agent** | Needs Read tool + reasoning |
| Data processing / calculations | **Claude sub-agent** | Needs Python |
| Cross-reference verification | **Claude sub-agent** (WebSearch) | Search from different angle |
| Progress updates | **You (dispatcher)** | Needs Feishu `update_status` |
| Report writing & delivery | **You (dispatcher)** | Needs deep reasoning + Feishu tools |

## Scripts

```bash
SKILL="/Users/ltl/Workspace/bot/feishu-claude-code/workspace/skills/deepresearch-feishu"

# State management
research_state.py init --topic "..." --question "..."    # → session_id
research_state.py add --id $ID --fact "..." --source "..." --confidence high/medium/low
research_state.py append --id $ID                        # pipe markdown via stdin
research_state.py batch-add --id $ID                     # pipe JSON array via stdin
research_state.py queried --id $ID --query "..."
research_state.py status --id $ID
research_state.py update-coverage --id $ID
research_state.py path --id $ID

# Source management (auto tier classification, dedup, formatted output)
sources.py collect --id $ID                          # extract from state file → markdown
sources.py collect --id $ID --format plain           # for Word/PDF
sources.py collect --id $ID --format json            # structured data
sources.py add --id $ID --url "..." --title "..."    # manual add (auto tier)
sources.py show --id $ID --format markdown           # show saved sources
sources.py tier "https://..."                        # classify single URL

# Analysis helpers
compare_data.py duplicates|conflicts|coverage --id $SESSION_ID
source_scorer.py --url "..." --type "..." --date "..."
echo "text" | web_extract.py numbers|money|percentages|dates
```

---

## Workflow: Mandatory Protocol

Every phase produces a **required artifact**. You MUST produce the artifact before moving to the next phase. Skipping a phase = broken report. No exceptions.

**Why this matters:** Past failures happened because phases 3-5 were skipped — report was written from raw search output without state management, source tiering, or verification. The result had no citations and unverified claims.

**Feishu rule:** Call `update_status(request_id, status, text)` at the START of every phase. The user is remote — status is their only visibility.

---

### Phase 1: Clarify Intent

```
update_status(request_id, "Clarifying...", "Understanding your research request")
```

**Before doing ANY research, clarify the scope.** Two paths:

**Path A — User gave vague request:** Ask 2-4 targeted clarifying questions via `reply()`. Wait for response before proceeding. Use a Feishu V2 card with structured options if the questions have discrete choices (e.g. depth level, region focus, output format).

**Path B — User gave detailed requirements (specific angles, data points, constraints):** Skip clarification, go straight to planning. Acknowledge what you understood in `update_status`.

**After scope is clear, decompose and plan:**

1. Break into 3-5 research angles
2. Init session + coverage checklist:

```bash
SESSION_ID=$(python3 $SKILL/research_state.py init --topic "..." --question "...")
echo '## Coverage Checklist
- [ ] Angle 1 (need 2+ sources)
- [ ] Angle 2 (need 2+ sources)
...' | python3 $SKILL/research_state.py append --id $SESSION_ID
```

3. If using Path A, present the plan and ask: **"Should I start, or adjust?"**

**Phase 1 artifact:** `$SESSION_ID` initialized with coverage checklist.

---

### Phase 2: Search (Claude Sub-Agents in Parallel)

```
update_status(request_id, "Searching...", "Launching N parallel search agents")
```

Dispatch Claude sub-agents using the Agent tool. Each sub-agent uses `WebSearch` for discovery and `WebFetch` for deep page reading.

For each angle, launch TWO sub-agents (one per language):

```
Agent(
  subagent_type="general-purpose",
  prompt="""
  Research angle: [description]

  1. Use WebSearch to search for: "[search query]"
  2. For the most promising 3-5 results, use WebFetch to read the full page
  3. Extract key facts with exact numbers and source URLs
  4. Write findings to state file:
     python3 {SKILL}/research_state.py add --id {SESSION_ID} \
       --fact "..." --source "https://..." --confidence high/medium/low
  5. Record your query:
     python3 {SKILL}/research_state.py queried --id {SESSION_ID} --query "..."

  Return a summary of what you found (max 300 words).
  """,
  run_in_background=true
)
```

**Query design:**
- Specific and descriptive, not generic keywords
- Include time ranges, specific data points
- One sub-agent per language: user's language + English

**Phase 2 artifact:** All sub-agents completed, findings written to state file.

---

### Phase 3: Verify State File Population

> **MANDATORY GATE — You MUST run this before Phase 4.**

```
update_status(request_id, "Processing results...", "Checking collected findings")
```

```bash
python3 $SKILL/research_state.py status --id $SESSION_ID
# MUST show: Findings: 20+ before proceeding
```

If sub-agents wrote findings directly (via `research_state.py add`), the state file should be populated. If not, read sub-agent outputs and batch-add:

```bash
echo '[
  {"fact": "specific claim", "source": "https://url", "confidence": "high"},
  ...
]' | python3 $SKILL/research_state.py batch-add --id $SESSION_ID
```

**Phase 3 artifact:** State file has 20+ findings. `status` command confirms count.

---

### Phase 4: Check Coverage

> **MANDATORY GATE — Run these exact commands.**

```
update_status(request_id, "Checking coverage...", "Verifying all angles are covered")
```

```bash
python3 $SKILL/research_state.py update-coverage --id $SESSION_ID
python3 $SKILL/research_state.py status --id $SESSION_ID
python3 $SKILL/compare_data.py coverage --id $SESSION_ID
```

- All angles covered → proceed to Phase 5
- Gaps found → dispatch targeted sub-agents, batch-add, re-check
- Stop after 3 rounds max

**Phase 4 artifact:** Coverage output reviewed, gaps addressed.

---

### Phase 5: Verify Sources & Cross-Check

> **MANDATORY GATE — You MUST run these commands before writing. Every time.**
> Past failure: skipping this step produced a report with unverified claims and no source tiers.

```
update_status(request_id, "Verifying...", "Cross-checking sources and claims")
```

**5a. Collect and tier all sources:**
```bash
python3 $SKILL/sources.py collect --id $SESSION_ID --format markdown
```

**5b. Detect conflicts:**
```bash
python3 $SKILL/compare_data.py conflicts --id $SESSION_ID
python3 $SKILL/compare_data.py duplicates --id $SESSION_ID
```

**5c. Cross-check critical claims:**
For any key stat that is SINGLE_SOURCE or Tier 3 only, dispatch a verification sub-agent:
```
Agent(prompt="Use WebSearch to verify: [claim]. Find 2+ independent sources.", run_in_background=true)
```

**5d. Mark verification status:**
- `VERIFIED` — 2+ independent sources agree
- `CONTRADICTED` — sources disagree (note both figures)
- `SINGLE_SOURCE` — only one source (flag in report)
- `UNVERIFIABLE` — cannot confirm

**Phase 5 artifact:** `sources.py collect` output with tiers. `compare_data.py conflicts` output reviewed.

---

### Phase 6: Map Data Relationships

Before writing, organize data into narrative structure:

1. **Causal chains**: What causes what?
2. **Data dependencies**: Does A explain B?
3. **Contradictions**: What needs reconciliation?
4. **Narrative skeleton**: One-sentence big picture, 3-4 key threads, what's uncertain

**Phase 6 artifact:** Narrative structure clear before writing.

---

### Phase 7: Write Report

> **MANDATORY GATE — You MUST read the state file before writing.**

```
update_status(request_id, "Writing report...", "Synthesizing findings")
```

**Step 1 — Read state file:**
```bash
cat $(python3 $SKILL/research_state.py path --id $SESSION_ID)
```

**Step 2 — Get formatted sources:**
```bash
python3 $SKILL/sources.py collect --id $SESSION_ID --format markdown
```

**Step 3 — Write report using ONLY data from the state file.**

**Report structure:**
1. **Key Findings** — top 3-5 takeaways with inline source links
2. **Detailed Analysis** — per angle, with causal chains. Every data point linked to source.
3. **Data Uncertainty** — CONTRADICTED / SINGLE_SOURCE / UNVERIFIABLE items
4. **Sources** — every source as clickable `[Title](URL)` with Tier

**Citation format (STRICT):**
- Inline: `According to [McKinsey 2025](https://url), the market reached $3.2B`
- Source list: `- [McKinsey: State of AI](https://full-url) — Tier 1`
- NEVER use bare URLs or `[N]` without actual links. Every citation must be clickable.

**Phase 7 artifact:** Complete report with inline citations and source list.

---

### Phase 8: Deliver via Feishu + Follow-ups

**CRITICAL: `reply()` can only be called ONCE per request_id.**

**Leverage all Feishu MCP tools for rich delivery:**

**Charts & Visualization:**
- Use Feishu V2 card JSON with chart elements (bar/line/pie) to visualize key metrics (market size, adoption rates, cost comparisons). See `workspace/skills/feishu-card/SKILL.md` for chart spec.
- Use collapsible panels (`collapsible_panel`) for long sections within a single card.

**Structured Data:**
- If research produces structured comparison data (e.g. regional comparison tables, feature matrices), consider outputting as a Feishu Bitable via `create_bitable(title, fields, records, views, chat_id)` — the user gets an interactive multi-dimensional spreadsheet they can filter and sort.

**Long-form Reports:**
- For reports exceeding card limits, create a Feishu cloud document via `create_doc(title, content, chat_id)` — native Feishu document with headings, lists, and code blocks.
- Or save as markdown/PDF to `/tmp/` and send via `reply_file(chat_id, file_path)`.

**Rich Text Posts:**
- For reports with inline images (charts generated as PNG), use `reply_post(chat_id, content)` to mix text and images in one message.

**Delivery strategy (choose based on report length):**
- **Short (<4KB):** `reply(request_id, markdown_text)` — plain text card
- **Medium (<28KB):** `reply(request_id, v2_card_json)` — V2 card with charts and collapsible panels
- **Long (>28KB):** `reply(request_id, summary_with_key_findings)` + `create_doc(...)` or `reply_file(...)` for full report
- **Data-heavy:** `reply(request_id, summary)` + `create_bitable(...)` for interactive data

Plan the split upfront — don't split because you forgot reply() is one-shot.

Include 2-3 specific follow-up research directions in the final reply.

---

## Rules

1. **Claude sub-agents do the searching** — dispatch via Agent tool with `WebSearch` + `WebFetch`. Run in parallel with `run_in_background`
2. **Specific queries** — descriptive search phrases, not keyword soup
3. **Parallel execution** — launch all angle sub-agents simultaneously
4. **Bilingual** — always search in user's language + English
5. **Clarify before research** — ask 2-4 questions. Wait for confirmation (skip for scheduled tasks)
6. **State file is the ONLY source of truth** — ALL findings go into state file. Report is written FROM state file, not from memory.
7. **Every phase gate is mandatory** — run the exact commands specified. Skipping = broken report.
8. **Sources MUST be collected and tiered** — run `sources.py collect` before writing.
9. **Conflicts MUST be checked** — run `compare_data.py conflicts` before writing.
10. **Every claim has a clickable source link** — no exceptions. Source list at end with tiers.
11. **Live progress via Feishu** — call `update_status()` at the start of every phase.
12. **Feishu delivery** — use MCP tools (`reply`, `reply_file`, `reply_image`, `reply_post`, `create_doc`, `create_bitable`). Plain text output does NOT reach the user.
13. **Rich delivery when it fits** — V2 charts, Bitable, cloud docs are available but not mandatory. Use them when data genuinely benefits from visualization, not as decoration.
14. **Limits:** 3 search rounds max / user stop.

## Triggers

"deep research" / "investigate" / "research report" / "deep dive" / user sends files + asks for report / "comprehensive analysis" / "detailed analysis"
