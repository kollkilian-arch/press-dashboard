import os
import json
import re
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Sentinel stored in ai_summary when pinning was attempted but no fulltext
# could be fetched. Checked in templates and in the daily report builder.
NO_FULLTEXT = "__kein_volltext__"

TRACKING_QUERY_PARAMS = {
    "fbclid",
    "gclid",
    "gbraid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
    "utm_id",
    "yclid",
}

STARTER_SOURCES = [
    ("VersicherungsJournal", "https://www.versicherungsjournal.de/rss-files/VersicherungsJournal.xml", "rss", "markt"),
    ("Versicherungswirtschaft heute", "https://www.versicherungswirtschaft-heute.de/feed/", "rss", "markt"),
    ("Handelsblatt – Finanzen", "https://www.handelsblatt.com/contentexport/feed/finanzen", "rss", "markt"),
    ("manager magazin – Finanzen", "https://www.manager-magazin.de/finanzen/index.rss", "rss", "markt"),
    ("Süddeutsche Zeitung – Wirtschaft", "https://rss.sueddeutsche.de/rss/Wirtschaft", "rss", "markt"),
    ("Cash.online – Versicherungen", "https://www.cash-online.de/feed/", "rss", "markt"),
]

STARTER_KEYWORDS = [
    ("eigene_produkte", "unsere produkte"),
    ("eigene_produkte", "eigene versicherung"),
    ("wettbewerber", "allianz"),
    ("wettbewerber", "axa"),
    ("wettbewerber", "generali"),
    ("wettbewerber", "zurich"),
    ("wettbewerber", "munich re"),
    ("wettbewerber", "hannover rück"),
    ("wettbewerber", "talanx"),
    ("wettbewerber", "hdi"),
    ("wettbewerber", "ergo"),
    ("wettbewerber", "signal iduna"),
    ("wettbewerber", "debeka"),
    ("wettbewerber", "r+v"),
    ("markt", "versicherung"),
    ("markt", "gdv"),
    ("markt", "prämie"),
    ("markt", "versicherungsmarkt"),
    ("markt", "finanzaufsicht"),
    ("markt", "bafin"),
    ("markt", "lebensversicherung"),
    ("markt", "krankenversicherung"),
    ("markt", "sachversicherung"),
    ("markt", "haftpflicht"),
    ("markt", "insurtec"),
    ("markt", "insurtech"),
]


class _Conn:
    """Wraps a psycopg2 connection with a sqlite3-compatible execute() API."""

    def __init__(self, raw):
        self._raw = raw
        self._cur = raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        self._cur.execute(sql, params)
        return self._cur

    def executemany(self, sql, params_seq):
        for p in params_seq:
            self._cur.execute(sql, p)
        return self._cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass
        self._raw.close()


