"""
Fetches and extracts the main article text from a URL.
Used before AI analysis so the model reads the full article, not just the RSS snippet.
"""
import re
from typing import Optional

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


def fetch_full_text(url: str, max_chars: int = 6000) -> Optional[str]:
    """
    Fetch *url* and extract the main article body.
    Returns cleaned text (up to *max_chars*) or None on any failure.
    """
    if not url:
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        if "html" not in resp.headers.get("Content-Type", ""):
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Strip noise elements
        for sel in NOISE_SELECTORS:
            for el in soup.select(sel):
                el.decompose()

        # Find the content block
        content = None
        for sel in CONTENT_SELECTORS:
            el = soup.select_one(sel)
            if el:
                content = el
                break
        if content is None:
            content = soup.body or soup

        # Collect meaningful paragraphs (skip very short fragments)
        paragraphs = [
            p.get_text(separator=" ", strip=True)
            for p in content.find_all("p")
            if len(p.get_text(strip=True)) >= 40
        ]
        text = " ".join(paragraphs)

        if len(text) < 200:
            # Fallback: raw text of the content block
            text = content.get_text(separator=" ", strip=True)

        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars] if text else None

    except Exception:
        return None
