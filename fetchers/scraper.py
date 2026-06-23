import json
import requests
from bs4 import BeautifulSoup
import database as db
from categorizer import classify


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; NewsMonitor/1.0; +internal)"
    )
}


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
    for item in soup.select(article_sel):
        title_el = item.select_one(title_sel)
        link_el = item.select_one(link_sel)
        snippet_el = item.select_one(snippet_sel)

        title = title_el.get_text(strip=True) if title_el else ""
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            from urllib.parse import urljoin
            href = urljoin(source["url"], href)
        snippet = snippet_el.get_text(strip=True)[:500] if snippet_el else ""

        if not title:
            continue

        category = classify(title + " " + snippet, source["category_hint"])

        article_id, created = db.add_article(
            title,
            href or None,
            source["name"],
            snippet,
            category,
            None,
            source_id=source["id"],
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