def normalize_article_url(url):
    """Return a stable URL for duplicate detection, ignoring common tracking noise."""
    url = (url or "").strip()
    if not url:
        return None

    try:
        parsed = urlsplit(url)
    except ValueError:
        return url

    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
        netloc = netloc.rsplit(":", 1)[0]

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_PARAMS:
            continue
        query_items.append((key, value))
    query = urlencode(sorted(query_items), doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def _normalize_fingerprint_part(value):
    value = (value or "").casefold()
    return " ".join(re.findall(r"\w+", value, flags=re.UNICODE))


def article_title_fingerprint(title):
    normalized_title = _normalize_fingerprint_part(title)
    return normalized_title or None


def article_duplicate_key(title, url=None, source_name=None, published_at=None):
    normalized_url = normalize_article_url(url)
    if normalized_url:
        return f"url:{normalized_url}"

    normalized_title = article_title_fingerprint(title)
    if not normalized_title:
        return None

    normalized_source = _normalize_fingerprint_part(source_name) or "unknown"
    published_day = (published_at or "")[:10]
    if published_day:
        return f"title:{normalized_source}:{published_day}:{normalized_title}"
    return f"title:{normalized_source}:{normalized_title}"


@contextmanager
def get_db():
    raw = psycopg2.connect(DATABASE_URL)
    conn = _Conn(raw)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        for stmt in [
            """CREATE TABLE IF NOT EXISTS sources (
                id              SERIAL PRIMARY KEY,
                name            TEXT NOT NULL,
                url             TEXT NOT NULL,
                type            TEXT NOT NULL CHECK(type IN ('rss','scraper','manual')),
                category_hint   TEXT NOT NULL DEFAULT 'sonstige',
                scraper_config  TEXT,
                is_active       INTEGER NOT NULL DEFAULT 1,
                last_fetched    TEXT,
                created_at      TEXT NOT NULL DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')
            )""",
            """CREATE TABLE IF NOT EXISTS articles (
                id              SERIAL PRIMARY KEY,
                title           TEXT NOT NULL,
                url             TEXT UNIQUE,
                source_id       INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                source_name     TEXT,
                content_snippet TEXT,
                category        TEXT NOT NULL DEFAULT 'sonstige',
                published_at    TEXT,
                fetched_at      TEXT NOT NULL DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'),
                is_read         INTEGER NOT NULL DEFAULT 0,
                ai_summary      TEXT,
                priority        TEXT,
                alerted         INTEGER NOT NULL DEFAULT 0,
                is_ignored      INTEGER NOT NULL DEFAULT 0,
                full_text       TEXT,
                is_pinned       INTEGER NOT NULL DEFAULT 0,
                ai_model        TEXT,
                geschaeftsfeld  TEXT,
                ai_implications TEXT,
                radar_sector    TEXT,
                normalized_url  TEXT,
                duplicate_key   TEXT,
                title_fingerprint TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS keywords (
                id       SERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                keyword  TEXT NOT NULL,
                UNIQUE(category, keyword)
            )""",
            """CREATE TABLE IF NOT EXISTS article_tags (
                article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                tag         TEXT NOT NULL,
                PRIMARY KEY (article_id, tag)
            )""",
            """CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )""",
            """CREATE TABLE IF NOT EXISTS alert_rules (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                keywords   TEXT NOT NULL,
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')
            )""",
            """CREATE TABLE IF NOT EXISTS reports (
                id            SERIAL PRIMARY KEY,
                date          TEXT NOT NULL UNIQUE,
                content       TEXT NOT NULL,
                article_count INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')
            )""",
            """CREATE TABLE IF NOT EXISTS radar_runs (
                id            SERIAL PRIMARY KEY,
                label         TEXT NOT NULL DEFAULT 'Trendradar',
                filters_json  TEXT NOT NULL DEFAULT '{}',
                sectors_json  TEXT NOT NULL DEFAULT '[]',
                model         TEXT,
                article_count INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')
            )""",
            """CREATE TABLE IF NOT EXISTS radar_topics (
                id            SERIAL PRIMARY KEY,
                run_id        INTEGER NOT NULL REFERENCES radar_runs(id) ON DELETE CASCADE,
                name          TEXT NOT NULL,
                sector        TEXT NOT NULL,
                horizon       TEXT NOT NULL,
                summary       TEXT,
                evidence      TEXT,
                confidence    INTEGER NOT NULL DEFAULT 70,
                article_ids   TEXT NOT NULL DEFAULT '[]',
                display_order INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS article_chunks (
                id              SERIAL PRIMARY KEY,
                article_id      INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                chunk_index     INTEGER NOT NULL,
                content         TEXT NOT NULL,
                content_hash    TEXT NOT NULL,
                embedding_json  TEXT,
                embedding_model TEXT,
                updated_at      TEXT NOT NULL DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'),
                UNIQUE(article_id, chunk_index)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_articles_category    ON articles(category)",
            "CREATE INDEX IF NOT EXISTS idx_articles_published   ON articles(published_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_articles_fetched     ON articles(fetched_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_article_tags_tag     ON article_tags(tag)",
            "CREATE INDEX IF NOT EXISTS idx_radar_runs_created   ON radar_runs(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_radar_topics_run     ON radar_topics(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_chunks_article ON article_chunks(article_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_chunks_hash    ON article_chunks(content_hash)",
        ]:
            conn.execute(stmt)

        # Migrations for existing DBs (no-op on fresh schema, safe via savepoints)
        for col_name, col_def in [
            ("ai_summary", "TEXT"),
            ("priority", "TEXT"),
            ("alerted", "INTEGER NOT NULL DEFAULT 0"),
            ("is_ignored", "INTEGER NOT NULL DEFAULT 0"),
            ("full_text", "TEXT"),
            ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
            ("ai_model", "TEXT"),
            ("geschaeftsfeld", "TEXT"),
            ("ai_implications", "TEXT"),
            ("radar_sector", "TEXT"),
            ("normalized_url", "TEXT"),
            ("duplicate_key", "TEXT"),
            ("title_fingerprint", "TEXT"),
        ]:
            conn.execute("SAVEPOINT add_col")
            try:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col_name} {col_def}")
                conn.execute("RELEASE SAVEPOINT add_col")
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT add_col")

        rows = conn.execute(
            "SELECT id, title, url, source_name, published_at "
            "FROM articles WHERE normalized_url IS NULL "
            "OR duplicate_key IS NULL OR title_fingerprint IS NULL"
        ).fetchall()
        for row in rows:
            normalized_url = normalize_article_url(row["url"])
            title_fingerprint = article_title_fingerprint(row["title"])
            duplicate_key = article_duplicate_key(
                row["title"],
                row["url"],
                row["source_name"],
                row["published_at"],
            )
            conn.execute(
                "UPDATE articles SET normalized_url=%s, duplicate_key=%s, "
                "title_fingerprint=%s WHERE id=%s",
                (normalized_url, duplicate_key, title_fingerprint, row["id"]),
            )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_normalized_url "
            "ON articles(normalized_url)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_duplicate_key "
            "ON articles(duplicate_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_title_fingerprint "
            "ON articles(title_fingerprint)"
        )

        conn.execute("SAVEPOINT duplicate_key_unique_idx")
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_duplicate_key_unique "
                "ON articles(duplicate_key) WHERE duplicate_key IS NOT NULL"
            )
            conn.execute("RELEASE SAVEPOINT duplicate_key_unique_idx")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT duplicate_key_unique_idx")

        # Seed starter sources idempotently
        for source in STARTER_SOURCES:
            exists = conn.execute(
                "SELECT 1 FROM sources WHERE url = %s LIMIT 1",
                (source[1],),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO sources (name, url, type, category_hint) VALUES (%s,%s,%s,%s)",
                    source,
                )

        # Seed keywords if table is empty
        kw_count = conn.execute("SELECT COUNT(*) AS n FROM keywords").fetchone()["n"]
        if kw_count == 0:
            conn.executemany(
                "INSERT INTO keywords (category, keyword) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                STARTER_KEYWORDS,
            )


# --- Article helpers ---

_TAGS_SUBQUERY = """
    LEFT JOIN (
        SELECT article_id, STRING_AGG(tag, ',') AS tags
        FROM article_tags
        GROUP BY article_id
    ) t ON t.article_id = a.id
"""

_ARTICLE_SELECT = """
    SELECT a.*, s.url AS source_url,
           COALESCE(s.url, a.url) AS source_logo_ref,
           t.tags
    FROM articles a
    LEFT JOIN sources s ON a.source_id = s.id
""" + _TAGS_SUBQUERY


def get_articles(category=None, search=None, von=None, bis=None, tag=None, source_id=None,
                 priority=None, alerted_only=False, include_ignored=False, limit=200):
    sql = _ARTICLE_SELECT + " WHERE 1=1"
    params = []
    if category and category != "alle":
        sql += " AND a.category = %s"
        params.append(category)
    if search:
        sql += " AND (a.title LIKE %s OR a.content_snippet LIKE %s)"
        params += [f"%{search}%", f"%{search}%"]
    if von:
        sql += " AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) >= %s"
        params.append(von)
    if bis:
        sql += " AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) <= %s"
        params.append(bis)
    if tag:
        sql += " AND a.id IN (SELECT article_id FROM article_tags WHERE tag = %s)"
        params.append(tag)
    if source_id:
        sql += " AND a.source_id = %s"
        params.append(source_id)
    if priority and priority != "alle":
        sql += " AND a.priority = %s"
        params.append(priority)
    if alerted_only:
        sql += " AND a.alerted = 1"
    if not include_ignored:
        sql += " AND COALESCE(a.is_ignored, 0) = 0"
    sql += " ORDER BY COALESCE(a.published_at, a.fetched_at) DESC LIMIT %s"
    params.append(limit)
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


def get_article(article_id):
    sql = _ARTICLE_SELECT + " WHERE a.id = %s"
    with get_db() as conn:
        return conn.execute(sql, (article_id,)).fetchone()


def get_all_tags():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT tag, COUNT(*) as n FROM article_tags GROUP BY tag ORDER BY tag"
        ).fetchall()
    return [{"tag": r["tag"], "count": r["n"]} for r in rows]


def mark_read(article_id):
    with get_db() as conn:
        conn.execute("UPDATE articles SET is_read = 1 WHERE id = %s", (article_id,))


def mark_articles_read(article_ids):
    ids = [int(article_id) for article_id in article_ids if str(article_id).isdigit()]
    if not ids:
        return 0
    with get_db() as conn:
        result = conn.execute("UPDATE articles SET is_read = 1 WHERE id = ANY(%s)", (ids,))
        return result.rowcount


def set_article_ignored(article_id, ignored=True):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET is_ignored = %s WHERE id = %s",
            (1 if ignored else 0, article_id),
        )


def set_articles_ignored(article_ids, ignored=True):
    ids = [int(article_id) for article_id in article_ids if str(article_id).isdigit()]
    if not ids:
        return 0
    with get_db() as conn:
        result = conn.execute(
            "UPDATE articles SET is_ignored = %s WHERE id = ANY(%s)",
            (1 if ignored else 0, ids),
        )
        return result.rowcount


def _insert_tags(conn, article_id, tags):
    for tag in tags:
        tag = tag.strip().lower()
        if tag:
            conn.execute(
                "INSERT INTO article_tags (article_id, tag) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (article_id, tag),
            )


def _find_duplicate_article(conn, title, url=None, source_name=None, published_at=None):
    normalized_url = normalize_article_url(url)
    duplicate_key = article_duplicate_key(title, url, source_name, published_at)
    title_fingerprint = article_title_fingerprint(title)

    if normalized_url:
        row = conn.execute(
            "SELECT * FROM articles WHERE normalized_url = %s OR url = %s "
            "ORDER BY is_pinned DESC, id ASC LIMIT 1",
            (normalized_url, url),
        ).fetchone()
        if row:
            return row

    if duplicate_key and not normalized_url:
        return conn.execute(
            "SELECT * FROM articles WHERE duplicate_key = %s "
            "ORDER BY is_pinned DESC, id ASC LIMIT 1",
            (duplicate_key,),
        ).fetchone()

    if title_fingerprint:
        return conn.execute(
            "SELECT * FROM articles WHERE title_fingerprint = %s "
            "ORDER BY is_pinned DESC, id ASC LIMIT 1",
            (title_fingerprint,),
        ).fetchone()

    return None


def find_duplicate_article(title, url=None, source_name=None, published_at=None):
    with get_db() as conn:
        return _find_duplicate_article(conn, title, url, source_name, published_at)


def _merge_duplicate_metadata(conn, article_id, source_id=None, source_name=None, published_at=None):
    conn.execute(
        """UPDATE articles
           SET source_id = COALESCE(source_id, %s),
               source_name = COALESCE(NULLIF(source_name, ''), %s),
               published_at = COALESCE(published_at, %s)
           WHERE id = %s""",
        (source_id, source_name, published_at, article_id),
    )


def add_article(title, url, source_name, content_snippet, category, published_at,
                tags=None, source_id=None, return_status=False):
    article_id = None
    created = False
    normalized_url = normalize_article_url(url)
    duplicate_key = article_duplicate_key(title, url, source_name, published_at)
    title_fingerprint = article_title_fingerprint(title)

    with get_db() as conn:
        duplicate = _find_duplicate_article(conn, title, url, source_name, published_at)
        if duplicate:
            article_id = duplicate["id"]
            _merge_duplicate_metadata(conn, article_id, source_id, source_name, published_at)
            return (article_id, False) if return_status else article_id

        cur = conn.execute(
            """INSERT INTO articles
               (title, url, source_id, source_name, content_snippet, category, published_at,
                normalized_url, duplicate_key, title_fingerprint)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (
                title,
                url or None,
                source_id,
                source_name,
                content_snippet,
                category,
                published_at,
                normalized_url,
                duplicate_key,
                title_fingerprint,
            ),
        )
        row = cur.fetchone()
        article_id = row["id"] if row else None
        if article_id is None:
            duplicate = _find_duplicate_article(conn, title, url, source_name, published_at)
            if duplicate:
                article_id = duplicate["id"]
                _merge_duplicate_metadata(conn, article_id, source_id, source_name, published_at)
        else:
            created = True
        if article_id and tags:
            _insert_tags(conn, article_id, tags)
    if return_status:
        return article_id, created
    return article_id


def get_pinned_duplicate_article(article_id):
    with get_db() as conn:
        target = conn.execute(
            "SELECT normalized_url, duplicate_key, title_fingerprint "
            "FROM articles WHERE id = %s",
            (article_id,),
        ).fetchone()
        if not target:
            return None

        normalized_url = target["normalized_url"]
        duplicate_key = target["duplicate_key"]
        title_fingerprint = target["title_fingerprint"]
        if not normalized_url and not duplicate_key and not title_fingerprint:
            return None

        return conn.execute(
            """SELECT id, title, url, source_name
               FROM articles
               WHERE id != %s
                 AND is_pinned = 1
                 AND (
                   (%s IS NOT NULL AND normalized_url = %s)
                   OR (%s IS NOT NULL AND duplicate_key = %s)
                   OR (%s IS NOT NULL AND title_fingerprint = %s)
                 )
               ORDER BY id ASC
               LIMIT 1""",
            (
                article_id,
                normalized_url,
                normalized_url,
                duplicate_key,
                duplicate_key,
                title_fingerprint,
                title_fingerprint,
            ),
        ).fetchone()


def set_article_tags(article_id, tags):
    with get_db() as conn:
        conn.execute("DELETE FROM article_tags WHERE article_id = %s", (article_id,))
        _insert_tags(conn, article_id, tags)


def count_unread():
    with get_db() as conn:
        row = conn.execute(
            "SELECT category, COUNT(*) as n FROM articles "
            "WHERE is_read=0 AND COALESCE(is_ignored, 0)=0 GROUP BY category"
        ).fetchall()
    return {r["category"]: r["n"] for r in row}


def count_ignored():
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM articles WHERE COALESCE(is_ignored, 0)=1"
        ).fetchone()
    return row["n"] if row else 0


