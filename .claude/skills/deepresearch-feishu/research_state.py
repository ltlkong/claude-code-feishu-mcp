#!/usr/bin/env python3
"""Research State Manager — Markdown-based persistent state for research sessions.

All research data is stored as a single markdown file that's easy to read,
write, and append to. Sub-agents simply append findings to the file.

Usage:
    # Initialize a new research session
    python research_state.py init --topic "electric vehicles" --question "market outlook"

    # Add a finding (appends to the markdown file)
    python research_state.py add --id <session_id> \
        --confidence high --fact "BYD sold 3M cars in 2025" --source "https://reuters.com/..."

    # Record a searched query
    python research_state.py queried --id <session_id> --query "BYD sales 2025"

    # Add a contradiction
    python research_state.py conflict --id <session_id> \
        --a "Market size is 500B" --source-a "mckinsey.com" \
        --b "Market size is 380B" --source-b "idc.com"

    # Get status summary (stdout)
    python research_state.py status --id <session_id>

    # Get the full markdown path (for Claude to read)
    python research_state.py path --id <session_id>
"""

import argparse
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

STATE_DIR = Path("/tmp/feishu-channel/research")


def _md_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.md"


def init_session(topic: str, question: str, sub_questions: list[str] = None) -> str:
    """Create a new research session markdown file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    session_id = f"research_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    path = _md_path(session_id)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    subs = ""
    if sub_questions:
        subs = "\n".join(f"- {q}" for q in sub_questions)

    content = f"""# Research: {topic}

- **Question**: {question}
- **Session**: {session_id}
- **Started**: {now}
- **Status**: researching

## Research Plan
{subs if subs else "(to be filled)"}

## Findings

## Contradictions

## Searched Queries

"""
    path.write_text(content)
    return session_id


def add_finding(session_id: str, fact: str, source: str,
                confidence: str = "medium", topic: str = "") -> None:
    """Append a finding to the markdown file."""
    path = _md_path(session_id)
    if not path.exists():
        print(f"Session {session_id} not found", file=sys.stderr)
        sys.exit(1)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conf_label = {"high": "HIGH", "medium": "MED", "low": "LOW"}.get(confidence, confidence)
    topic_line = f"\n- Topic: {topic}" if topic else ""

    entry = f"""
### [{conf_label}] {fact}
- Source: {source}{topic_line}
- Added: {now}
"""

    content = path.read_text()
    # Insert before "## Contradictions"
    content = content.replace("## Contradictions", entry + "\n## Contradictions", 1)
    path.write_text(content)


def add_query(session_id: str, query: str) -> None:
    """Record a searched query."""
    path = _md_path(session_id)
    if not path.exists():
        print(f"Session {session_id} not found", file=sys.stderr)
        sys.exit(1)

    content = path.read_text()
    content += f"- `{query}`\n"
    path.write_text(content)


def add_contradiction(session_id: str, claim_a: str, source_a: str,
                      claim_b: str, source_b: str) -> None:
    """Record a contradiction between sources."""
    path = _md_path(session_id)
    if not path.exists():
        print(f"Session {session_id} not found", file=sys.stderr)
        sys.exit(1)

    entry = f"""
- **{claim_a}** (source: {source_a}) vs **{claim_b}** (source: {source_b})
"""

    content = path.read_text()
    content = content.replace("## Searched Queries", entry + "\n## Searched Queries", 1)
    path.write_text(content)


def batch_add(session_id: str, findings_json: str) -> None:
    """Add multiple findings from JSON stdin. Avoids shell escaping issues.

    Input format: [{"fact": "...", "source": "...", "confidence": "high", "topic": ""}]
    """
    import json as _json
    findings = _json.loads(findings_json)
    for f in findings:
        add_finding(
            session_id,
            f["fact"],
            f["source"],
            f.get("confidence", "medium"),
            f.get("topic", ""),
        )


def append_raw(session_id: str, text: str) -> None:
    """Append raw markdown text to the findings section. Simplest way to add content."""
    path = _md_path(session_id)
    if not path.exists():
        print(f"Session {session_id} not found", file=sys.stderr)
        sys.exit(1)

    content = path.read_text()
    content = content.replace("## Contradictions", text + "\n## Contradictions", 1)
    path.write_text(content)


def get_status(session_id: str) -> str:
    """Return a brief status summary."""
    path = _md_path(session_id)
    if not path.exists():
        return f"Session {session_id} not found"

    content = path.read_text()

    # Count findings by confidence
    high = len(re.findall(r"### \[HIGH\]", content))
    medium = len(re.findall(r"### \[MED\]", content))
    low = len(re.findall(r"### \[LOW\]", content))
    total = high + medium + low

    # Count contradictions
    contradictions = content.count("** vs **")

    # Count searched queries
    queries = len(re.findall(r"^- `", content, re.MULTILINE))

    return f"""Research Status: {session_id}
