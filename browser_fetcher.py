"""
Optional Playwright-backed article fetching.

Requests remains the default fetcher. This module is used only for source domains
configured for browser fetching, where a persisted browser profile may already
contain a legitimate consent/login session.
"""
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import text_fetcher


DEFAULT_PROFILE_ROOT = ".browser_profiles"


class BrowserFetchUnavailable(RuntimeError):
    pass


def _safe_domain(domain: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "_", (domain or "").lower()).strip("._") or "default"


def profile_root() -> Path:
    return Path(os.environ.get("PRESS_DASHBOARD_BROWSER_PROFILE_DIR", DEFAULT_PROFILE_ROOT))


def profile_dir_for_domain(domain: str) -> Path:
    return profile_root() / _safe_domain(domain)


def domain_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    return parsed.netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def fetch_rendered_html(url: str, domain: str = None, timeout_ms: int = 25000, headless: bool = True) -> dict:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserFetchUnavailable("Playwright ist nicht installiert.") from exc

    domain = domain or domain_from_url(url)
    user_data_dir = profile_dir_for_domain(domain)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=headless,
                viewport={"width": 1365, "height": 900},
                locale="de-DE",
            )
        except Exception as exc:
            raise BrowserFetchUnavailable(
                "Playwright-Browser konnte nicht gestartet werden. "
                "Bitte Chromium installieren und die lokalen Ausführungsrechte prüfen."
            ) from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            return {
                "html": page.content(),
                "final_url": page.url or (response.url if response else url),
            }
        finally:
            context.close()


def run_profile_setup(url: str, domain: str = None, timeout_ms: int = 60000) -> None:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserFetchUnavailable("Playwright ist nicht installiert.") from exc

    domain = domain or domain_from_url(url)
    user_data_dir = profile_dir_for_domain(domain)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=False,
                viewport={"width": 1365, "height": 900},
                locale="de-DE",
            )
        except Exception as exc:
            raise BrowserFetchUnavailable(
                "Playwright-Browser konnte nicht gestartet werden. "
                "Bitte Chromium installieren und die lokalen Ausführungsrechte prüfen."
            ) from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            input("Browser offen lassen, Consent/Login abschließen, dann hier Enter drücken...")
        finally:
            context.close()


def fetch_article_details(url: str, max_chars: int = 15000, domain: str = None) -> dict:
    rendered = fetch_rendered_html(url, domain=domain, headless=True)
    details = text_fetcher.extract_article_details_from_html(
        rendered["html"],
        rendered["final_url"],
        max_chars=max_chars,
    )
    details["fetch_method"] = "browser"
    return details