def delete_article(article_id):
    with get_db() as conn:
        conn.execute("DELETE FROM articles WHERE id = %s", (article_id,))


# --- Source helpers ---

def get_sources(active_only=False):
    sql = "SELECT * FROM sources"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY name"
    with get_db() as conn:
        return conn.execute(sql).fetchall()


def get_source(source_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM sources WHERE id = %s", (source_id,)).fetchone()


def add_source(name, url, src_type, category_hint, scraper_config=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sources (name, url, type, category_hint, scraper_config) VALUES (%s,%s,%s,%s,%s)",
            (name, url, src_type, category_hint, scraper_config),
        )


def delete_source(source_id):
    with get_db() as conn:
        conn.execute("DELETE FROM sources WHERE id = %s", (source_id,))


def update_last_fetched(source_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE sources SET last_fetched = to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = %s",
            (source_id,),
        )


def toggle_source(source_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE sources SET is_active = 1 - is_active WHERE id = %s", (source_id,)
        )


# --- Keyword helpers ---

def get_keywords():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM keywords ORDER BY category, keyword").fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["category"], []).append({"id": r["id"], "keyword": r["keyword"]})
    return result


def add_keyword(category, keyword):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO keywords (category, keyword) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (category, keyword.lower().strip()),
        )


def delete_keyword(keyword_id):
    with get_db() as conn:
        conn.execute("DELETE FROM keywords WHERE id = %s", (keyword_id,))


