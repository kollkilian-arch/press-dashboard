"""
Fetches and extracts the main article text from a URL.
Used before AI analysis so the model reads the full article, not just the RSS snippet.
"""
import re
import json
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

INTERSTITIAL_PATH_MARKERS = (
    "/consent",
    "/cookie",
    "/cookies",
    "/datenschutz",
    "/privacy",
    "/terms",
    "/agb",
)

CONSENT_TEXT_MARKERS = (
    "datenschutzeinstellungen",
    "datenschutzinformation",
    "einwilligung",
    "einverstanden",
    "cookies",
    "personenbezogene daten",
    "partnern (",
    "zwecke der datenverarbeitung",
    "speichern von oder zugriff auf informationen",
    "auswahl akzeptieren",
    "alles akzeptieren",
    "nichts akzeptieren",
    "wie gewohnt mit werbung lesen",
    "werbefrei lesen",
    "personalisierte werbung",
)


def _meta_content(soup: BeautifulSoup, *selectors: str) -> Optional[str]:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            value = el.get("content") or el.get("datetime") or el.get_text(separator=" ", strip=True)
            value = re.sub(r"\s+", " ", value or "").strip()
            if value:
                return value
    return None


def _json_ld_objects(value):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph:
            yield from _json_ld_objects(graph)
    elif isinstance(value, list):
        for item in value:
            yield from _json_ld_objects(item)


def _schema_type_matches(value) -> bool:
    types = value if isinstance(value, list) else [value]
    for schema_type in types:
        if not isinstance(schema_type, str):
            continue
        name = schema_type.rsplit("/", 1)[-1].casefold()
        if name in {"article", "newsarticle", "blogposting"}:
            return True
    return False


def _json_ld_date_candidates(soup: BeautifulSoup) -> list[str]:
    typed_candidates = []
    fallback_candidates = []

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _json_ld_objects(data):
            candidate = obj.get("datePublished") or obj.get("dateCreated")
            if not candidate:
                continue
            if isinstance(candidate, list):
                candidate = next((item for item in candidate if item), None)
            if not isinstance(candidate, str):
                continue
            if _schema_type_matches(obj.get("@type")):
                typed_candidates.append(candidate)
            else:
                fallback_candidates.append(candidate)

    return typed_candidates or fallback_candidates


def _visible_date_candidates(soup: BeautifulSoup) -> list[str]:
    candidates = []
    for selector in (
        "article time[datetime]",
        "main time[datetime]",
        "[role='main'] time[datetime]",
        ".opener_content time[datetime]",
        ".post time[datetime]",
        ".entry time[datetime]",
    ):
        for el in soup.select(selector):
            value = el.get("datetime") or el.get_text(separator=" ", strip=True)
            if value:
                candidates.append(value)

    h1 = soup.find("h1")
    if h1:
        seen = set()
        node = h1
        for _ in range(4):
            node = node.parent
            if not node or id(node) in seen:
                break
            seen.add(id(node))
            text = node.get_text(" ", strip=True)
            if text and re.search(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", text):
                candidates.append(text)

    return candidates


def _extract_published_at(soup: BeautifulSoup) -> Optional[str]:
    candidate_groups = [
        _json_ld_date_candidates(soup),
        [
            _meta_content(
                soup,
                "meta[property='article:published_time']",
                "meta[name='article:published_time']",
                "meta[property='og:published_time']",
                "meta[itemprop='datePublished']",
                "meta[name='pubdate']",
            )
        ],
        _visible_date_candidates(soup),
        [
            _meta_content(
                soup,
                "meta[name='date']",
                "time[datetime]",
            )
        ],
    ]

    for group in candidate_groups:
        for candidate in group:
            normalized = _normalize_date(candidate)
            if normalized:
                return normalized
    return None


def _source_from_url(url: str) -> str:
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    if not domain:
        return "Manuell"
    return domain.split(".")[0].replace("-", " ").title()


def _is_interstitial_url(url: str) -> bool:
    parsed = urlparse(url or "")
    path = parsed.path.casefold()
    return any(path == marker or path.startswith(f"{marker}/") for marker in INTERSTITIAL_PATH_MARKERS)


def _looks_like_consent_page(soup: BeautifulSoup, final_url: str, text: Optional[str] = None) -> bool:
    if _is_interstitial_url(final_url):
        return True

    title = _meta_content(
        soup,
        "meta[property='og:title']",
        "meta[name='twitter:title']",
        "title",
        "h1",
    ) or ""
    description = _meta_content(
        soup,
        "meta[property='og:description']",
        "meta[name='twitter:description']",
        "meta[name='description']",
    ) or ""
    haystack = " ".join(
        part for part in (title, description, text or soup.get_text(" ", strip=True)[:6000]) if part
    ).casefold()
    marker_count = sum(1 for marker in CONSENT_TEXT_MARKERS if marker in haystack)
    has_article_schema = any(
        _schema_type_matches(obj.get("@type"))
        for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})
        for obj in _json_ld_objects(_safe_json_loads(script.string or script.get_text()))
    )

    title_is_generic = title.strip().casefold() in {
        "fonds professionell",
        "willkommen bei fonds professionell",
        "datenschutz",
        "datenschutzerklärung",
        "datenschutzeinstellungen",
    }
    return not has_article_schema and (marker_count >= 6 or (title_is_generic and marker_count >= 3))


def _safe_json_loads(raw: Optional[str]):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


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
            match = re.search(
                r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})(?:,\s*(\d{1,2}):(\d{2})(?:\s*Uhr)?)?",
                raw,
            )
            if match:
                day, month, year, hour, minute = match.groups()
                return (
                    f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
                    f"{int(hour or 0):02d}:{int(minute or 0):02d}:00"
                )
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


def extract_article_details_from_html(html: str, final_url: str, max_chars: int = 15000) -> dict:
    soup = BeautifulSoup(html or "", "html.parser")

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
    published_at = _extract_published_at(soup)

    full_text = _extract_main_text(soup, max_chars=max_chars)
    fetch_status = "ok"
    fetch_reason = ""
    if _looks_like_consent_page(soup, final_url, full_text):
        fetch_status = "blocked_by_consent"
        fetch_reason = "consent_or_legal_interstitial"
        title = ""
        description = ""
        full_text = None
    snippet = description or (full_text[:500] if full_text else "")

    return {
        "title": title or "",
        "source_name": source_name,
        "content_snippet": snippet[:500],
        "published_at": published_at,
        "full_text": full_text,
        "fetch_status": fetch_status,
        "fetch_reason": fetch_reason,
        "final_url": final_url,
        "fetch_method": "requests",
    }


def fetch_article_details(url: str, max_chars: int = 15000, cookie_header: Optional[str] = None) -> dict:
    """
    Fetch *url* and extract article metadata plus main body text.
    Returns an empty dict on failure so callers can keep manual fallback behavior.
    """
    if not url:
        return {}
    try:
        headers = dict(HEADERS)
        if cookie_header:
            headers["Cookie"] = cookie_header
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        if "html" not in resp.headers.get("Content-Type", ""):
            return {}

        final_url = resp.url or url
        return extract_article_details_from_html(resp.text, final_url, max_chars=max_chars)
    except Exception:
        return {}


def fetch_full_text(url: str, max_chars: int = 15000, cookie_header: Optional[str] = None) -> Optional[str]:
    """
    Fetch *url* and extract the main article body.
    Returns cleaned text (up to *max_chars*) or None on any failure.
    """
    return fetch_article_details(url, max_chars=max_chars, cookie_header=cookie_header).get("full_text")
