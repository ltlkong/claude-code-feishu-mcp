---
name: deepresearch-feishu
description: Multi-source autonomous deep research with Feishu live progress and delivery. Uses Claude sub-agents + WebSearch/WebFetch for search (no Gemini). Dispatches parallel research agents, manages state on disk, verifies sources, delivers via Feishu cards and files. Trigger on "deep research", "investigate", "research report", "deep dive", "comprehensive analysis", or when user sends files + asks for analysis.
---

# Deep Research (Feishu Edition)

Multi-source autonomous research. You are the dispatcher — you orchestrate, Claude sub-agents search the web, and you synthesize the final report. All progress is streamed to the user via Feishu.

## Architecture

```
You (Dispatcher / Claude)
  ├─ Assess → Decompose → Plan → Confirm (via Feishu)
  ├─ Search: parallel Claude sub-agents (WebSearch + WebFetch)
  ├─ File analysis: Claude sub-agents (Read tool + Python)
  ├─ Process results → state file on disk
  ├─ Check coverage → fill gaps
  ├─ Verify: additional WebSearch cross-checks
  ├─ Write report from state file
  └─ Deliver via Feishu (card / markdown / file)
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

## Workflow

### Step 0: Feishu Setup

Every step below must call `update_status(request_id, status, text)` to keep the user informed. Status examples:
- `"Clarifying..."` / `"Searching..."` / `"Analyzing files..."` / `"Verifying..."` / `"Writing report..."`

### Step 1: Clarify Intent

**Before doing ANY research, ask clarifying questions to narrow the scope.**

**1a. Restate and probe:**
- Restate the user's request in your own words
- Ask 2-4 targeted clarifying questions:
  - What specific aspect matters most?
  - Intended use? (personal decision, business report, pitch deck...)
  - Geographic/industry/time constraints?
  - Depth level? (quick overview vs. exhaustive analysis)
  - Specific data points or comparisons needed?
- If user provided files, ask what role they play

Use `update_status(request_id, "Clarifying...", "<your questions>")` then `reply(request_id, "<your questions>")`.

**1b. Wait for user response.** Do NOT proceed until confirmed.

**1c. After confirmation, decompose and plan:**

1. Assess sources: user files? web? both?
2. Break into 3-5 research angles
3. Init session + coverage checklist:

```bash
SESSION_ID=$(python3 $SKILL/research_state.py init --topic "..." --question "...")
echo '## Coverage Checklist
- [ ] Angle 1 (need 2+ sources)
- [ ] Angle 2 (need 2+ sources)
...' | python3 $SKILL/research_state.py append --id $SESSION_ID
```

4. Present the research plan via Feishu and ask: **"This is my plan. Should I start, or do you want to adjust?"** (skip for scheduled tasks)

### Step 2: Search (Claude Sub-Agents in Parallel)

**Dispatch Claude sub-agents using the Agent tool.** Each sub-agent uses `WebSearch` for discovery and `WebFetch` for deep page reading.

For each research angle, launch TWO sub-agents in parallel (one per language):

```
Agent(
  subagent_type="general-purpose",
  prompt="""
  Research angle: [description]

  1. Use WebSearch to search for: "[search query]"
  2. For the most promising 3-5 results, use WebFetch to read the full page
  3. Extract key facts, data points, quotes with exact numbers
  4. Write findings to the state file:
     python3 {SKILL}/research_state.py add --id {SESSION_ID} \
       --fact "..." --source "https://..." --confidence high/medium/low --topic "angle name"
  5. Record your query:
     python3 {SKILL}/research_state.py queried --id {SESSION_ID} --query "..."

  Return a summary of what you found (max 300 words).
  """,
  run_in_background=true
)
```

**Query design:**
- Each query should be specific and descriptive, not generic keywords
- Include time ranges, specific data points you're looking for
- One sub-agent per language: user's language + English
- Example (user's language): `"China real estate policy 2025-2026 guaranteed delivery urban village renovation mortgage rates developer debt restructuring sales data"` (in the user's language)
- Example (English): `"China real estate policy changes 2025-2026 guaranteed delivery urban village renovation mortgage rates"`

**After all sub-agents complete**, update status:

```
update_status(request_id, "Processing results...", "N sub-agents completed, processing findings")
```

Then check what was collected:
```bash
python3 $SKILL/research_state.py status --id $SESSION_ID
```

### Step 3: File Analysis (Claude Sub-Agents)

**Only if user provided files.** Dispatch sub-agents for:
- Reading PDFs (use `pages` param for large files)
- Parsing Excel/Word documents
- Analyzing images
- Cross-referencing between documents

Sub-agent prompt must include:
- Specific files to analyze and what to extract
- Write findings via `research_state.py add` with source=filename
- Flag discrepancies between documents
- Return max 500 word summary with key numbers

### Step 4: Check Coverage

```bash
python3 $SKILL/research_state.py update-coverage --id $SESSION_ID
python3 $SKILL/research_state.py status --id $SESSION_ID
python3 $SKILL/compare_data.py coverage --id $SESSION_ID
```

Update Feishu status with coverage progress.

For uncovered angles, dispatch additional sub-agents with more targeted queries.

Stop when: all covered OR 3 rounds OR diminishing returns.

### Step 5: Verify Sources & Cross-Check Data

**Never skip this step.**

```
update_status(request_id, "Verifying...", "Cross-checking key claims across sources")
```

**5a. Source Credibility Check:**
For each key data point, assess the source:
- **Tier 1** (high trust): Government data, major research firms, public filings, top-tier financial media
- **Tier 2** (medium trust): Industry media, second-hand citations, well-known platforms
- **Tier 3** (low trust): Unsourced aggregators, marketing content, no traceable origin

Flag any key claim that only comes from Tier 3. For critical single-source numbers, dispatch a verification sub-agent:

```
Agent(
  subagent_type="general-purpose",
  prompt="Use WebSearch to verify: [specific claim with numbers]. Find 2+ independent sources that confirm or contradict this.",
  run_in_background=true
)
```

**5b. Cross-Verification:**
```bash
python3 $SKILL/compare_data.py conflicts --id $SESSION_ID
python3 $SKILL/compare_data.py duplicates --id $SESSION_ID
```

Mark claims: `VERIFIED` (2+ sources) / `CONTRADICTED` (sources disagree) / `SINGLE_SOURCE` / `UNVERIFIABLE`

### Step 6: Map Data Relationships

**Before writing, organize the data into a coherent narrative.**

**6a. Causal Chains:** Policy → Market impact, Tech adoption → Industry shift, etc.

**6b. Data Dependencies:** Does data A explain B? Any contradictions to reconcile?

**6c. Narrative Skeleton:**
1. Big picture story (one sentence)
2. 3-4 key threads
3. How they connect
4. What is uncertain and why

### Step 7: Write & Deliver Report

```
update_status(request_id, "Writing report...", "Synthesizing findings into report")
```

Read state file as source of truth:
```bash
cat $(python3 $SKILL/research_state.py path --id $SESSION_ID)
```

**Report structure:**
1. **Key Findings** — top 3-5 takeaways, each claim with markdown link to source
2. **Charts** — where data supports it, include Feishu V2 charts (bar/line/pie) to visualize key metrics (e.g. market size growth, adoption rates, cost savings). See feishu-card skill for chart spec.
3. **Detailed Analysis** — per angle, with causal chains. Not data dumps. Every data point must link to its source.
4. **Data Uncertainty** — CONTRADICTED / SINGLE_SOURCE / UNVERIFIABLE items
5. **Sources** — EVERY source as a clickable markdown link: `[Title](URL)` with Tier noted. User must be able to click and verify each one. No bare URLs or references without links.

**Source citation format (STRICT):**
- Inline: `According to [McKinsey 2025 Report](https://url), banking AI cost reduction can reach 20%` (adapt to user's language, but always include the markdown link)
- Source list: `- [McKinsey: The State of AI](https://full-url) — Tier 1`
- NEVER use `[N]` numbered references without the actual URL. Every citation must be a clickable link.

Generate sources:
```bash
python3 $SKILL/sources.py collect --id $SESSION_ID --format markdown
```

**Delivery via Feishu:**

- **Default:** Split into 2 parts:
  1. **Executive summary card** — Feishu V2 card with key findings + 1-2 charts (market size, adoption rate, etc.)
  2. **Full report** — `reply(request_id, report_text)` with markdown. All sources as clickable links.
- **For data-heavy reports:** Add charts inline using the `chart` tag in Feishu V2 card JSON.
- **PDF/DOCX/XLSX:** Generate with the appropriate skill, save to `/tmp/`, then `reply_file(chat_id, file_path)`.

### Step 8: Follow-ups

Suggest 2-3 specific follow-up research directions based on gaps found. Include them in the final reply.

## Rules

1. **Claude sub-agents do the searching** — dispatch via Agent tool with `WebSearch` + `WebFetch`. Run searches in parallel using `run_in_background`
2. **Specific queries** — descriptive search phrases, not keyword soup. Include what data you're looking for
3. **Parallel execution** — launch all angle sub-agents simultaneously. Don't wait for one to finish before starting the next
4. **Bilingual** — always search in both the user's language and English
5. **Clarify before research** — ask 2-4 questions to narrow scope. Wait for confirmation (skip for scheduled tasks)
6. **Coverage** — each angle needs 2+ sources before moving on
7. **Verify MANDATORY** — NEVER skip source verification. Assess source tier for critical claims
8. **Map relationships before writing** — identify causal chains, contradictions, narrative structure before prose
9. **Clickable citations** — every claim must have an inline `[source title](URL)` markdown link. Source list at end must also be clickable links with Tier. User must be able to verify every data point by clicking.
10. **State file = truth** — all findings on disk via research_state.py, not in memory
11. **Live progress** — call `update_status()` at every step so the user sees what's happening
12. **Feishu delivery** — always use `reply()` or `reply_file()` to deliver. Plain text output does NOT reach the user
13. **Limits:** 3 search rounds max / user stop

## Triggers

"deep research" / "investigate" / "research report" / "deep dive" / user sends files + asks for report / "comprehensive analysis" / "detailed analysis"