# --- AI helpers ---

def update_article_ai(article_id, summary, category, priority=None, model_used=None,
                       geschaeftsfeld=None, implications=None, radar_sector=None):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET ai_summary=%s, category=%s, priority=%s, ai_model=%s, "
            "geschaeftsfeld=%s, ai_implications=%s, radar_sector=%s WHERE id=%s",
            (
                summary,
                category,
                priority,
                model_used,
                geschaeftsfeld,
                implications,
                radar_sector or None,
                article_id,
            ),
        )


def clear_article_radar_sectors():
    with get_db() as conn:
        result = conn.execute("UPDATE articles SET radar_sector = NULL WHERE radar_sector IS NOT NULL")
        return result.rowcount


def update_article_manual_fields(article_id, ai_summary=None, ai_implications=None,
                                   geschaeftsfeld=None, category=None, radar_sector=None,
                                   update_radar_sector=False):
    with get_db() as conn:
        if category and update_radar_sector:
            conn.execute(
                "UPDATE articles SET ai_summary=%s, ai_implications=%s, "
                "geschaeftsfeld=%s, category=%s, radar_sector=%s WHERE id=%s",
                (ai_summary, ai_implications, geschaeftsfeld, category, radar_sector, article_id),
            )
        elif category:
            conn.execute(
                "UPDATE articles SET ai_summary=%s, ai_implications=%s, "
                "geschaeftsfeld=%s, category=%s WHERE id=%s",
                (ai_summary, ai_implications, geschaeftsfeld, category, article_id),
            )
        elif update_radar_sector:
            conn.execute(
                "UPDATE articles SET ai_summary=%s, ai_implications=%s, "
                "geschaeftsfeld=%s, radar_sector=%s WHERE id=%s",
                (ai_summary, ai_implications, geschaeftsfeld, radar_sector, article_id),
            )
        else:
            conn.execute(
                "UPDATE articles SET ai_summary=%s, ai_implications=%s, "
                "geschaeftsfeld=%s WHERE id=%s",
                (ai_summary, ai_implications, geschaeftsfeld, article_id),
            )


