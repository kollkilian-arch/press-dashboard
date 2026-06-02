"""
Fetches and extracts the main article text from a URL.
Used before AI analysis so the model reads the full article, not just the RSS snippet.
"""
import re
from typing import Optional
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Tried in order — first match wins
CONTENT_SELECTORS = [
    "article",
    "main",
    "[role='main']",
    ".article-body",
    ".article-content",
    ".article-text",
    ".entry-content",
    ".post-content",
    ".story-body",
    ".news-content",
    ".text-content",
    "#article-body",
    "#content",
]

# Removed before text extraction
NOISE_SELECTORS = [
    "script", "style", "nav", "header", "footer", "aside",
    "[class*='ad-']", "[class*='-ad']", "[class*='banner']",
    "[class*='sidebar']", "[class*='related']", "[class*='recommend']",
    "[class*='subscribe']", "[class*='newsletter']", "[class*='cookie']",
    "[class*='popup']", "[class*='social']", "[class*='share']",
]


def _meta_content(soup: BeautifulSoup, *selectors: str) -> Optional[str]:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            value = el.get("content") or el.get("datetime") or el.get_text(separator=" ", strip=True)
            value = re.sub(r"\s+", " ", value or "").strip()
            if value:
                return value
    return None


def _source_from_url(url: str) -> str:
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    if not domain:
        return "Manuell"
    return domain.split(".")[0].replace("-", " ").title()


def _normalize_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            return raw[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw) else None
    if dt.tzinfo:
        dt = dt.astimezone(ZoneInfo("Europe/Berlin"))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _extract_main_text(soup: BeautifulSoup, max_chars: int = 6000) -> Optional[str]:
    for sel in NOISE_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    content = None
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el:
            content = el
            break
    if content is None:
        content = soup.body or soup

    paragraphs = [
        p.get_text(separator=" ", strip=True)
        for p in content.find_all("p")
        if len(p.get_text(strip=True)) >= 40
    ]
    text = " ".join(paragraphs)

    if len(text) < 200:
        text = content.get_text(separator=" ", strip=True)

    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars] if text else None


def fetch_article_details(url: str, max_chars: int = 6000) -> dict:
    """
    Fetch *url* and extract article metadata plus main body text.
    Returns an empty dict on failure so callers can keep manual fallback behavior.
    """
    if not url:
        return {}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        if "html" not in resp.headers.get("Content-Type", ""):
            return {}

        soup = BeautifulSoup(resp.text, "html.parser")
        final_url = resp.url or url

        title = _meta_content(
            soup,
            "meta[property='og:title']",
            "meta[name='twitter:title']",
            "title",
            "h1",
        )
        description = _meta_content(
            soup,
            "meta[property='og:description']",
            "meta[name='twitter:description']",
            "meta[name='description']",
        )
        source_name = _meta_content(
            soup,
            "meta[property='og:site_name']",
            "meta[name='application-name']",
        ) or _source_from_url(final_url)
        published_at = _normalize_date(_meta_content(
            soup,
            "meta[property='article:published_time']",
            "meta[name='article:published_time']",
            "meta[name='pubdate']",
            "meta[name='date']",
            "time[datetime]",
        ))

        full_text = _extract_main_text(soup, max_chars=max_chars)
        snippet = description or (full_text[:500] if full_text else "")

        return {
            "title": title or "",
            "source_name": source_name,
            "content_snippet": snippet[:500],
            "published_at": published_at,
            "full_text": full_text,
        }
    except Exception:
        return {}


def fetch_full_text(url: str, max_chars: int = 6000) -> Optional[str]:
    """
    Fetch *url* and extract the main article body.
    Returns cleaned text (up to *max_chars*) or None on any failure.
    """
    return fetch_article_details(url, max_chars=max_chars).get("full_text")
