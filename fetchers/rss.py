import calendar
import os
import feedparser
import database as db
from categorizer import classify
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _app_timezone():
    name = os.environ.get("APP_TIMEZONE", "Europe/Berlin")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Berlin")


def _parse_date(entry):
    """
    feedparser always returns published_parsed/updated_parsed in UTC.
    Use calendar.timegm() to read the struct as UTC, then convert it to
    the configured app timezone so dates do not depend on the server TZ.
    """
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                utc_ts = calendar.timegm(val)
                local_dt = datetime.fromtimestamp(utc_ts, _app_timezone())
                return local_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
    return None


def fetch_source(source):
    feed = feedparser.parse(source["url"])
    new_count = 0
    for entry in feed.entries:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        summary = getattr(entry, "summary", "") or ""
        # Strip HTML tags from summary
        import re
        summary = re.sub(r"<[^>]+>", " ", summary).strip()
        summary = summary[:500]

        if not title:
            continue

        category = classify(title + " " + summary, source["category_hint"])
        published = _parse_date(entry)

        with db.get_db() as conn:
            cur = conn.execute(
                """INSERT INTO articles
                   (title, url, source_id, source_name, content_snippet, category, published_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (url) DO NOTHING
                   RETURNING id""",
                (title, link or None, source["id"], source["name"], summary, category, published),
            )
            row = cur.fetchone()
            if row:
                db.check_and_alert(conn, row["id"], title, summary)
                new_count += 1
            elif link and published:
                conn.execute(
                    """UPDATE articles
                       SET published_at = %s, source_id = %s, source_name = %s
                       WHERE url = %s""",
                    (published, source["id"], source["name"], link),
                )

    db.update_last_fetched(source["id"])
    return new_count


def fetch_all():
    sources = db.get_sources(active_only=True)
    total = 0
    for source in sources:
        if source["type"] == "rss":
            try:
                total += fetch_source(source)
            except Exception as e:
                print(f"[RSS] Fehler bei {source['name']}: {e}")
    return total