def toggle_pin(article_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET is_pinned = 1 - is_pinned WHERE id = %s", (article_id,)
        )


def set_article_pinned(article_id, pinned=True):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET is_pinned = %s WHERE id = %s",
            (1 if pinned else 0, article_id),
        )


def get_pinned_articles(search=None, tag=None, von=None, bis=None,
                         source_id=None, geschaeftsfeld=None):
    sql = """
        SELECT a.*, s.url AS source_url,
               COALESCE(s.url, a.url) AS source_logo_ref,
               t.tags
        FROM articles a
        LEFT JOIN sources s ON a.source_id = s.id
        LEFT JOIN (
            SELECT article_id, STRING_AGG(tag, ',') AS tags
            FROM article_tags GROUP BY article_id
        ) t ON t.article_id = a.id
        WHERE a.is_pinned = 1
    """
    params = []
    if search:
        sql += " AND (a.title ILIKE %s OR a.ai_summary ILIKE %s)"
        params += [f"%{search}%", f"%{search}%"]
    if tag:
        sql += " AND a.id IN (SELECT article_id FROM article_tags WHERE tag ILIKE %s)"
        params.append(f"%{tag}%")
    if von:
        sql += " AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) >= %s"
        params.append(von)
    if bis:
        sql += " AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) <= %s"
        params.append(bis)
    if source_id:
        sql += " AND a.source_id = %s"
        params.append(source_id)
    if geschaeftsfeld:
        sql += " AND a.geschaeftsfeld = %s"
        params.append(geschaeftsfeld)
    sql += " ORDER BY COALESCE(a.published_at, a.fetched_at) DESC"
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


