import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Sentinel stored in ai_summary when pinning was attempted but no fulltext
# could be fetched. Checked in templates and in the daily report builder.
NO_FULLTEXT = "__kein_volltext__"

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
                full_text       TEXT,
                is_pinned       INTEGER NOT NULL DEFAULT 0,
                ai_model        TEXT,
                geschaeftsfeld  TEXT,
                ai_implications TEXT
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
            "CREATE INDEX IF NOT EXISTS idx_articles_category    ON articles(category)",
            "CREATE INDEX IF NOT EXISTS idx_articles_published   ON articles(published_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_articles_fetched     ON articles(fetched_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_article_tags_tag     ON article_tags(tag)",
        ]:
            conn.execute(stmt)

        # Migrations for existing DBs (no-op on fresh schema, safe via savepoints)
        for col_name, col_def in [
            ("ai_summary", "TEXT"),
            ("priority", "TEXT"),
            ("alerted", "INTEGER NOT NULL DEFAULT 0"),
            ("full_text", "TEXT"),
            ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
            ("ai_model", "TEXT"),
            ("geschaeftsfeld", "TEXT"),
            ("ai_implications", "TEXT"),
        ]:
            conn.execute("SAVEPOINT add_col")
            try:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col_name} {col_def}")
                conn.execute("RELEASE SAVEPOINT add_col")
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT add_col")

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
                 priority=None, alerted_only=False, limit=200):
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


def _insert_tags(conn, article_id, tags):
    for tag in tags:
        tag = tag.strip().lower()
        if tag:
            conn.execute(
                "INSERT INTO article_tags (article_id, tag) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (article_id, tag),
            )


def add_article(title, url, source_name, content_snippet, category, published_at, tags=None):
    article_id = None
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO articles
               (title, url, source_name, content_snippet, category, published_at)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (url) DO NOTHING
               RETURNING id""",
            (title, url or None, source_name, content_snippet, category, published_at),
        )
        row = cur.fetchone()
        article_id = row["id"] if row else None
        if article_id is None and url:
            row = conn.execute("SELECT id FROM articles WHERE url = %s", (url,)).fetchone()
            if row:
                article_id = row["id"]
        if article_id and tags:
            _insert_tags(conn, article_id, tags)
    return article_id


def set_article_tags(article_id, tags):
    with get_db() as conn:
        conn.execute("DELETE FROM article_tags WHERE article_id = %s", (article_id,))
        _insert_tags(conn, article_id, tags)


def count_unread():
    with get_db() as conn:
        row = conn.execute(
            "SELECT category, COUNT(*) as n FROM articles WHERE is_read=0 GROUP BY category"
        ).fetchall()
    return {r["category"]: r["n"] for r in row}


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
                       geschaeftsfeld=None, implications=None):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET ai_summary=%s, category=%s, priority=%s, ai_model=%s, "
            "geschaeftsfeld=%s, ai_implications=%s WHERE id=%s",
            (summary, category, priority, model_used, geschaeftsfeld, implications, article_id),
        )


def update_article_manual_fields(article_id, ai_summary=None, ai_implications=None,
                                   geschaeftsfeld=None):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET ai_summary=%s, ai_implications=%s, geschaeftsfeld=%s WHERE id=%s",
            (ai_summary, ai_implications, geschaeftsfeld, article_id),
        )


def toggle_pin(article_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET is_pinned = 1 - is_pinned WHERE id = %s", (article_id,)
        )


def get_pinned_articles():
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
        ORDER BY COALESCE(a.published_at, a.fetched_at) DESC
    """
    with get_db() as conn:
        return conn.execute(sql).fetchall()


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
        WHERE SUBSTRING(COALESCE(a.published_at, a.fetched_at), 1, 10) = %s
        ORDER BY a.category, COALESCE(a.published_at, a.fetched_at) DESC
    """
    with get_db() as conn:
        return conn.execute(sql, (date,)).fetchall()
