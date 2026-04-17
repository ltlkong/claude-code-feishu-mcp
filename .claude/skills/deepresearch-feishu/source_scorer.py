#!/usr/bin/env python3
"""Source Reliability Scorer — Ranks sources by trustworthiness.

Inspired by: Genspark's dedicated fact-checking agent role.

Scores sources based on domain reputation, content type, recency,
and whether data can be cross-verified.

Usage:
    python source_scorer.py --url "https://reuters.com/article/..." --type "news"
    echo '[{"url": "...", "type": "..."}]' | python source_scorer.py --stdin
"""

import json
import sys
import argparse
from datetime import datetime
from urllib.parse import urlparse

# Domain trust tiers
TIER_1 = {  # Official / Government / Top institutions — highest trust
    "gov.cn", "stats.gov.cn", "mof.gov.cn", "ndrc.gov.cn",  # China government
    "pbc.gov.cn", "csrc.gov.cn", "safe.gov.cn",              # Financial regulators
    "who.int", "worldbank.org", "imf.org", "un.org",          # International orgs
    "sec.gov", "fed.gov", "europa.eu",                        # Foreign government
    "nature.com", "science.org", "lancet.com",                # Top academic
}

TIER_2 = {  # Major news agencies / Top research firms
    "reuters.com", "apnews.com", "bbc.com", "ft.com",
    "wsj.com", "nytimes.com", "bloomberg.com", "economist.com",
    "caixin.com", "thepaper.cn", "yicai.com",                 # China business media
    "mckinsey.com", "bcg.com", "bain.com", "deloitte.com",    # Consulting
    "idc.com", "gartner.com", "statista.com",                 # Research firms
}

TIER_3 = {  # Reputable tech/business media
    "36kr.com", "huxiu.com", "geekpark.net",                  # Tech media
    "cnbc.com", "techcrunch.com", "wired.com",
    "sina.com.cn", "163.com", "qq.com", "sohu.com",           # Portals
    "eastmoney.com", "xueqiu.com",                            # Finance
}

# Content type weights
TYPE_WEIGHTS = {
    "government_report": 1.0,
    "academic_paper": 0.95,
    "industry_report": 0.85,
    "financial_filing": 0.85,
    "major_news": 0.75,
    "company_announcement": 0.7,
    "tech_media": 0.6,
    "blog": 0.4,
    "social_media": 0.25,
    "unknown": 0.5,
}


def score_source(url: str, content_type: str = "unknown",
                 publish_date: str = "", claim_count: int = 1) -> dict:
    """Score a source's reliability on a 0-1 scale.

    Args:
        url: Source URL
        content_type: Type of content (see TYPE_WEIGHTS)
        publish_date: ISO date string (YYYY-MM-DD)
        claim_count: How many other sources make the same claim

    Returns:
        Score breakdown and final score
    """
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")

    # Domain score
    domain_score = 0.5  # default
    for tier_domains, tier_score in [(TIER_1, 1.0), (TIER_2, 0.8), (TIER_3, 0.6)]:
        if any(domain.endswith(d) for d in tier_domains):
            domain_score = tier_score
            break

    # Content type score
    type_score = TYPE_WEIGHTS.get(content_type, 0.5)

    # Recency score
    recency_score = 0.5
    if publish_date:
        try:
            pub = datetime.strptime(publish_date, "%Y-%m-%d")
            days_old = (datetime.now() - pub).days
            if days_old <= 30:
                recency_score = 1.0
            elif days_old <= 90:
                recency_score = 0.9
            elif days_old <= 365:
                recency_score = 0.7
            elif days_old <= 730:
                recency_score = 0.5
            else:
                recency_score = 0.3
        except ValueError:
            pass

    # Cross-verification bonus
    verification_score = min(1.0, 0.3 + (claim_count * 0.2))

    # Weighted final score
    final = (
        domain_score * 0.3 +
        type_score * 0.25 +
        recency_score * 0.2 +
        verification_score * 0.25
    )

    # Determine reliability label
    if final >= 0.8:
        label = "high"
    elif final >= 0.6:
        label = "medium"
    elif final >= 0.4:
        label = "low"
    else:
        label = "unreliable"

    return {
        "url": url,
        "domain": domain,
        "scores": {
            "domain": round(domain_score, 2),
            "content_type": round(type_score, 2),
            "recency": round(recency_score, 2),
            "verification": round(verification_score, 2),
        },
        "final_score": round(final, 2),
        "reliability": label,
        "recommendation": (
            "Use as primary source" if label == "high"
            else "Use with verification" if label == "medium"
            else "Cross-verify before citing" if label == "low"
            else "Do not cite without corroboration"
        ),
    }


def rank_sources(sources: list[dict]) -> list[dict]:
    """Score and rank multiple sources."""
    scored = []
    for src in sources:
        result = score_source(
            url=src.get("url", ""),
            content_type=src.get("type", "unknown"),
            publish_date=src.get("date", ""),
            claim_count=src.get("claim_count", 1),
        )
        result["title"] = src.get("title", "")
        scored.append(result)

    scored.sort(key=lambda x: x["final_score"], reverse=True)
    return scored


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score source reliability")
    parser.add_argument("--url", help="Single URL to score")
    parser.add_argument("--type", default="unknown", help="Content type")
    parser.add_argument("--date", default="", help="Publish date (YYYY-MM-DD)")
    parser.add_argument("--stdin", action="store_true", help="Read JSON array from stdin")

    args = parser.parse_args()

    if args.stdin:
        sources = json.load(sys.stdin)
        results = rank_sources(sources)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.url:
        result = score_source(args.url, args.type, args.date)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        parser.print_help()
