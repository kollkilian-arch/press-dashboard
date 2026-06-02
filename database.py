import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "news.db")

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


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                url             TEXT NOT NULL,
                type            TEXT NOT NULL CHECK(type IN ('rss','scraper','manual')),
                category_hint   TEXT NOT NULL DEFAULT 'sonstige',
                scraper_config  TEXT,
                is_active       INTEGER NOT NULL DEFAULT 1,
                last_fetched    TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS articles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                url             TEXT UNIQUE,
                source_id       INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                source_name     TEXT,
                content_snippet TEXT,
                category        TEXT NOT NULL DEFAULT 'sonstige',
                published_at    TEXT,
                fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
                is_read         INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS keywords (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                keyword  TEXT NOT NULL,
                UNIQUE(category, keyword)
            );

            CREATE TABLE IF NOT EXISTS article_tags (
                article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                tag         TEXT NOT NULL,
                PRIMARY KEY (article_id, tag)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS alert_rules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                keywords   TEXT NOT NULL,
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                date          TEXT NOT NULL UNIQUE,
                content       TEXT NOT NULL,
                article_count INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_articles_category    ON articles(category);
            CREATE INDEX IF NOT EXISTS idx_articles_published   ON articles(published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_fetched     ON articles(fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_article_tags_tag     ON article_tags(tag);
        """)
        # Migrations for existing DBs
        for col_sql in [
            "ALTER TABLE articles ADD COLUMN ai_summary TEXT",
            "ALTER TABLE articles ADD COLUMN priority TEXT",
            "ALTER TABLE articles ADD COLUMN alerted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN full_text TEXT",
            "ALTER TABLE articles ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass

        # Seed starter sources idempotently so new defaults appear in existing DBs.
        for source in STARTER_SOURCES:
            exists = conn.execute(
                "SELECT 1 FROM sources WHERE url = ? LIMIT 1",
                (source[1],),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO sources (name, url, type, category_hint) VALUES (?,?,?,?)",
                    source,
                )

        # Seed keywords if table is empty
        kw_count = conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
        if kw_count == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO keywords (category, keyword) VALUES (?,?)",
                STARTER_KEYWORDS,
            )


# --- Article helpers ---

def get_articles(category=None, search=None, von=None, bis=None, tag=None, source_id=None,
                 priority=None, alerted_only=False, limit=200):
    sql = """
        SELECT a.*, s.url AS source_url,
               COALESCE(s.url, a.url) AS source_logo_ref,
               GROUP_CONCAT(at.tag, ',') AS tags
        FROM articles a
        LEFT JOIN sources s ON a.source_id = s.id
        LEFT JOIN article_tags at ON a.id = at.article_id
        WHERE 1=1
    """
    params = []
    if category and category != "alle":
        sql += " AND a.category = ?"
        params.append(category)
    if search:
        sql += " AND (a.title LIKE ? OR a.content_snippet LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    if von:
        sql += " AND date(COALESCE(a.published_at, a.fetched_at)) >= date(?)"
        params.append(von)
    if bis:
        sql += " AND date(COALESCE(a.published_at, a.fetched_at)) <= date(?)"
        params.append(bis)
    if tag:
        sql += " AND a.id IN (SELECT article_id FROM article_tags WHERE tag = ?)"
        params.append(tag)
    if source_id:
        sql += " AND a.source_id = ?"
        params.append(source_id)
    if priority and priority != "alle":
        sql += " AND a.priority = ?"
        params.append(priority)
    if alerted_only:
        sql += " AND a.alerted = 1"
    sql += " GROUP BY a.id ORDER BY COALESCE(a.published_at, a.fetched_at) DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


def get_article(article_id):
    sql = """
        SELECT a.*, s.url AS source_url,
               COALESCE(s.url, a.url) AS source_logo_ref,
               GROUP_CONCAT(at.tag, ',') AS tags
        FROM articles a
        LEFT JOIN sources s ON a.source_id = s.id
        LEFT JOIN article_tags at ON a.id = at.article_id
        WHERE a.id = ?
        GROUP BY a.id
    """
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
        conn.execute("UPDATE articles SET is_read = 1 WHERE id = ?", (article_id,))


def _insert_tags(conn, article_id, tags):
    for tag in tags:
        tag = tag.strip().lower()
        if tag:
            conn.execute(
                "INSERT OR IGNORE INTO article_tags (article_id, tag) VALUES (?,?)",
                (article_id, tag),
            )


def add_article(title, url, source_name, content_snippet, category, published_at, tags=None):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO articles
               (title, url, source_name, content_snippet, category, published_at)
               VALUES (?,?,?,?,?,?)""",
            (title, url or None, source_name, content_snippet, category, published_at),
        )
        article_id = cur.lastrowid if cur.rowcount > 0 else None
        if article_id is None and url:
            row = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
            if row:
                article_id = row["id"]
        if article_id and tags:
            _insert_tags(conn, article_id, tags)


def set_article_tags(article_id, tags):
    with get_db() as conn:
        conn.execute("DELETE FROM article_tags WHERE article_id = ?", (article_id,))
        _insert_tags(conn, article_id, tags)


def count_unread():
    with get_db() as conn:
        row = conn.execute(
            "SELECT category, COUNT(*) as n FROM articles WHERE is_read=0 GROUP BY category"
        ).fetchall()
    return {r["category"]: r["n"] for r in row}


def delete_article(article_id):
    with get_db() as conn:
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))


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
        return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def add_source(name, url, src_type, category_hint, scraper_config=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sources (name, url, type, category_hint, scraper_config) VALUES (?,?,?,?,?)",
            (name, url, src_type, category_hint, scraper_config),
        )


