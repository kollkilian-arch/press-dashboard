"""
Fetches and extracts the main article text from a URL.
Used before AI analysis so the model reads the full article, not just the RSS snippet.
"""
import re
import json
import logging
from typing import Optional
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

try:
    # curl_cffi matches a real browser's TLS/HTTP2 fingerprint. It is used only
    # after a publisher rejects the ordinary HTTP client with 403/429.
    from curl_cffi import requests as browser_requests
except ImportError:  # Keeps the standard fetcher usable during partial installs.
    browser_requests = None

logger = logging.getLogger(__name__)

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


def _fonds_mobile_url(url: str) -> Optional[str]:
    """Map known German FONDS article URLs to the mobile reading endpoint.

    This is an HTTP fetch target only; callers retain the original article URL
    for display, storage and duplicate detection.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "fondsprofessionell.de", "www.fondsprofessionell.de", "m.fondsprofessionell.de",
    }:
        return None
    if parsed.path == "/newssingle.php":
        ids = parse_qs(parsed.query).get("uid", [])
        uid = ids[0] if len(ids) == 1 else ""
    else:
        match = re.fullmatch(r"/+news/(?:[^/]+/)*headline/[^/]+-([0-9]+)/?", parsed.path)
        uid = match.group(1) if match else ""
    if not re.fullmatch(r"[0-9]+", uid) or int(uid) <= 0:
        return None
    return f"https://m.fondsprofessionell.de/newssingle.php?uid={uid}&rd=1"


def _fonds_article_details(soup, url, max_chars, include_error):
    """Read the mobile article, excluding recommendations and page furniture."""
    article = soup.select_one(".singleNewsContainer")
    body = article.select_one(".newsContent") if article else None
    heading = article.select_one("h1") if article else None
    if not body or not heading or not body.get_text(strip=True):
        return _fetch_failure(
            url,
            "Auf der mobilen FONDS-professionell-Seite wurde kein Artikeltext erkannt.",
            include_error=include_error,
        )
    published_at = _normalize_date(_meta_content(article, ":scope > h3"))
    intro = _meta_content(article, ".newsContentWrapper > h3") or ""
    for selector in NOISE_SELECTORS:
        for element in body.select(selector):
            element.decompose()
    body_text = body.get_text(" ", strip=True)
    if not body_text:
        return _fetch_failure(url, "Im geladenen HTML wurde kein Artikeltext erkannt.", include_error=include_error)
    full_text = re.sub(r"\s+", " ", f"{intro} {body_text}").strip()
    return {
        "title": heading.get_text(" ", strip=True),
        "source_name": "FONDS professionell",
        "content_snippet": (intro or full_text)[:500],
        "published_at": published_at,
        "full_text": full_text[:max_chars],
        "content_kind": "fulltext",
    }


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
    content = None
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el:
            content = el
            break
    has_scoped_content = content is not None

    # Pick the article container before cleaning the document. Some publishers
    # (including Versicherungsbote) keep the article intro inside an
    # ``<article><header>`` element. Removing every header from the full page
    # first therefore discarded genuine article copy. Once a content container
    # is known, clean only inside it and preserve its header.
    if content is None:
        content = soup.body or soup

    for sel in NOISE_SELECTORS:
        if sel == "header" and has_scoped_content:
            continue
        for el in content.select(sel):
            el.decompose()

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


def _fetch_failure(url: str, reason: str, *, include_error: bool) -> dict:
    """Return a caller-safe fetch failure while retaining the cause in logs."""
    logger.warning("Article fetch failed for %s: %s", url, reason)
    return {"fetch_error": reason} if include_error else {}


def _article_details_from_response(resp, url: str, max_chars: int, include_error: bool) -> dict:
    """Extract article metadata and copy from an already successful response."""
    if "html" not in resp.headers.get("Content-Type", "").lower():
        content_type = resp.headers.get("Content-Type", "unbekannt")
        return _fetch_failure(
            url,
            f"Die Website lieferte keine HTML-Seite (Content-Type: {content_type}).",
            include_error=include_error,
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    final_url = str(resp.url or url)

    if _fonds_mobile_url(url):
        # A consent page or a redirect must never become AI input as full text.
        if _fonds_mobile_url(final_url) != _fonds_mobile_url(url):
            return _fetch_failure(
                url,
                "Die mobile FONDS-professionell-Seite hat auf eine andere Seite weitergeleitet.",
                include_error=include_error,
            )
        return _fonds_article_details(soup, url, max_chars, include_error)

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
    if not full_text:
        return _fetch_failure(
            url,
            "Im geladenen HTML wurde kein Artikeltext erkannt.",
            include_error=include_error,
        )
    snippet = description or full_text[:500]

    return {
        "title": title or "",
        "source_name": source_name,
        "content_snippet": snippet[:500],
        "published_at": published_at,
        "full_text": full_text,
        "content_kind": "fulltext",
    }


def _fetch_with_browser_retry(url: str, max_chars: int, include_error: bool, status: int) -> dict:
    """Retry a publisher rejection once with a browser-compatible HTTP client."""
    if browser_requests is None:
        return _fetch_failure(
            url,
            f"Die Website hat den Abruf abgelehnt (HTTP {status}); der Browser-Abruf ist nicht verfügbar.",
            include_error=include_error,
        )

    logger.info("Retrying rejected article fetch with browser-compatible client: %s", url)
    try:
        resp = browser_requests.get(
            url,
            headers=HEADERS,
            timeout=15,
            allow_redirects=True,
            impersonate="chrome",
        )
    except Exception as exc:
        return _fetch_failure(
            url,
            f"Die Website hat den Abruf abgelehnt (HTTP {status}); Browser-Abruf fehlgeschlagen ({exc.__class__.__name__}).",
            include_error=include_error,
        )

    retry_status = getattr(resp, "status_code", None)
    if retry_status is None or retry_status >= 400:
        detail = f"HTTP {retry_status}" if retry_status else "unbekannter HTTP-Fehler"
        return _fetch_failure(
            url,
            f"Die Website hat den Abruf abgelehnt (HTTP {status}); Browser-Abruf ebenfalls abgelehnt ({detail}).",
            include_error=include_error,
        )
    try:
        return _article_details_from_response(resp, url, max_chars, include_error)
    except Exception as exc:
        logger.exception("Browser-compatible article extraction failed for %s", url)
        return _fetch_failure(
            url,
            f"Der Artikeltext konnte nach dem Browser-Abruf nicht verarbeitet werden ({exc.__class__.__name__}).",
            include_error=include_error,
        )


def fetch_article_details(url: str, max_chars: int = 15000, include_error: bool = False) -> dict:
    """
    Fetch *url* and extract article metadata plus main body text.
    Returns an empty dict on failure by default so existing callers can keep
    manual fallback behavior. Callers that need an auditable reason can pass
    ``include_error=True`` and receive ``fetch_error`` instead.
    """
    if not url:
        return {}
    try:
        url = _fonds_mobile_url(url) or url
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        return _article_details_from_response(resp, url, max_chars, include_error)
    except requests.Timeout:
        return _fetch_failure(
            url,
            "Zeitüberschreitung beim Laden der Website.", include_error=include_error
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {403, 429}:
            return _fetch_with_browser_retry(url, max_chars, include_error, status)
        detail = f" (HTTP {status})" if status else ""
        return _fetch_failure(
            url,
            f"Die Website hat den Abruf abgelehnt oder konnte nicht erreicht werden{detail}.",
            include_error=include_error,
        )
    except requests.RequestException as exc:
        return _fetch_failure(
            url,
            f"Netzwerkfehler beim Laden der Website ({exc.__class__.__name__}).",
            include_error=include_error,
        )
    except Exception as exc:
        logger.exception("Article extraction failed for %s", url)
        return _fetch_failure(
            url,
            f"Der Artikeltext konnte nicht verarbeitet werden ({exc.__class__.__name__}).",
            include_error=include_error,
        )


def fetch_full_text(url: str, max_chars: int = 15000) -> Optional[str]:
    """
    Fetch *url* and extract the main article body.
    Returns cleaned text (up to *max_chars*) or None on any failure.
    """
    return fetch_article_details(url, max_chars=max_chars).get("full_text")


def merge_stored_article_fallback(fetched: dict, stored_article, min_chars: int = 300) -> dict:
    """Fill a weak live fetch from an article already known to PresseRadar.

    RSS feeds often contain a useful snippet even when the publisher temporarily
    blocks or changes its article page. This mirrors the pin-article fallback and
    keeps the fetched metadata whenever it is more complete.
    """
    result = dict(fetched or {})
    if not stored_article:
        return result

    for target, source in (
        ("title", "title"),
        ("source_name", "source_name"),
        ("published_at", "published_at"),
        ("content_snippet", "content_snippet"),
    ):
        if not str(result.get(target) or "").strip():
            result[target] = stored_article.get(source) or ""

    current_text = str(result.get("full_text") or "").strip()
    stored_full_text = str(stored_article.get("full_text") or "").strip()
    stored_snippet = str(stored_article.get("content_snippet") or "").strip()
    best_stored_text = max((stored_full_text, stored_snippet), key=len)
    if len(current_text) < min_chars and len(best_stored_text) > len(current_text):
        result["full_text"] = best_stored_text
        result["content_kind"] = "stored_fulltext" if best_stored_text == stored_full_text else "teaser"
    return result
