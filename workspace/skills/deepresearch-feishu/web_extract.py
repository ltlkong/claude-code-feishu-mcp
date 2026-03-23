#!/usr/bin/env python3
"""Extract structured data from web page text — tables, numbers, lists.

Useful when WebFetch returns raw text and you need to parse specific data.

Usage:
    echo "page text..." | python web_extract.py numbers          # extract all numbers with context
    echo "page text..." | python web_extract.py dates            # extract dates
    echo "page text..." | python web_extract.py percentages      # extract percentages
    echo "page text..." | python web_extract.py money            # extract monetary amounts
    echo "page text..." | python web_extract.py urls             # extract URLs
"""

import re
import sys


def extract_numbers(text):
    """Extract numbers with surrounding context."""
    # Match numbers including decimals, commas, Chinese numerals
    pattern = r'(.{0,30}?)(\d[\d,，.]*(?:\.\d+)?(?:\s*(?:万|亿|百万|千万|trillion|billion|million|%|％))?)(.{0,30})'
    for m in re.finditer(pattern, text):
        before, number, after = m.groups()
        context = f"{before.strip()} **{number}** {after.strip()}"
        print(context.strip())


def extract_dates(text):
    """Extract date patterns."""
    patterns = [
        r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?',
        r'\d{4}[-/年]\d{1,2}[月]',
        r'\d{4}年',
        r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}',
        r'\d{1,2}/\d{1,2}/\d{4}',
    ]
    seen = set()
    for p in patterns:
        for m in re.finditer(p, text):
            date = m.group()
            if date not in seen:
                seen.add(date)
                # Get context
                start = max(0, m.start() - 20)
                end = min(len(text), m.end() + 40)
                context = text[start:end].replace('\n', ' ').strip()
                print(f"{date} | {context}")


def extract_percentages(text):
    """Extract percentage values with context."""
    pattern = r'(.{0,40}?)(\d+\.?\d*\s*[%％])(.{0,40})'
    for m in re.finditer(pattern, text):
        before, pct, after = m.groups()
        print(f"{pct.strip()} | {before.strip()} {pct.strip()} {after.strip()}")


def extract_money(text):
    """Extract monetary amounts."""
    patterns = [
        r'[\$￥¥€£]\s*\d[\d,，.]*(?:\.\d+)?(?:\s*(?:万|亿|百万|千万|trillion|billion|million))?',
        r'\d[\d,，.]*(?:\.\d+)?\s*(?:元|万元|亿元|美元|人民币|RMB|USD|CNY|yuan)',
        r'\d[\d,，.]*(?:\.\d+)?\s*(?:万|亿)\s*(?:元|美元)?',
    ]
    seen = set()
    for p in patterns:
        for m in re.finditer(p, text):
            amount = m.group()
            if amount not in seen:
                seen.add(amount)
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 30)
                context = text[start:end].replace('\n', ' ').strip()
                print(f"{amount} | {context}")


def extract_urls(text):
    """Extract URLs."""
    pattern = r'https?://[^\s<>\[\]\"\'）)，。、\u3000]+'
    for m in re.finditer(pattern, text):
        print(m.group())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    text = sys.stdin.read()

    if cmd == "numbers":
        extract_numbers(text)
    elif cmd == "dates":
        extract_dates(text)
    elif cmd == "percentages":
        extract_percentages(text)
    elif cmd == "money":
        extract_money(text)
    elif cmd == "urls":
        extract_urls(text)
    else:
        print(f"Unknown: {cmd}. Options: numbers, dates, percentages, money, urls")
        sys.exit(1)