- Findings: {total} (high: {high}, medium: {medium}, low: {low})
- Contradictions: {contradictions}
- Queries searched: {queries}
- File: {path}"""


def update_coverage(session_id: str, min_sources: int = 2) -> str:
    """Auto-check coverage checklist items based on findings with matching topics.

    For each checklist item, counts findings whose topic (case-insensitive)
    overlaps with the checklist text. Checks the item if count >= min_sources.
    """
    path = _md_path(session_id)
    if not path.exists():
        return f"Session {session_id} not found"

    content = path.read_text()

    # Extract all finding topics
    topic_counts: dict[str, int] = {}
    for m in re.finditer(r"- Topic: (.+)", content):
        t = m.group(1).strip().lower()
        topic_counts[t] = topic_counts.get(t, 0) + 1

    # Find checklist items and try to match
    results = []
    def replace_checklist(m):
        check = m.group(1)
        item_text = m.group(2)
        item_lower = item_text.lower()

        # Count matching findings: fuzzy word overlap (handles plurals/prefixes)
        matched = 0
        for topic, count in topic_counts.items():
            topic_words = set(topic.split())
            item_words = set(re.findall(r'[a-z]+', item_lower))
            # Match if any topic word shares a 4+ char prefix with any item word
            hit = False
            for tw in topic_words:
                for iw in item_words:
                    prefix_len = min(len(tw), len(iw), 4)
                    if tw[:prefix_len] == iw[:prefix_len] and prefix_len >= 4:
                        hit = True
                        break
                if not hit:
                    # Also check substring match
                    if tw in item_lower or any(iw in topic for iw in item_words if len(iw) >= 4):
                        hit = True
                if hit:
                    break
            if hit:
                matched += count

        if matched >= min_sources:
            results.append(f"[x] {item_text} ({matched} sources)")
            return f"- [x] {item_text} ({matched} sources)"
        else:
            results.append(f"[ ] {item_text} ({matched} sources)")
            return f"- [ ] {item_text} ({matched} sources)"

    content = re.sub(r"- \[(.)\] (.+?)(?:\s*\(\d+ sources?\))?$",
                     replace_checklist, content, flags=re.MULTILINE)
    path.write_text(content)

    checked = sum(1 for r in results if r.startswith("[x]"))
    return f"Coverage: {checked}/{len(results)}\n" + "\n".join(results)


def get_path(session_id: str) -> str:
    """Return the markdown file path."""
    return str(_md_path(session_id))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage research session state (markdown)")
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init")
    p_init.add_argument("--topic", required=True)
    p_init.add_argument("--question", required=True)

    # add
    p_add = sub.add_parser("add")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--fact", required=True)
    p_add.add_argument("--source", required=True)
    p_add.add_argument("--confidence", default="medium", choices=["high", "medium", "low"])
    p_add.add_argument("--topic", default="")

    # queried
    p_query = sub.add_parser("queried")
    p_query.add_argument("--id", required=True)
    p_query.add_argument("--query", required=True)

    # conflict
    p_conflict = sub.add_parser("conflict")
    p_conflict.add_argument("--id", required=True)
    p_conflict.add_argument("--a", required=True)
    p_conflict.add_argument("--source-a", required=True)
    p_conflict.add_argument("--b", required=True)
    p_conflict.add_argument("--source-b", required=True)

    # batch-add (reads JSON from stdin)
    p_batch = sub.add_parser("batch-add")
    p_batch.add_argument("--id", required=True)

    # append (reads raw markdown from stdin)
    p_append = sub.add_parser("append")
    p_append.add_argument("--id", required=True)

    # status
    p_status = sub.add_parser("status")
    p_status.add_argument("--id", required=True)

    # update-coverage
    p_cov = sub.add_parser("update-coverage")
    p_cov.add_argument("--id", required=True)
    p_cov.add_argument("--min", type=int, default=2, help="Minimum sources to check off")

    # path
    p_path = sub.add_parser("path")
    p_path.add_argument("--id", required=True)

    args = parser.parse_args()

    if args.command == "init":
        sid = init_session(args.topic, args.question)
        print(sid)
    elif args.command == "add":
        add_finding(args.id, args.fact, args.source, args.confidence, args.topic)
        print("OK")
    elif args.command == "batch-add":
        batch_add(args.id, sys.stdin.read())
        print("OK")
    elif args.command == "append":
        append_raw(args.id, sys.stdin.read())
        print("OK")
    elif args.command == "queried":
        add_query(args.id, args.query)
        print("OK")
    elif args.command == "conflict":
        add_contradiction(args.id, args.a, args.source_a, args.b, args.source_b)
        print("OK")
    elif args.command == "status":
        print(get_status(args.id))
    elif args.command == "update-coverage":
        print(update_coverage(args.id, args.min))
    elif args.command == "path":
        print(get_path(args.id))
    else:
        parser.print_help()
        sys.exit(1)