def get_pinned_articles_for_report():
    sql = """
        SELECT a.*, t.tags
        FROM articles a
        LEFT JOIN (
            SELECT article_id, STRING_AGG(tag, ',') AS tags
            FROM article_tags GROUP BY article_id
        ) t ON t.article_id = a.id
        WHERE a.is_pinned = 1
        ORDER BY a.category, COALESCE(a.published_at, a.fetched_at) DESC
    """
    with get_db() as conn:
        return conn.execute(sql).fetchall()


def delete_old_unpinned_articles(days=30):
    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM articles WHERE is_pinned = 0 "
            "AND COALESCE(published_at, fetched_at) < "
            "to_char(NOW() - INTERVAL '1 day' * %s, 'YYYY-MM-DD HH24:MI:SS')",
            (days,),
        )
        return result.rowcount


# --- Alert helpers ---

def check_and_alert(conn, article_id, title, snippet):
    rules = conn.execute(
        "SELECT keywords FROM alert_rules WHERE is_active = 1"
    ).fetchall()
    if not rules:
        return
    text = (title + " " + (snippet or "")).lower()
    for rule in rules:
        keywords = [k.strip().lower() for k in rule["keywords"].split(",") if k.strip()]
        if any(kw in text for kw in keywords):
            conn.execute("UPDATE articles SET alerted = 1 WHERE id = %s", (article_id,))
            return


def get_alert_rules():
    with get_db() as conn:
        return conn.execute("SELECT * FROM alert_rules ORDER BY name").fetchall()


def add_alert_rule(name, keywords):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO alert_rules (name, keywords) VALUES (%s,%s)",
            (name, keywords),
        )


def delete_alert_rule(rule_id):
    with get_db() as conn:
        conn.execute("DELETE FROM alert_rules WHERE id = %s", (rule_id,))


def toggle_alert_rule(rule_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE alert_rules SET is_active = 1 - is_active WHERE id = %s", (rule_id,)
        )


def recheck_all_alerts():
    with get_db() as conn:
        conn.execute("UPDATE articles SET alerted = 0")
        rules = conn.execute(
            "SELECT keywords FROM alert_rules WHERE is_active = 1"
        ).fetchall()
        if not rules:
            return
        articles = conn.execute("SELECT id, title, content_snippet FROM articles").fetchall()
        for a in articles:
            text = (a["title"] + " " + (a["content_snippet"] or "")).lower()
            for rule in rules:
                keywords = [k.strip().lower() for k in rule["keywords"].split(",") if k.strip()]
                if any(kw in text for kw in keywords):
                    conn.execute("UPDATE articles SET alerted = 1 WHERE id = %s", (a["id"],))
                    break


# --- Settings helpers ---

def get_setting(key, default=""):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (%s,%s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


# --- Report helpers ---

def save_report(date, content_json, article_count):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reports (date, content, article_count) VALUES (%s,%s,%s) "
            "ON CONFLICT (date) DO UPDATE SET content=EXCLUDED.content, "
            "article_count=EXCLUDED.article_count, "
            "created_at=to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')",
            (date, content_json, article_count),
        )


def get_report(date):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM reports WHERE date = %s", (date,)
        ).fetchone()


def get_recent_reports(limit=10):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM reports ORDER BY date DESC LIMIT %s", (limit,)
        ).fetchall()


def get_articles_for_report(date):
    sql = """
        SELECT a.*, t.tags
        FROM articles a
        LEFT JOIN (
            SELECT article_id, STRING_AGG(tag, ',') AS tags
            FROM article_tags GROUP BY article_id
        ) t ON t.article_id = a.id
        WHERE a.is_pinned = 1
          AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) = %s
        ORDER BY a.category, COALESCE(a.published_at, a.fetched_at) DESC
    """
    with get_db() as conn:
        return conn.execute(sql, (date,)).fetchall()


# --- Trendradar analytics ---

