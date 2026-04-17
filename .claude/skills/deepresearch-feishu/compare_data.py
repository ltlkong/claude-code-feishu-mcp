#!/usr/bin/env python3
"""Cross-reference and compare data from multiple sources.

Useful for verification: compare numbers from different files or findings.

Usage:
    python compare_data.py duplicates --id SESSION_ID     # find duplicate/similar findings
    python compare_data.py conflicts --id SESSION_ID      # find conflicting numbers on same topic
    python compare_data.py coverage --id SESSION_ID       # check sub-question coverage
"""

import re
import sys
from pathlib import Path
from collections import defaultdict

STATE_DIR = Path("/tmp/feishu-channel/research")


def load_state(session_id):
    path = STATE_DIR / f"{session_id}.md"
    if not path.exists():
        print(f"Session {session_id} not found")
        sys.exit(1)
    return path.read_text()


def extract_findings(content):
    """Parse findings from state markdown."""
    findings = []
    current = None
    for line in content.split('\n'):
        if line.startswith('### ['):
            # Parse: ### [HIGH] fact text
            m = re.match(r'### \[(HIGH|MED|LOW)\] (.+)', line)
            if m:
                current = {"confidence": m.group(1), "fact": m.group(2), "source": ""}
                findings.append(current)
        elif current and line.startswith('- Source:'):
            current["source"] = line.replace('- Source:', '').strip()
    return findings


def cmd_duplicates(session_id):
    """Find findings that are very similar (potential duplicates)."""
    content = load_state(session_id)
    findings = extract_findings(content)

    print(f"Total findings: {len(findings)}\n")

    # Simple similarity: check if one fact contains another
    for i, a in enumerate(findings):
        for j, b in enumerate(findings):
            if i >= j:
                continue
            # Check word overlap
            words_a = set(re.findall(r'[\u4e00-\u9fff]+|\w+', a["fact"].lower()))
            words_b = set(re.findall(r'[\u4e00-\u9fff]+|\w+', b["fact"].lower()))
            if not words_a or not words_b:
                continue
            overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
            if overlap > 0.6:
                print(f"SIMILAR ({overlap:.0%}):")
                print(f"  A: [{a['confidence']}] {a['fact'][:80]}")
                print(f"  B: [{b['confidence']}] {b['fact'][:80]}")
                print()


def cmd_conflicts(session_id):
    """Find findings with conflicting numbers about the same topic."""
    content = load_state(session_id)
    findings = extract_findings(content)

    # Extract numbers from each finding
    number_findings = []
    for f in findings:
        numbers = re.findall(r'\d[\d,，.]*(?:\.\d+)?(?:\s*(?:万|亿|%|％))?', f["fact"])
        if numbers:
            number_findings.append({"fact": f["fact"], "numbers": numbers, "source": f["source"]})

    print(f"Findings with numbers: {len(number_findings)}\n")

    # Group by topic keywords and check for number conflicts
    for i, a in enumerate(number_findings):
        for j, b in enumerate(number_findings):
            if i >= j:
                continue
            # Check if they discuss the same topic but have different numbers
            words_a = set(re.findall(r'[\u4e00-\u9fff]{2,}', a["fact"]))
            words_b = set(re.findall(r'[\u4e00-\u9fff]{2,}', b["fact"]))
            topic_overlap = words_a & words_b
            if len(topic_overlap) >= 2:
                nums_a = set(a["numbers"])
                nums_b = set(b["numbers"])
                if nums_a != nums_b and not nums_a.issubset(nums_b) and not nums_b.issubset(nums_a):
                    print(f"POTENTIAL CONFLICT (shared topics: {', '.join(list(topic_overlap)[:3])}):")
                    print(f"  A: {a['fact'][:80]} [{a['source'][:40]}]")
                    print(f"  B: {b['fact'][:80]} [{b['source'][:40]}]")
                    print()


def cmd_coverage(session_id):
    """Check coverage checklist status."""
    content = load_state(session_id)

    # Find checklist
    checklist_match = re.findall(r'- \[(.)\] (.+)', content)
    if not checklist_match:
        print("No coverage checklist found in state file")
        return

    covered = 0
    total = len(checklist_match)
    for check, item in checklist_match:
        status = "✅" if check == 'x' else "❌"
        print(f"{status} {item}")
        if check == 'x':
            covered += 1

    print(f"\nCoverage: {covered}/{total} ({covered/total*100:.0f}%)")
    if covered < total:
        print("GAPS: Some sub-questions need more sources")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    session_id = None
    for i, arg in enumerate(sys.argv):
        if arg == "--id" and i + 1 < len(sys.argv):
            session_id = sys.argv[i + 1]

    if not session_id:
        print("Missing --id SESSION_ID")
        sys.exit(1)

    if cmd == "duplicates":
        cmd_duplicates(session_id)
    elif cmd == "conflicts":
        cmd_conflicts(session_id)
    elif cmd == "coverage":
        cmd_coverage(session_id)
    else:
        print(f"Unknown: {cmd}")
        sys.exit(1)