def delete_source(source_id):
    with get_db() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def update_last_fetched(source_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE sources SET last_fetched = datetime('now') WHERE id = ?", (source_id,)
        )


def toggle_source(source_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE sources SET is_active = 1 - is_active WHERE id = ?", (source_id,)
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
            "INSERT OR IGNORE INTO keywords (category, keyword) VALUES (?,?)",
            (category, keyword.lower().strip()),
        )


def delete_keyword(keyword_id):
    with get_db() as conn:
        conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))


# --- AI helpers ---

def update_article_ai(article_id, summary, category, priority=None):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET ai_summary=?, category=?, priority=? WHERE id=?",
            (summary, category, priority, article_id),
        )


def toggle_pin(article_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET is_pinned = 1 - is_pinned WHERE id = ?", (article_id,)
        )


def get_pinned_articles():
    sql = """
        SELECT a.*, s.url AS source_url,
               COALESCE(s.url, a.url) AS source_logo_ref,
               GROUP_CONCAT(at.tag, ',') AS tags
        FROM articles a
        LEFT JOIN sources s ON a.source_id = s.id
        LEFT JOIN article_tags at ON a.id = at.article_id
        WHERE a.is_pinned = 1
        GROUP BY a.id
        ORDER BY COALESCE(a.published_at, a.fetched_at) DESC
    """
    with get_db() as conn:
        return conn.execute(sql).fetchall()


# --- Alert helpers ---

def check_and_alert(conn, article_id, title, snippet):
    """Run inside an open connection. Sets alerted=1 if any active rule matches."""
    rules = conn.execute(
        "SELECT keywords FROM alert_rules WHERE is_active = 1"
    ).fetchall()
    if not rules:
        return
    text = (title + " " + (snippet or "")).lower()
    for rule in rules:
        keywords = [k.strip().lower() for k in rule["keywords"].split(",") if k.strip()]
        if any(kw in text for kw in keywords):
            conn.execute("UPDATE articles SET alerted = 1 WHERE id = ?", (article_id,))
            return


def get_alert_rules():
    with get_db() as conn:
        return conn.execute("SELECT * FROM alert_rules ORDER BY name").fetchall()


def add_alert_rule(name, keywords):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO alert_rules (name, keywords) VALUES (?,?)",
            (name, keywords),
        )


def delete_alert_rule(rule_id):
    with get_db() as conn:
        conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))


def toggle_alert_rule(rule_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE alert_rules SET is_active = 1 - is_active WHERE id = ?", (rule_id,)
        )


def recheck_all_alerts():
    """Re-run alert matching on all existing articles (useful after adding a new rule)."""
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
                    conn.execute("UPDATE articles SET alerted = 1 WHERE id = ?", (a["id"],))
                    break


# --- Settings helpers ---

def get_setting(key, default=""):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --- Report helpers ---

def save_report(date, content_json, article_count):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reports (date, content, article_count) VALUES (?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET content=excluded.content, "
            "article_count=excluded.article_count, created_at=datetime('now')",
            (date, content_json, article_count),
        )


def get_report(date):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM reports WHERE date = ?", (date,)
        ).fetchone()


def get_recent_reports(limit=10):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM reports ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()


def get_articles_for_report(date):
    """Return all articles from a given day for the daily report."""
    sql = """
        SELECT a.*, GROUP_CONCAT(at.tag, ',') AS tags
        FROM articles a
        LEFT JOIN article_tags at ON a.id = at.article_id
        WHERE date(COALESCE(a.published_at, a.fetched_at)) = ?
        GROUP BY a.id
        ORDER BY a.category, COALESCE(a.published_at, a.fetched_at) DESC
    """
    with get_db() as conn:
        return conn.execute(sql, (date,)).fetchall()