def get_category_trend(weeks=12):
    """Weekly article count per category for the last n weeks."""
    sql = """
        SELECT
            DATE_TRUNC('week',
                SUBSTRING(COALESCE(published_at, fetched_at), 1, 10)::date
            )::date AS week_monday,
            category,
            COUNT(*) AS count
        FROM articles
        WHERE SUBSTRING(COALESCE(published_at, fetched_at), 1, 10) != ''
          AND SUBSTRING(COALESCE(published_at, fetched_at), 1, 10)::date
              >= CURRENT_DATE - (%s * INTERVAL '1 week')
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    with get_db() as conn:
        return conn.execute(sql, (weeks,)).fetchall()


def get_alert_trend(weeks=12):
    """Weekly alerted-article count for the last n weeks."""
    sql = """
        SELECT
            DATE_TRUNC('week',
                SUBSTRING(COALESCE(published_at, fetched_at), 1, 10)::date
            )::date AS week_monday,
            COUNT(*) AS count
        FROM articles
        WHERE alerted = 1
          AND SUBSTRING(COALESCE(published_at, fetched_at), 1, 10) != ''
          AND SUBSTRING(COALESCE(published_at, fetched_at), 1, 10)::date
              >= CURRENT_DATE - (%s * INTERVAL '1 week')
        GROUP BY 1
        ORDER BY 1
    """
    with get_db() as conn:
        return conn.execute(sql, (weeks,)).fetchall()


def get_top_tags(weeks=12, limit=12):
    """Most common tags in pinned articles over the last n weeks."""
    sql = """
        SELECT at.tag, COUNT(*) AS count
        FROM article_tags at
        JOIN articles a ON at.article_id = a.id
        WHERE a.is_pinned = 1
          AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) != ''
          AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10)::date
              >= CURRENT_DATE - (%s * INTERVAL '1 week')
        GROUP BY at.tag
        ORDER BY count DESC
        LIMIT %s
    """
    with get_db() as conn:
        return conn.execute(sql, (weeks, limit)).fetchall()


def get_source_stats(weeks=12, limit=12):
    """Article count and pin count per source for the last n weeks."""
    sql = """
        SELECT
            COALESCE(source_name, '(unbekannt)') AS source_name,
            COUNT(*)        AS total,
            SUM(is_pinned)  AS pinned
        FROM articles
        WHERE SUBSTRING(COALESCE(published_at, fetched_at), 1, 10) != ''
          AND SUBSTRING(COALESCE(published_at, fetched_at), 1, 10)::date
              >= CURRENT_DATE - (%s * INTERVAL '1 week')
        GROUP BY 1
        ORDER BY total DESC
        LIMIT %s
    """
    with get_db() as conn:
        return conn.execute(sql, (weeks, limit)).fetchall()


def get_trend_stats(weeks=12):
    """Summary counts (total, pinned, alerted, analysed) for the last n weeks."""
    sql = """
        SELECT
            COUNT(*)                                                    AS total,
            SUM(is_pinned)                                              AS pinned,
            SUM(alerted)                                                AS alerted,
            SUM(CASE WHEN ai_summary IS NOT NULL
                          AND ai_summary != '' THEN 1 ELSE 0 END)      AS analysed
        FROM articles
        WHERE SUBSTRING(COALESCE(published_at, fetched_at), 1, 10) != ''
          AND SUBSTRING(COALESCE(published_at, fetched_at), 1, 10)::date
              >= CURRENT_DATE - (%s * INTERVAL '1 week')
    """
    with get_db() as conn:
        return conn.execute(sql, (weeks,)).fetchone()


def get_articles_for_week_report(end_date):
    """Return all articles from the 7 days up to and including end_date."""
    from datetime import date as date_type, timedelta
    end = date_type.fromisoformat(end_date)
    start = (end - timedelta(days=6)).isoformat()
    sql = """
        SELECT a.*, t.tags
        FROM articles a
        LEFT JOIN (
            SELECT article_id, STRING_AGG(tag, ',') AS tags
            FROM article_tags GROUP BY article_id
        ) t ON t.article_id = a.id
        WHERE a.is_pinned = 1
          AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) BETWEEN %s AND %s
        ORDER BY a.category, COALESCE(a.published_at, a.fetched_at) DESC
    """
    with get_db() as conn:
        return conn.execute(sql, (start, end_date)).fetchall()


# --- AI Trendradar helpers ---

def _radar_filters_json(category=None, geschaeftsfeld=None, days=None):
    filters = {
        "category": category or "",
        "geschaeftsfeld": geschaeftsfeld or "",
        "days": int(days) if days else 0,
    }
    return json.dumps(filters, ensure_ascii=False, sort_keys=True)


def get_pinned_articles_for_radar(category=None, geschaeftsfeld=None, days=None):
    sql = """
        SELECT a.*, t.tags
        FROM articles a
        LEFT JOIN (
            SELECT article_id, STRING_AGG(tag, ',') AS tags
            FROM article_tags GROUP BY article_id
        ) t ON t.article_id = a.id
        WHERE a.is_pinned = 1
    """
    params = []
    if category and category != "alle":
        sql += " AND a.category = %s"
        params.append(category)
    if geschaeftsfeld:
        sql += " AND a.geschaeftsfeld = %s"
        params.append(geschaeftsfeld)
    if days:
        sql += """
          AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) != ''
          AND SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10)::date
              >= CURRENT_DATE - (%s * INTERVAL '1 day')
        """
        params.append(int(days))
    sql += " ORDER BY COALESCE(a.published_at, a.fetched_at) DESC, a.id DESC"
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


def save_radar_run(result, filters, article_count, model_used=None):
    filters_json = json.dumps(filters, ensure_ascii=False, sort_keys=True)
    sectors_json = json.dumps(result.get("sectors", []), ensure_ascii=False)
    topics = result.get("topics", [])
    with get_db() as conn:
        row = conn.execute(
            """INSERT INTO radar_runs (label, filters_json, sectors_json, model, article_count)
               VALUES (%s,%s,%s,%s,%s)
               RETURNING id""",
            (
                result.get("title") or "Trendradar",
                filters_json,
                sectors_json,
                model_used or result.get("model_used"),
                article_count,
            ),
        ).fetchone()
        run_id = row["id"]
        for i, topic in enumerate(topics):
            conn.execute(
                """INSERT INTO radar_topics
                   (run_id, name, sector, horizon, summary, evidence, confidence, article_ids, display_order)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    run_id,
                    topic.get("name") or "Unbenanntes Thema",
                    topic.get("sector") or "Sonstiges",
                    topic.get("horizon") or "Monitor",
                    topic.get("summary") or "",
                    topic.get("evidence") or "",
                    int(topic.get("confidence") or 70),
                    json.dumps(topic.get("article_ids", []), ensure_ascii=False),
                    i,
                ),
            )
    return run_id


