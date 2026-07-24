#!/usr/bin/env python3
"""Builds index.html: BBC worldwide news, companies to watch, financial news + indices."""

import hashlib
import html
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from companies_build import fetch_company, pick_symbols

HERE = Path(__file__).resolve().parent
CLAUDE_BIN = "/Users/renarsm8/.local/bin/claude"
WORLD_FEED = "https://feeds.bbci.co.uk/news/world/rss.xml"
MARKET_FEED = "https://feeds.content.dowjones.io/public/rss/mw_topstories"
INDICES = [("^GSPC", "S&P 500"), ("^DJI", "Dow Jones"), ("^IXIC", "Nasdaq")]
MAX_STORIES = 12
MAX_MARKET_STORIES = 9
MEDIA_NS = "{http://search.yahoo.com/mrss/}"


CACHE_FILE = HERE / ".cache.json"


def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def content_key(parts):
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def fetch(url):
    # curl instead of urllib: the python.org install lacks local SSL root certs
    result = subprocess.run(
        ["curl", "-sfL", "--max-time", "15", "-A", "Mozilla/5.0 (NewsDesk)", url],
        capture_output=True, check=True,
    )
    return result.stdout


def clean_text(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_date(text):
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError):
        return None


def fetch_world():
    root = ET.fromstring(fetch(WORLD_FEED))
    articles = []
    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue

        thumb = item.find(f"{MEDIA_NS}thumbnail")
        image = thumb.get("url") if thumb is not None else None
        if image:
            image = image.replace("/standard/240/", "/standard/800/")

        articles.append({
            "title": title,
            "link": link,
            "description": clean_text(item.findtext("description")),
            "published": parse_date(item.findtext("pubDate")),
            "image": image,
            "section": "World",
        })
        if len(articles) >= MAX_STORIES:
            break
    return articles


def fetch_market_news():
    root = ET.fromstring(fetch(MARKET_FEED))
    stories = []
    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        media = item.find(f"{MEDIA_NS}content")
        image = media.get("url") if media is not None else None
        if image and "images.mktw.net" in image:
            image += "?width=700"
        stories.append({
            "title": title,
            "link": link,
            "description": clean_text(item.findtext("description")),
            "published": parse_date(item.findtext("pubDate")),
            "image": image,
            "section": "Markets",
        })
        if len(stories) >= MAX_MARKET_STORIES:
            break
    return stories


def fetch_indices():
    quotes = []
    for symbol, name in INDICES:
        try:
            url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
                   + symbol.replace("^", "%5E") + "?range=1d&interval=1d")
            meta = json.loads(fetch(url))["chart"]["result"][0]["meta"]
            price = meta["regularMarketPrice"]
            prev = meta["chartPreviousClose"]
            quotes.append({
                "name": name,
                "price": round(price, 2),
                "changePct": round((price - prev) / prev * 100, 2),
            })
        except Exception as exc:  # one dead quote shouldn't kill the build
            print(f"  (skipped {name}: {exc})")
    return quotes


def fetch_companies():
    companies = []
    for symbol, badge in pick_symbols().items():
        try:
            companies.append(fetch_company(symbol, badge))
        except Exception as exc:
            print(f"  ({symbol} failed: {exc})")
    return companies


def summarize_companies(companies):
    """One claude CLI call: per-company 'about' sentence + 2-sentence news summary."""
    for c in companies:
        c["about"] = ""
        c["newsSummary"] = ""
    if not companies:
        return
    brief = [{"symbol": c["symbol"], "name": c["name"], "changePct": c["changePct"],
              "headlines": [n["title"] for n in c["news"]]} for c in companies]
    prompt = (
        "For each company in the JSON below, using its headlines and today's percent move, "
        'output ONLY a JSON object shaped {"SYMBOL": {"about": "...", "newsSummary": "..."}}. '
        '"about": one plain-English sentence saying what the company does. '
        '"newsSummary": two sentences summarizing what its latest headlines say and, if clear, '
        "why the stock is moving today. Neutral and factual; no investment advice, no filler. "
        "No text before or after the JSON.\n\n" + json.dumps(brief, ensure_ascii=False)
    )
    cache = load_cache()
    key = content_key(brief)
    try:
        if cache.get("summaries_key") == key:
            summaries = cache["summaries"]
            print("  summaries: cached (content unchanged)")
        else:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", "--model", "haiku", prompt],
                capture_output=True, timeout=240, check=True,
            )
            match = re.search(r"\{.*\}", result.stdout.decode("utf-8"), re.S)
            summaries = json.loads(match.group(0))
            cache["summaries_key"] = key
            cache["summaries"] = summaries
            save_cache(cache)
            print(f"  summaries: fresh {len(summaries)}")
        for c in companies:
            entry = summaries.get(c["symbol"], {})
            c["about"] = str(entry.get("about", ""))
            c["newsSummary"] = str(entry.get("newsSummary", ""))
    except Exception as exc:
        print(f"  (summaries failed, popups will show headlines only: {exc})")


