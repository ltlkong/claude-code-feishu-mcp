#!/usr/bin/env python3
"""Source collection, deduplication, tier classification, and formatted output.

Extracts sources from research state files or raw search output,
deduplicates by domain, classifies reliability tier, and outputs
in multiple formats for different delivery channels.

Usage:
    # From research state file
    sources.py collect --id $SESSION_ID
    sources.py collect --id $SESSION_ID --format markdown
    sources.py collect --id $SESSION_ID --format plain
    sources.py collect --id $SESSION_ID --format numbered

    # From piped search output (extracts URLs + titles)
    echo "search output" | sources.py parse
    echo "search output" | sources.py parse --format markdown

    # Add sources manually
    sources.py add --id $SESSION_ID --url "https://..." --title "..." --tier 1

    # Show all sources for a session
    sources.py show --id $SESSION_ID --format markdown
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import urlparse

RESEARCH_DIR = "/tmp/feishu-channel/research"

# ── Tier Classification ──

TIER_1_DOMAINS = {
    # 中国官方
    "gov.cn", "stats.gov.cn", "mof.gov.cn", "pbc.gov.cn", "csrc.gov.cn",
    "mohurd.gov.cn", "ndrc.gov.cn", "samr.gov.cn", "mee.gov.cn",
    "news.cn", "xinhuanet.com", "people.com.cn", "cnr.cn", "cctv.com",
    # 上市公司/行业权威
    "sse.com.cn", "szse.cn", "hkexnews.hk",
    # 国际权威
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "imf.org", "worldbank.org", "oecd.org",
    # 研究机构
    "mckinsey.com", "bcg.com", "bain.com", "deloitte.com",
    "pwc.com", "ey.com", "kpmg.com",
    "gartner.com", "forrester.com", "idc.com",
    "grandviewresearch.com", "marketsandmarkets.com",
    # 地产行业权威
    "cih-index.com", "fang.com",
    "cushmanwakefield.com", "jll.com", "cbre.com", "savills.com",
    "morningstar.com",
}

TIER_2_DOMAINS = {
    # 主流财经媒体
    "caixin.com", "21jingji.com", "stcn.com", "nbd.com.cn",
    "bjnews.com.cn", "thepaper.cn", "jiemian.com", "cls.cn",
    "yicai.com", "eeo.com.cn", "guancha.cn",
    "cnbc.com", "scmp.com", "nikkei.com",
    "forbes.com", "fortune.com", "fastcompany.com",
    # 行业媒体
    "36kr.com", "leiphone.com", "ageclub.net",
    "sina.com.cn", "sina.com", "sohu.com", "qq.com",
    "eastmoney.com", "cnyes.com",
    # 研究/咨询
    "chyxx.com", "qianzhan.com", "chinabgao.com",
    "chnfund.com",
    # 知识平台
    "zhihu.com", "medium.com",
}


def classify_tier(url: str) -> int:
    """Classify a URL into Tier 1/2/3 based on domain."""
    try:
        domain = urlparse(url).netloc.lower()
        # Strip www.
        domain = domain.removeprefix("www.").removeprefix("m.")

        # Check exact match and parent domain
        for t1 in TIER_1_DOMAINS:
            if domain == t1 or domain.endswith("." + t1):
                return 1
        for t2 in TIER_2_DOMAINS:
            if domain == t2 or domain.endswith("." + t2):
                return 2
        return 3
    except Exception:
        return 3


def domain_key(url: str) -> str:
    """Extract domain for dedup."""
    try:
        d = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
        return d
    except Exception:
        return url


# ── Source Storage ──

def sources_path(session_id: str) -> str:
    return os.path.join(RESEARCH_DIR, f"{session_id}_sources.json")


def load_sources(session_id: str) -> list[dict]:
    path = sources_path(session_id)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def save_sources(session_id: str, sources: list[dict]):
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    with open(sources_path(session_id), "w") as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)


def add_source(session_id: str, url: str, title: str, tier: int = 0):
    """Add a source, auto-classifying tier if not provided."""
    sources = load_sources(session_id)
    if tier == 0:
        tier = classify_tier(url)

    # Dedup by URL
    existing_urls = {s["url"] for s in sources}
    if url not in existing_urls:
        sources.append({"url": url, "title": title, "tier": tier})
        save_sources(session_id, sources)
    return sources


# ── Extract from State File ──

def collect_from_state(session_id: str) -> list[dict]:
    """Extract sources from research state file and search outputs."""
    state_path = os.path.join(RESEARCH_DIR, f"{session_id}.md")
    if not os.path.exists(state_path):
        print(f"State file not found: {state_path}", file=sys.stderr)
        return []

    with open(state_path) as f:
        content = f.read()

    sources = load_sources(session_id)
    existing_urls = {s["url"] for s in sources}

    # Extract Source: lines from findings
    for match in re.finditer(r"^- Source:\s*(https?://\S+)", content, re.MULTILINE):
        url = match.group(1).rstrip(".,;:!?)")
        if url not in existing_urls:
            # Try to find the finding title (the line with ### above)
            pos = match.start()
            preceding = content[:pos]
            title_matches = re.findall(r"###\s*\[(?:HIGH|MEDIUM|LOW)\]\s*(.+?)$",
                                       preceding, re.MULTILINE)
            title = title_matches[-1][:80] if title_matches else domain_key(url)
            tier = classify_tier(url)
            sources.append({"url": url, "title": title, "tier": tier})
            existing_urls.add(url)

    save_sources(session_id, sources)
    return sources


# ── Parse from Search Output ──

def parse_search_output(text: str) -> list[dict]:
    """Extract sources from google_search.py output."""
    sources = []
    seen = set()

    # Match **[N] Title** ... URL: <url> pattern
    blocks = re.split(r"\*\*\[\d+\]", text)
    for block in blocks[1:]:  # skip before first match
        title_match = re.match(r"\s*(.+?)\*\*", block)
        url_match = re.search(r"URL:\s*(https?://\S+)", block)
        if not url_match:
            # Try bare URL at end
            url_match = re.search(r"(https?://\S+)", block)

        if url_match:
            url = url_match.group(1).rstrip(".,;:!?)")
            if url not in seen:
                title = title_match.group(1).strip() if title_match else domain_key(url)
                sources.append({
                    "url": url,
                    "title": title[:100],
                    "tier": classify_tier(url),
                })
                seen.add(url)

    return sources


# ── Formatting ──

def format_sources(sources: list[dict], fmt: str = "markdown") -> str:
    """Format source list for output."""
    # Sort by tier then title
    sorted_sources = sorted(sources, key=lambda s: (s["tier"], s["title"]))

    lines = []
    for i, s in enumerate(sorted_sources, 1):
        tier_label = f"Tier {s['tier']}"
        domain = domain_key(s["url"])

        if fmt == "markdown":
            lines.append(f"[{i}] [{s['title']}]({s['url']}) — {domain}（{tier_label}）")
        elif fmt == "plain":
            lines.append(f"[{i}] {s['title']} — {s['url']}（{tier_label}）")
        elif fmt == "numbered":
            lines.append(f"{i}. {s['title']} ({s['url']})")
        elif fmt == "json":
            pass  # handled separately

    if fmt == "json":
        return json.dumps(sorted_sources, ensure_ascii=False, indent=2)

    return "\n".join(lines)


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="Research source manager")
    sub = parser.add_subparsers(dest="cmd")

    # collect: extract from state file
    p_collect = sub.add_parser("collect", help="Extract sources from state file")
    p_collect.add_argument("--id", required=True, help="Session ID")
    p_collect.add_argument("--format", default="markdown",
                           choices=["markdown", "plain", "numbered", "json"])

    # parse: extract from piped search output
    p_parse = sub.add_parser("parse", help="Parse sources from search output (stdin)")
    p_parse.add_argument("--format", default="markdown",
                         choices=["markdown", "plain", "numbered", "json"])
    p_parse.add_argument("--id", help="Session ID to save to (optional)")

    # add: manually add a source
    p_add = sub.add_parser("add", help="Add a source manually")
    p_add.add_argument("--id", required=True, help="Session ID")
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--tier", type=int, default=0, help="1/2/3, 0=auto")

    # show: display all sources for a session
    p_show = sub.add_parser("show", help="Show all sources for a session")
    p_show.add_argument("--id", required=True, help="Session ID")
    p_show.add_argument("--format", default="markdown",
                        choices=["markdown", "plain", "numbered", "json"])

    # tier: classify a single URL
    p_tier = sub.add_parser("tier", help="Classify tier for a URL")
    p_tier.add_argument("url")

    args = parser.parse_args()

    if args.cmd == "collect":
        sources = collect_from_state(args.id)
        print(format_sources(sources, args.format))

    elif args.cmd == "parse":
        text = sys.stdin.read()
        sources = parse_search_output(text)
        if hasattr(args, "id") and args.id:
            existing = load_sources(args.id)
            existing_urls = {s["url"] for s in existing}
            for s in sources:
                if s["url"] not in existing_urls:
                    existing.append(s)
                    existing_urls.add(s["url"])
            save_sources(args.id, existing)
            sources = existing
        print(format_sources(sources, args.format))

    elif args.cmd == "add":
        tier = args.tier if args.tier > 0 else classify_tier(args.url)
        sources = add_source(args.id, args.url, args.title, tier)
        print(f"OK — {len(sources)} sources total")

    elif args.cmd == "show":
        sources = load_sources(args.id)
        if not sources:
            print("No sources found. Run 'collect' first.", file=sys.stderr)
        else:
            print(format_sources(sources, args.format))

    elif args.cmd == "tier":
        t = classify_tier(args.url)
        print(f"Tier {t} — {args.url}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
