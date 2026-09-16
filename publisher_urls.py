"""Publisher URL identities shared by discovery, storage and text fetching."""
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse


def fonds_mobile_url(url: str) -> Optional[str]:
    """Map known German FONDS article URLs to the mobile reading endpoint.

    This is both the HTTP fetch target and a stable duplicate-detection key.
    The original article URL remains available for display and storage.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
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