def classify_stories(world, market):
    """One claude CLI call: impact 1-3, tone, and an emoji for every story.

    Returns world reordered so the 4 most impactful (marked featured) lead.
    """
    for a in world + market:
        a["impact"] = 1
        a["tone"] = "neutral"
        a["emoji"] = ""
        a["themes"] = []
        a["featured"] = False
    items = ([{"id": f"w{i}", "kind": "world", "title": a["title"], "summary": a["description"][:200]}
              for i, a in enumerate(world)]
             + [{"id": f"m{i}", "kind": "markets", "title": a["title"], "summary": a["description"][:200]}
                for i, a in enumerate(market)])
    themes = ("luck, effort, truth, happiness, money, fear, courage, change, acceptance, "
              "kindness, cruelty, legacy, power, ordinary-life, ambition")
    prompt = (
        "Classify each news item below. Output ONLY a JSON object shaped "
        '{"<id>": {"impact": 1|2|3, "tone": "good"|"bad"|"neutral", "emoji": "x", "themes": ["a","b"]}}. '
        "impact: 3 = major global or market significance, 2 = notable, 1 = minor. "
        'tone (mainly for kind="markets"): "bad" = negative for markets/economy or grim news, '
        '"good" = positive, "neutral" = mixed or unclear. '
        'emoji: exactly one emoji — for kind="world" use the country\'s flag emoji when the story '
        "is clearly about one country, otherwise a topic emoji (e.g. conflict, energy, health, tech); "
        'for kind="markets" use an emoji for the industry or asset involved. '
        f"themes: 1-2 that best fit the story's human angle, chosen ONLY from: {themes}. "
        "No text before or after the JSON.\n\n" + json.dumps(items, ensure_ascii=False)
    )
    cache = load_cache()
    key = content_key(items)
    try:
        if cache.get("classify_key") == key:
            tags = cache["classify"]
            print("  story classification: cached (content unchanged)")
        else:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", "--model", "haiku", prompt],
                capture_output=True, timeout=240, check=True,
            )
            match = re.search(r"\{.*\}", result.stdout.decode("utf-8"), re.S)
            tags = json.loads(match.group(0))
            cache["classify_key"] = key
            cache["classify"] = tags
            save_cache(cache)
            print("  story classification: fresh")
        for prefix, arts in (("w", world), ("m", market)):
            for i, a in enumerate(arts):
                entry = tags.get(f"{prefix}{i}", {})
                a["impact"] = int(entry.get("impact", 1))
                a["tone"] = str(entry.get("tone", "neutral"))
                a["emoji"] = str(entry.get("emoji", ""))[:4]
                a["themes"] = [str(t) for t in entry.get("themes", [])][:2]
    except Exception as exc:
        print(f"  (classification failed, cards will be uniform: {exc})")

    order = sorted(range(len(world)), key=lambda i: -world[i]["impact"])
    featured = order[:4]
    for i in featured:
        world[i]["featured"] = True
    return ([world[i] for i in featured]
            + [world[i] for i in range(len(world)) if i not in set(featured)])


def main():
    articles = fetch_world()
    market_news = fetch_market_news()
    indices = fetch_indices()
    companies = fetch_companies()
    summarize_companies(companies)
    articles = classify_stories(articles, market_news)
    data = {
        "articles": articles,
        "market": {"indices": indices, "news": market_news},
        "companies": companies,
        "builtAt": datetime.now(timezone.utc).isoformat(),
    }

    template = (HERE / "template.html").read_text(encoding="utf-8")
    html_out = template.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    (HERE / "index.html").write_text(html_out, encoding="utf-8")
    print(f"Built index.html: {len(articles)} stories, {len(market_news)} market stories, "
          f"{len(indices)} indices, {len(companies)} companies.")


if __name__ == "__main__":
    main()
