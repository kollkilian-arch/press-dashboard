import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import database as db
from categorizer import classify


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; NewsMonitor/1.0; +internal)"
    )
}


SELF_SELECTORS = {"", ":self", "self"}
GERMAN_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}


def _select_one(item, selector):
    selector = (selector or "").strip()
    if selector in SELF_SELECTORS:
        return item
    return item.select_one(selector)


def _extract_text(item, selector):
    el = _select_one(item, selector)
    return el.get_text(" ", strip=True) if el else ""


def _extract_link(item, selector):
    el = _select_one(item, selector)
    return el.get("href", "").strip() if el else ""


def _parse_date(item, config):
    selector = config.get("date") or config.get("published")
    text = _extract_text(item, selector) if selector else item.get_text(" ", strip=True)
    text = " ".join(text.split())
    if not text:
        return None

    date_format = config.get("date_format")
    if date_format:
        try:
            return datetime.strptime(text, date_format).strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            pass

    date_regex = config.get("date_regex")
    match = re.search(date_regex, text) if date_regex else None
    if match:
        text = match.group(1) if match.groups() else match.group(0)

    match = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b", text)
    if match:
        day, month, year = (int(part) for part in match.groups())
        if year < 100:
            year += 2000
        return f"{year:04d}-{month:02d}-{day:02d} 00:00:00"

    match = re.search(r"\b(\d{1,2})\.\s+([A-Za-zÄÖÜäöüß]+)\s+(\d{4})\b", text)
    if match:
        day = int(match.group(1))
        month = GERMAN_MONTHS.get(match.group(2).casefold())
        year = int(match.group(3))
        if month:
            return f"{year:04d}-{month:02d}-{day:02d} 00:00:00"

    match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return f"{year:04d}-{month:02d}-{day:02d} 00:00:00"

    return None


def _fetch_detail_soup(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def fetch_source(source):
    if not source["scraper_config"]:
        return 0
    try:
        config = json.loads(source["scraper_config"])
    except (json.JSONDecodeError, TypeError):
        return 0

    resp = requests.get(source["url"], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    article_sel = config.get("article", "article")
    title_sel = config.get("title", "h2")
    link_sel = config.get("link", "a")
    snippet_sel = config.get("snippet", "p")

    new_count = 0
    limit = config.get("limit")
    processed_count = 0
    for item in soup.select(article_sel):
        if limit and processed_count >= int(limit):
            break
        processed_count += 1

        title = _extract_text(item, title_sel)
        if not title and item.get("title"):
            title = item.get("title", "").strip()

        href = _extract_link(item, link_sel)
        if href and not href.startswith("http"):
            href = urljoin(source["url"], href)
        snippet = _extract_text(item, snippet_sel)[:500]
        published = _parse_date(item, config)

        detail_soup = None
        detail_snippet_sel = config.get("detail_snippet")
        detail_date_sel = config.get("detail_date")
        if href and ((detail_snippet_sel and not snippet) or (detail_date_sel and not published)):
            detail_soup = _fetch_detail_soup(href)
            if detail_snippet_sel and not snippet:
                snippet = _extract_text(detail_soup, detail_snippet_sel)[:500]
            if detail_date_sel and not published:
                published = _parse_date(detail_soup, {**config, "date": detail_date_sel})

        if not title:
            continue

        category = classify(title + " " + snippet, source["category_hint"])

        article_id, created = db.add_article(
            title,
            href or None,
            source["name"],
            snippet,
            category,
            published,
            source_id=source["id"],
            origin_type="scraper",
            return_status=True,
        )
        if created:
            with db.get_db() as conn:
                db.check_and_alert(conn, article_id, title, snippet)
            new_count += 1

    db.update_last_fetched(source["id"])
    return new_count


def fetch_all():
    sources = db.get_sources(active_only=True)
    total = 0
    for source in sources:
        if source["type"] == "scraper":
            try:
                total += fetch_source(source)
            except Exception as e:
                print(f"[Scraper] Fehler bei {source['name']}: {e}")
    return total
