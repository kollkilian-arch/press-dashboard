"""Discover FONDS professionell articles through its public mobile news list."""
import logging
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

import database as db
import text_fetcher
from categorizer import classify
from publisher_urls import fonds_mobile_url

logger = logging.getLogger(__name__)
LIST_URL = "https://m.fondsprofessionell.de/news.php"
MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mär", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "dez"), 1
)}


def _list_date(text):
    match = re.search(r"(\d{1,2})\.\s+([\w]+)\.?\s+(\d{4})", text)
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return datetime(int(year), MONTHS[month[:3].lower()], int(day)).strftime("%Y-%m-%d 00:00:00")
    except (KeyError, ValueError):
        return None


def _parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for item in soup.select("ul.ul-news-list-view > li.li-news-list-view"):
        link = item.select_one("a.a-news-list-view[href]")
        heading = item.select_one("h2")
        if not link or not heading:
            continue
        url = fonds_mobile_url(urljoin(LIST_URL, link["href"]))
        title = heading.get_text(" ", strip=True)
        if not url or not title:
            continue
        divider = item.find_previous_sibling("li", class_="li-news-list-divider")
        snippet = item.select_one("p")
        entries.append({
            "url": url, "title": title,
            "content_snippet": snippet.get_text(" ", strip=True)[:500] if snippet else "",
            "published_at": _list_date(divider.get_text(" ", strip=True)) if divider else None,
        })
    if not entries:
        raise ValueError("Die mobile FONDS-Nachrichtenübersicht enthält keine erkennbaren Artikel.")

    next_page = None
    for link in soup.select("a[href]"):
        if link.get_text(" ", strip=True) != "Weitere News":
            continue
        parsed = urlsplit(urljoin(LIST_URL, link["href"]))
        if parsed.hostname == "m.fondsprofessionell.de" and parsed.path == "/news.php":
            value = parse_qs(parsed.query).get("cp", [""])[0]
            if value.isdigit() and int(value) > 0:
                next_page = int(value)
    return entries, next_page


def fetch_source(source, config):
    # Bound every run, including initial discovery. No article-ID enumeration.
    max_pages = max(1, min(int(config.get("max_pages", 5)), 10))
    entries_by_url = {}
    page = 1
    visited = set()
    for _ in range(max_pages):
        if page in visited:
            break
        visited.add(page)
        response = requests.get(
            f"{LIST_URL}?cp={page}&rd=1", headers=text_fetcher.HEADERS, timeout=15,
        )
        response.raise_for_status()
        entries, next_page = _parse_page(response.text)
        for entry in entries:
            previous = entries_by_url.get(entry["url"])
            if previous is None or (not previous["published_at"] and entry["published_at"]):
                entries_by_url[entry["url"]] = entry
        if next_page is None:
            break
        page = next_page

    new_count = 0
    for entry in entries_by_url.values():
        # Top news lack a date divider. Reuse known metadata or fetch just
        # those detail pages, keeping routine discovery inexpensive.
        if not entry["published_at"]:
            stored = db.find_duplicate_article(entry["title"], entry["url"])
            entry["published_at"] = stored.get("published_at") if stored else None
            if not entry["published_at"]:
                details = text_fetcher.fetch_article_details(entry["url"], include_error=True)
                if details.get("published_at"):
                    entry["published_at"] = details["published_at"]
                if details.get("content_snippet"):
                    entry["content_snippet"] = details["content_snippet"]
                if details.get("fetch_error"):
                    logger.warning("FONDS top-news metadata unavailable: %s", details["fetch_error"])

        title, snippet = entry["title"], entry["content_snippet"]
        category = classify(title + " " + snippet, source["category_hint"])
        article_id, created = db.add_article(
            title, entry["url"], source["name"], snippet, category, entry["published_at"],
            source_id=source["id"], origin_type="scraper", return_status=True,
            workspace=source.get("workspace") or "core",
        )
        if created:
            with db.get_db() as conn:
                db.check_and_alert(conn, article_id, title, snippet)
            new_count += 1

    db.update_last_fetched(source["id"])
    return new_count