def get_latest_radar_run(category=None, geschaeftsfeld=None, days=None):
    filters_json = _radar_filters_json(category, geschaeftsfeld, days)
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM radar_runs WHERE filters_json = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (filters_json,),
        ).fetchone()


def get_radar_run(run_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM radar_runs WHERE id = %s", (run_id,)).fetchone()


def get_recent_radar_runs(limit=8):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM radar_runs ORDER BY created_at DESC, id DESC LIMIT %s",
            (limit,),
        ).fetchall()


def get_radar_topics(run_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM radar_topics WHERE run_id = %s ORDER BY display_order, id",
            (run_id,),
        ).fetchall()


def update_radar_topic(topic_id, sector, horizon):
    with get_db() as conn:
        return conn.execute(
            """UPDATE radar_topics
               SET sector = %s, horizon = %s
               WHERE id = %s
               RETURNING id, run_id, name, sector, horizon""",
            (sector, horizon, topic_id),
        ).fetchone()


def delete_radar_run(run_id):
    with get_db() as conn:
        conn.execute("DELETE FROM radar_runs WHERE id = %s", (run_id,))


def get_articles_by_ids(article_ids):
    ids = [int(article_id) for article_id in article_ids if str(article_id).isdigit()]
    if not ids:
        return []
    sql = _ARTICLE_SELECT + " WHERE a.id = ANY(%s)"
    with get_db() as conn:
        rows = conn.execute(sql, (ids,)).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    return [by_id[i] for i in ids if i in by_id]


# --- Pinned article assistant / retrieval helpers ---

def get_pinned_articles_for_assistant():
    sql = """
        SELECT a.*, t.tags
        FROM articles a
        LEFT JOIN (
            SELECT article_id, STRING_AGG(tag, ',') AS tags
            FROM article_tags GROUP BY article_id
        ) t ON t.article_id = a.id
        WHERE a.is_pinned = 1
        ORDER BY COALESCE(a.published_at, a.fetched_at) DESC, a.id DESC
    """
    with get_db() as conn:
        return conn.execute(sql).fetchall()


def get_article_chunks_for_pinned():
    sql = """
        SELECT
            c.*,
            a.title,
            a.url,
            a.source_name,
            a.published_at,
            a.fetched_at,
            a.category,
            a.geschaeftsfeld,
            a.ai_summary,
            a.ai_implications,
            t.tags
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id
        LEFT JOIN (
            SELECT article_id, STRING_AGG(tag, ',') AS tags
            FROM article_tags GROUP BY article_id
        ) t ON t.article_id = a.id
        WHERE a.is_pinned = 1
        ORDER BY c.article_id, c.chunk_index
    """
    with get_db() as conn:
        return conn.execute(sql).fetchall()


def replace_article_chunks(article_id, chunks):
    """Replace retrieval chunks for one article.

    chunks is a list of dicts containing content, content_hash,
    embedding_json and embedding_model.
    """
    with get_db() as conn:
        conn.execute("DELETE FROM article_chunks WHERE article_id = %s", (article_id,))
        for idx, chunk in enumerate(chunks):
            conn.execute(
                """INSERT INTO article_chunks
                   (article_id, chunk_index, content, content_hash, embedding_json, embedding_model)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    article_id,
                    idx,
                    chunk["content"],
                    chunk["content_hash"],
                    chunk.get("embedding_json"),
                    chunk.get("embedding_model"),
                ),
            )


def delete_article_chunks(article_id):
    with get_db() as conn:
        conn.execute("DELETE FROM article_chunks WHERE article_id = %s", (article_id,))
