#!/usr/bin/env python3
"""PROTOTYPE: builds companies.html — companies worth a look today.

Mixes Yahoo Finance trending tickers with the day's top gainers/losers and
most-traded stocks; each card gets an intraday sparkline and recent headlines.
"""

import html
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_CARDS = 8
HEADLINES_PER_COMPANY = 4


def fetch(url):
    result = subprocess.run(
        ["curl", "-sfL", "--max-time", "15", "-A", "Mozilla/5.0 (NewsDesk)", url],
        capture_output=True, check=True,
    )
    return result.stdout


def clean_text(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def is_equity(symbol):
    return not any(ch in symbol for ch in "-=^.")


def pick_symbols():
    """Ordered dict of symbol -> badge, mixing trending + screener lists."""
    picks = {}

    def add(symbols, badge, limit):
        added = 0
        for sym in symbols:
            if added >= limit or len(picks) >= MAX_CARDS:
                return
            if sym not in picks and is_equity(sym):
                picks[sym] = badge
                added += 1

    try:
        data = json.loads(fetch("https://query1.finance.yahoo.com/v1/finance/trending/US?count=20"))
        add([q["symbol"] for q in data["finance"]["result"][0]["quotes"]], "Trending", 4)
    except Exception as exc:
        print(f"  (trending failed: {exc})")

    for scr, badge, limit in [("day_gainers", "Top gainer", 2),
                              ("day_losers", "Top loser", 1),
                              ("most_actives", "Most active", 2)]:
        try:
            data = json.loads(fetch(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
                f"?scrIds={scr}&count=10"))
            add([q["symbol"] for q in data["finance"]["result"][0]["quotes"]], badge, limit)
        except Exception as exc:
            print(f"  ({scr} failed: {exc})")

    return picks


def fetch_company(symbol, badge):
    data = json.loads(fetch(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=15m"))
    result = data["chart"]["result"][0]
    meta = result["meta"]
    price = meta["regularMarketPrice"]
    prev = meta["chartPreviousClose"]
    points = [(t, c) for t, c in zip(result.get("timestamp", []),
                                     result["indicators"]["quote"][0]["close"])
              if c is not None]
    closes = [c for _, c in points]
    times = [t for t, _ in points]

    headlines = []
    try:
        root = ET.fromstring(fetch(
            "https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={symbol}&region=US&lang=en-US"))
        for item in root.findall(".//item")[:HEADLINES_PER_COMPANY]:
            title = clean_text(item.findtext("title"))
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            published = None
            try:
                published = parsedate_to_datetime(item.findtext("pubDate")).isoformat()
            except (TypeError, ValueError):
                pass
            headlines.append({"title": title, "link": link, "published": published})
    except Exception as exc:
        print(f"  (news for {symbol} failed: {exc})")

    return {
        "symbol": symbol,
        "badge": badge,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "price": round(price, 2),
        "changePct": round((price - prev) / prev * 100, 2),
        "prevClose": round(prev, 2),
        "dayHigh": round(meta.get("regularMarketDayHigh", 0), 2),
        "dayLow": round(meta.get("regularMarketDayLow", 0), 2),
        "spark": [round(c, 4) for c in closes],
        "sparkT": times,
        "news": headlines,
    }


def main():
    companies = []
    for symbol, badge in pick_symbols().items():
        try:
            companies.append(fetch_company(symbol, badge))
            print(f"  {symbol}: ok")
        except Exception as exc:
            print(f"  ({symbol} failed: {exc})")

    data = {"companies": companies, "builtAt": datetime.now(timezone.utc).isoformat()}
    template = (HERE / "companies_template.html").read_text(encoding="utf-8")
    out = template.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    (HERE / "companies.html").write_text(out, encoding="utf-8")
    print(f"Built companies.html with {len(companies)} companies.")


if __name__ == "__main__":
    main()
