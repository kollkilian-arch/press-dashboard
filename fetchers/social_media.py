import requests
import database as db

_TWITTER_SEARCH = "https://api.twitter.com/2/tweets/search/recent"
_LINKEDIN_AUTH  = "https://www.linkedin.com/oauth/v2/authorization"
_LINKEDIN_TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
_LINKEDIN_ME    = "https://api.linkedin.com/v2/me"


def fetch_twitter(bearer_token, keywords):
    """
    Search X/Twitter for posts matching any of the keywords (X API v2, app-only auth).
    All keywords are combined into one query to stay within free-tier rate limits
    (500K tweet reads/month; ~1 req/15 min on recent search).
    Returns the number of newly stored posts.
    """
    if not bearer_token or not keywords:
        return 0

    active = [k["keyword"] for k in keywords if k.get("is_active", 1)]
    if not active:
        return 0

    # Build combined query – quote multi-word terms; use hashtag as-is
    parts = []
    for kw in active[:10]:  # X free tier: keep queries short
        if kw.startswith("#"):
            parts.append(kw)
        elif " " in kw:
            parts.append(f'"{kw}"')
        else:
            parts.append(kw)
    query = "(" + " OR ".join(parts) + ") -is:retweet"

    headers = {"Authorization": f"Bearer {bearer_token}"}
    resp = requests.get(
        _TWITTER_SEARCH,
        headers=headers,
        params={
            "query": query,
            "max_results": 20,
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "name,username",
        },
        timeout=15,
    )

    if resp.status_code == 429:
        raise Exception("Rate Limit erreicht (Free Tier: 1 Anfrage / 15 min)")
    if resp.status_code == 401:
        raise Exception("Ungültiger Bearer Token – bitte in den Einstellungen prüfen")
    if resp.status_code != 200:
        raise Exception(f"X API Fehler {resp.status_code}: {resp.text[:200]}")

    data   = resp.json()
    tweets = data.get("data", [])
    users  = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

    new_count = 0
    for tweet in tweets:
        user    = users.get(tweet.get("author_id", ""), {})
        metrics = tweet.get("public_metrics", {})
        handle  = user.get("username", "")
        text    = tweet.get("text", "")

        # Match which keyword triggered this result
        text_lower = text.lower()
        matched_kw = next(
            (k for k in active if k.lstrip("#").lower() in text_lower),
            active[0],
        )

        added = db.add_social_post(
            platform="twitter",
            post_id=tweet["id"],
            author_name=user.get("name", ""),
            author_handle=handle,
            content=text,
            url=f"https://x.com/{handle}/status/{tweet['id']}" if handle else "",
            keyword=matched_kw,
            likes=metrics.get("like_count", 0),
            shares=metrics.get("retweet_count", 0),
            published_at=tweet.get("created_at", ""),
        )
        if added:
            new_count += 1

    return new_count


# ── LinkedIn OAuth helpers ────────────────────────────────────────────────────

def get_linkedin_auth_url(client_id, redirect_uri, state):
    """Return the LinkedIn OAuth 2.0 authorization URL."""
    from urllib.parse import urlencode
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": "r_liteprofile r_emailaddress",
    }
    return _LINKEDIN_AUTH + "?" + urlencode(params)


def exchange_linkedin_code(client_id, client_secret, code, redirect_uri):
    """Exchange an authorization code for an access token."""
    resp = requests.post(
        _LINKEDIN_TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_linkedin_profile(access_token):
    """Return the authenticated LinkedIn member's profile dict, or None on error."""
    resp = requests.get(
        _LINKEDIN_ME,
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    return resp.json()


def fetch_linkedin(access_token, keywords):  # noqa: ARG001
    """
    LinkedIn keyword/hashtag monitoring.

    NOTE: The standard LinkedIn API does not expose a public content search or
    hashtag feed for arbitrary keywords – that requires Marketing Developer
    Platform (MDP) partner access, which is not available to standard apps.

    This function is a placeholder so the connection can be verified and the
    feature can be extended once partner access is granted.  Returns (0, msg).
    """
    return 0, (
        "LinkedIn-Keyword-Suche erfordert LinkedIn Marketing Developer Platform "
        "Partner-Zugang (nicht im Standard-API enthalten)."
    )
