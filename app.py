import os
import json
import threading
import uuid
from functools import wraps
from urllib.parse import quote_plus, urlparse
from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify, session
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.security import check_password_hash
import database as db
import categorizer
import exporter
import ai
import text_fetcher
from fetchers import rss as rss_fetcher, scraper as scraper_fetcher

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)

_NO_FULLTEXT = db.NO_FULLTEXT   # shared sentinel – defined once in database.py

CATEGORIES = {
    "alle":            "Alle",
    "eigene_produkte": "Eigene Produkte",
    "markt":           "Markt",
    "wettbewerber":    "Wettbewerber",
    "sonstige":        "Sonstige",
}

WRITE_ROLES = {"admin", "editor"}
AUTH_EXEMPT_ENDPOINTS = {"login", "logout", "static"}
# Viewer-safe interactive endpoint: lets read-only users ask questions about pinned articles.
# Other AI workflows remain blocked for viewers by the write-role POST guard below.
READ_ONLY_POST_ENDPOINTS = {"api_assistant_ask"}


# --- Lightweight auth ---

def _normalise_role(role):
    role = (role or "viewer").strip().lower()
    if role in ("admin", "editor", "viewer"):
        return role
    if role in ("edit", "writer"):
        return "editor"
    return "viewer"


def _load_users():
    """Load users from PRESS_DASHBOARD_USERS.

    Preferred JSON format:
      {"alice": {"password_hash": "...", "role": "editor"}}

    For quick prototypes, "password" is also accepted in the JSON config.

    Lightweight fallback:
      alice|password_hash|editor;bob|password_hash|viewer
    """
    raw = os.environ.get("PRESS_DASHBOARD_USERS", "").strip()
    users = {}
    if not raw:
        return users

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for username, config in data.items():
                if isinstance(config, str):
                    password_hash = config
                    password = ""
                    role = "viewer"
                else:
                    password_hash = (config or {}).get("password_hash", "")
                    password = (config or {}).get("password", "")
                    role = (config or {}).get("role", "viewer")
                if username and (password_hash or password):
                    users[username] = {
                        "password_hash": password_hash,
                        "password": password,
                        "role": _normalise_role(role),
                    }
            return users
    except json.JSONDecodeError:
        pass

    for part in raw.split(";"):
        if not part.strip():
            continue
        pieces = [p.strip() for p in part.split("|", 2)]
        if len(pieces) != 3:
            continue
        username, password_hash, role = pieces
        if username and password_hash:
            users[username] = {
                "password_hash": password_hash,
                "password": "",
                "role": _normalise_role(role),
            }
    return users


def current_user():
    username = session.get("username")
    if not username:
        return None
    user_config = _load_users().get(username)
    if not user_config:
        return None
    return {"username": username, "role": user_config["role"]}


def can_edit():
    user = current_user()
    return bool(user and user["role"] in WRITE_ROLES)


def _is_safe_next_url(next_url):
    if not next_url:
        return False
    parsed = urlparse(next_url)
    return not parsed.netloc and parsed.scheme == ""


def _password_matches(user_config, password):
    password_hash = user_config.get("password_hash", "")
    if password_hash:
        try:
            return check_password_hash(password_hash, password)
        except ValueError:
            return False
    return password == user_config.get("password", "")


def editor_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        if not can_edit():
            flash("Dein Zugang ist auf Lesen beschränkt.", "warning")
            return redirect(request.referrer or url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def require_login():
    endpoint = request.endpoint or ""
    if endpoint in AUTH_EXEMPT_ENDPOINTS:
        return None
    if not current_user():
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
    if request.method not in ("GET", "HEAD", "OPTIONS") and endpoint not in READ_ONLY_POST_ENDPOINTS and not can_edit():
        flash("Dein Zugang ist auf Lesen beschränkt.", "warning")
        return redirect(request.referrer or url_for("dashboard"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    users = _load_users()
    next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard")
    if not _is_safe_next_url(next_url):
        next_url = url_for("dashboard")

    if current_user():
        return redirect(next_url)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_config = users.get(username)
        if user_config and _password_matches(user_config, password):
            session.clear()
            session["username"] = username
            session["role"] = user_config["role"]
            flash("Willkommen zurück.", "success")
            return redirect(next_url)
        flash("Login fehlgeschlagen.", "danger")

    return render_template("login.html", users_configured=bool(users), next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Du bist abgemeldet.", "info")
    return redirect(url_for("login"))


# --- Background scheduler ---

def _fetch_job():
    total = rss_fetcher.fetch_all() + scraper_fetcher.fetch_all()
    print(f"[Scheduler] {total} neue Artikel importiert.")


def _cleanup_job():
    deleted = db.delete_old_unpinned_articles(days=30)
    if deleted:
        print(f"[Scheduler] {deleted} alte ungepinnte Artikel gelöscht.")


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(_fetch_job, "interval", hours=4, id="auto_fetch")
scheduler.add_job(_cleanup_job, "interval", hours=24, id="auto_cleanup")


# --- Routes ---

def _compact_radar_run(limit_topics=6):
    run = db.get_latest_radar_run()
    payload = _radar_payload(run) if run else None
    if payload and limit_topics:
        payload["topics"] = payload["topics"][:limit_topics]
    return payload


@app.route("/")
def dashboard():
    """Management overview for the implemented press-monitoring features."""
    tag_window = request.args.get("tags", "12")
    if tag_window not in ("4", "12", "26", "52", "all"):
        tag_window = "12"
    tag_rows = (
        sorted(db.get_all_tags(), key=lambda row: row["count"], reverse=True)[:12]
        if tag_window == "all"
        else db.get_top_tags(int(tag_window), limit=12)
    )
    sources = db.get_sources()
    latest_articles = db.get_articles(limit=8)
    pinned_articles = db.get_pinned_articles()
    curated_articles = pinned_articles[:6]
    recent_reports = db.get_recent_reports(limit=3)
    radar_run = _compact_radar_run(limit_topics=7)

    return render_template(
        "management_dashboard.html",
        sources=sources,
        active_sources=sum(1 for source in sources if source["is_active"]),
        latest_articles=latest_articles,
        curated_articles=curated_articles,
        has_pinned_articles=bool(pinned_articles),
        tag_window=tag_window,
        tag_labels=[row["tag"] for row in tag_rows],
        tag_counts=[row["count"] for row in tag_rows],
        total_tag_assignments=sum(row["count"] for row in tag_rows),
        recent_reports=recent_reports,
        radar_run=radar_run,
        categories=CATEGORIES,
    )


@app.route("/kuratierte-artikel")
def curated_articles():
    """Pinned-articles table — the curated article view."""
    search         = request.args.get("q", "").strip()
    tag            = request.args.get("tag", "").strip()
    von            = request.args.get("von", "")
    bis            = request.args.get("bis", "")
    source_id      = request.args.get("quelle", "").strip()
    geschaeftsfeld = request.args.get("gf", "").strip()
    show_internal_sector = can_edit() and request.args.get("internal_sector") == "1"

    articles = db.get_pinned_articles(
        search=search or None,
        tag=tag or None,
        von=von or None,
        bis=bis or None,
        source_id=int(source_id) if source_id.isdigit() else None,
        geschaeftsfeld=geschaeftsfeld or None,
    )
    sources = db.get_sources()
    is_filtered = bool(search or tag or von or bis or source_id or geschaeftsfeld)
    has_pinned_articles = bool(articles) if not is_filtered else bool(db.get_pinned_articles())
    dashboard_colspan = 7 + (1 if can_edit() else 0) + (1 if show_internal_sector else 0)
    return render_template(
        "dashboard.html",
        articles=articles,
        categories=CATEGORIES,
        sources=sources,
        radar_preset_sectors=ai.get_radar_preset_sectors(),
        search=search,
        active_tag=tag,
        von=von,
        bis=bis,
        active_source=source_id,
        active_gf=geschaeftsfeld,
        is_filtered=is_filtered,
        has_pinned_articles=has_pinned_articles,
        show_internal_sector=show_internal_sector,
        dashboard_colspan=dashboard_colspan,
    )


@app.route("/newsfeed")
def newsfeed():
    """Full RSS article feed for screening and pinning."""
    category     = request.args.get("kategorie", "alle")
    search       = request.args.get("q", "").strip()
    von          = request.args.get("von", "")
    bis          = request.args.get("bis", "")
    tag          = request.args.get("tag", "").strip()
    source_id    = request.args.get("quelle", "").strip()
    priority     = request.args.get("prio", "").strip()
    alerted_only = request.args.get("alerts") == "1"
    show_ignored = request.args.get("ignored") == "1"

    articles = db.get_articles(
        category=category if category != "alle" else None,
        search=search or None,
        von=von or None,
        bis=bis or None,
        tag=tag or None,
        source_id=int(source_id) if source_id.isdigit() else None,
        priority=priority or None,
        alerted_only=alerted_only,
        include_ignored=show_ignored,
    )
    unread = db.count_unread()
    all_tags = db.get_all_tags()
    sources = db.get_sources(active_only=True)
    alert_count = len(db.get_articles(alerted_only=True, limit=500))
    ignored_count = db.count_ignored()

    toggle_args = {}
    if category != "alle":
        toggle_args["kategorie"] = category
    for key, value in (
        ("q", search),
        ("von", von),
        ("bis", bis),
        ("tag", tag),
        ("quelle", source_id),
        ("prio", priority),
    ):
        if value:
            toggle_args[key] = value
    if alerted_only:
        toggle_args["alerts"] = "1"

    return render_template(
        "newsfeed.html",
        articles=articles,
        categories=CATEGORIES,
        active_category=category,
        unread=unread,
        search=search,
        von=von,
        bis=bis,
        active_tag=tag,
        active_source=source_id,
        all_tags=all_tags,
        sources=sources,
        active_prio=priority,
        alerted_only=alerted_only,
        alert_count=alert_count,
        show_ignored=show_ignored,
        ignored_count=ignored_count,
        show_ignored_url=url_for("newsfeed", **{**toggle_args, "ignored": "1"}),
        hide_ignored_url=url_for("newsfeed", **toggle_args),
    )


@app.route("/artikel/<int:article_id>")
def artikel(article_id):
    article = db.get_article(article_id)
    if article is None:
        flash("Artikel nicht gefunden.", "warning")
        return redirect(url_for("newsfeed"))
    if can_edit():
        db.mark_read(article_id)
    return render_template("artikel.html", article=article, categories=CATEGORIES)


@app.route("/artikel/add", methods=["POST"])
def add_artikel():
    mode = request.form.get("mode", "url")
    title = request.form.get("title", "").strip()
    url = request.form.get("url", "").strip()
    source_name = request.form.get("source_name", "").strip()
    content_snippet = request.form.get("content_snippet", "").strip()[:500]
    category = request.form.get("category", "sonstige")
    published_at = request.form.get("published_at", "").strip() or None

    raw_tags = request.form.get("tags", "")
    tags = [t.strip().lower() for t in raw_tags.split(",") if t.strip()]

    if url:
        duplicate = db.find_duplicate_article(title, url, source_name, published_at)
        if duplicate and duplicate["is_pinned"]:
            flash(
                f"Duplikat erkannt: „{duplicate['title']}“ ist bereits in den kuratierten Artikeln gepinnt.",
                "info",
            )
            return redirect(url_for("newsfeed"))

    fetched = {}
    fetch_warning = None
    if mode != "manual" and url and (not title or not source_name or not content_snippet or not published_at):
        fetched = text_fetcher.fetch_article_details(url)
        if ai.is_configured() and fetched:
            try:
                cleaned = ai.extract_article_object(url, fetched)
                fetched.update({k: v for k, v in cleaned.items() if v})
            except Exception as e:
                fetch_warning = f"KI-Bereinigung der URL-Daten fehlgeschlagen: {e}"
        title = title or fetched.get("title", "").strip()
        source_name = source_name or fetched.get("source_name", "").strip()
        content_snippet = content_snippet or fetched.get("content_snippet", "").strip()[:500]
        published_at = published_at or fetched.get("published_at")
        if category == "sonstige" and fetched.get("category") in CATEGORIES:
            category = fetched["category"]
        if not tags and fetched.get("tags"):
            tags = fetched["tags"]

    source_name = source_name or "Manuell"

    if not title:
        flash("Titel ist erforderlich oder muss aus einer gültigen URL lesbar sein.", "danger")
        return redirect(url_for("newsfeed"))

    if not content_snippet and fetched.get("full_text"):
        content_snippet = fetched["full_text"][:500]

    article_id, created = db.add_article(
        title,
        url or None,
        source_name,
        content_snippet,
        category,
        published_at,
        tags=tags,
        return_status=True,
    )
    article = db.get_article(article_id) if article_id else None
    was_pinned = bool(article["is_pinned"]) if article else False

    if article and was_pinned and not created:
        flash(
            f"Duplikat erkannt: „{article['title']}“ ist bereits in den kuratierten Artikeln gepinnt.",
            "info",
        )
        if fetch_warning:
            flash(fetch_warning, "warning")
        return redirect(url_for("newsfeed"))

    if article_id:
        db.set_article_ignored(article_id, False)
        db.set_article_pinned(article_id, True)

    if mode == "manual":
        if article_id and content_snippet:
            db.update_article_ai(article_id, content_snippet, category, None, None, ai_generated=False)
            db.set_article_tags(article_id, tags)
            categorizer.invalidate()
        if created:
            flash("Artikel wurde manuell hinzugefügt und in den kuratierten Artikeln gepinnt.", "success")
        else:
            flash("Bestehender Artikel wurde erkannt und in den kuratierten Artikeln gepinnt.", "success")
    elif url and article_id and ai.is_configured():
        text_for_ai = fetched.get("full_text") or content_snippet or ""
        try:
            result = ai.analyse_article_for_pin(title, text_for_ai)
            db.update_article_ai(
                article_id,
                summary=result["zusammenfassung"],
                category=result["kategorie"],
                priority=None,
                model_used=result.get("model_used"),
                geschaeftsfeld=result["geschaeftsfeld"],
                implications=result["implikationen"],
                radar_sector=result.get("radar_sector"),
            )
            db.set_article_tags(article_id, result["tags"])
            db.delete_article_chunks(article_id)
            categorizer.invalidate()
            if created:
                flash("Artikel wurde per KI analysiert und in den kuratierten Artikeln gepinnt.", "success")
            else:
                flash("Bestehender Artikel wurde erkannt, per KI analysiert und gepinnt.", "success")
        except Exception as e:
            flash(f"Artikel wurde gepinnt, KI-Analyse fehlgeschlagen: {e}", "warning")
    else:
        if created:
            flash("Artikel wurde hinzugefügt und in den kuratierten Artikeln gepinnt.", "success")
        else:
            flash("Bestehender Artikel wurde erkannt und in den kuratierten Artikeln gepinnt.", "success")
    if fetch_warning:
        flash(fetch_warning, "warning")
    return redirect(url_for("newsfeed"))


@app.route("/artikel/<int:article_id>/analyse", methods=["POST"])
def analyse_artikel(article_id):
    article = db.get_article(article_id)
    if article is None:
        flash("Artikel nicht gefunden.", "warning")
        return redirect(url_for("newsfeed"))

    full_text = None
    if article["url"]:
        try:
            full_text = text_fetcher.fetch_full_text(article["url"])
        except Exception:
            pass

    # JS-rendered sites return only a tiny fragment (title + site name).
    # Fall back to the stored RSS snippet so the AI has something meaningful.
    if not full_text or len(full_text) < 300:
        snippet_fallback = (article.get("content_snippet") or "").strip()
        if len(snippet_fallback) > len(full_text or ""):
            full_text = snippet_fallback or None

    if not full_text:
        flash(
            "Volltext konnte nicht geladen werden – keine KI-Zusammenfassung möglich.",
            "warning",
        )
        return redirect(request.referrer or url_for("artikel", article_id=article_id))

    try:
        result = ai.analyse_article_for_pin(article["title"], full_text)
        db.update_article_ai(
            article_id,
            summary=result["zusammenfassung"],
            category=result["kategorie"],
            priority=article["priority"],
            model_used=result.get("model_used"),
            geschaeftsfeld=result["geschaeftsfeld"],
            implications=result["implikationen"],
            radar_sector=result.get("radar_sector"),
        )
        db.set_article_tags(article_id, result["tags"])
        db.delete_article_chunks(article_id)
        categorizer.invalidate()
        flash("KI-Analyse abgeschlossen (Volltext gelesen).", "success")
    except Exception as e:
        flash(f"KI-Analyse fehlgeschlagen: {e}", "danger")
    return redirect(request.referrer or url_for("artikel", article_id=article_id))


@app.route("/artikel/<int:article_id>/pin", methods=["POST"])
def pin_artikel(article_id):
    article = db.get_article(article_id)
    if article is None:
        flash("Artikel nicht gefunden.", "warning")
        return redirect(request.referrer or url_for("newsfeed"))

    currently_pinned = bool(article["is_pinned"])

    if currently_pinned and request.form.get("confirm_unpin") != "1":
        flash("Bitte bestätige das Entpinnen des Artikels.", "warning")
        return redirect(request.referrer or url_for("newsfeed"))

    # Pinning (not unpinning) → fetch fulltext, then run AI
    if not currently_pinned:
        duplicate = db.get_pinned_duplicate_article(article_id)
        if duplicate:
            flash(
                f"Duplikat erkannt: „{duplicate['title']}“ ist bereits in den kuratierten Artikeln gepinnt.",
                "info",
            )
            return redirect(request.referrer or url_for("newsfeed"))

        full_text = None
        if article["url"]:
            try:
                full_text = text_fetcher.fetch_full_text(article["url"])
            except Exception:
                pass

        # JS-rendered sites return only a tiny fragment (title + site name).
        # Fall back to the stored RSS snippet so the AI has something meaningful.
        if not full_text or len(full_text) < 300:
            snippet_fallback = (article.get("content_snippet") or "").strip()
            if len(snippet_fallback) > len(full_text or ""):
                full_text = snippet_fallback or None

        if full_text and ai.is_configured():
            try:
                result = ai.analyse_article_for_pin(article["title"], full_text)
                db.update_article_ai(
                    article_id,
                    summary=result["zusammenfassung"],
                    category=result["kategorie"],
                    priority=article["priority"],
                    model_used=result.get("model_used"),
                    geschaeftsfeld=result["geschaeftsfeld"],
                    implications=result["implikationen"],
                    radar_sector=result.get("radar_sector"),
                )
                db.set_article_tags(article_id, result["tags"])
                db.delete_article_chunks(article_id)
                categorizer.invalidate()
            except Exception as e:
                flash(f"KI-Analyse beim Pinnen fehlgeschlagen: {e}", "warning")
        else:
            # Fulltext unavailable – store sentinel so the UI shows a clear message
            db.update_article_ai(
                article_id,
                summary=_NO_FULLTEXT,
                category=article["category"],
                priority=article["priority"],
                model_used=None,
                geschaeftsfeld=None,
                implications=None,
                ai_generated=False,
            )
            if ai.is_configured():
                flash(
                    "Volltext konnte nicht geladen werden – "
                    "keine KI-Zusammenfassung möglich. "
                    "Du kannst die Felder in den kuratierten Artikeln manuell ausfüllen.",
                    "info",
                )

    db.toggle_pin(article_id)
    if currently_pinned:
        db.delete_article_chunks(article_id)
    return redirect(request.referrer or url_for("newsfeed"))


@app.route("/artikel/<int:article_id>/loeschen", methods=["POST"])
def delete_artikel(article_id):
    db.delete_article(article_id)
    flash("Artikel wurde gelöscht.", "success")
    return redirect(request.referrer or url_for("newsfeed"))


@app.route("/artikel/<int:article_id>/mark-read", methods=["POST"])
def mark_artikel_read(article_id):
    db.mark_read(article_id)
    return redirect(request.referrer or url_for("newsfeed"))


@app.route("/artikel/<int:article_id>/ignore", methods=["POST"])
def ignore_artikel(article_id):
    db.set_article_ignored(article_id, True)
    flash("Artikel wurde ausgeblendet.", "success")
    return redirect(request.referrer or url_for("newsfeed"))


@app.route("/artikel/<int:article_id>/unignore", methods=["POST"])
def unignore_artikel(article_id):
    db.set_article_ignored(article_id, False)
    flash("Artikel wird wieder im Newsfeed angezeigt.", "success")
    return redirect(request.referrer or url_for("newsfeed"))


@app.route("/artikel/bulk", methods=["POST"])
def bulk_artikel_action():
    article_ids = request.form.getlist("article_ids")
    action = request.form.get("bulk_action", "")

    if not article_ids:
        flash("Bitte mindestens einen Artikel auswählen.", "warning")
        return redirect(request.referrer or url_for("newsfeed"))

    if action == "mark_read":
        count = db.mark_articles_read(article_ids)
        flash(f"{count} Artikel als gelesen markiert.", "success")
    elif action == "ignore":
        count = db.set_articles_ignored(article_ids, True)
        flash(f"{count} Artikel ausgeblendet.", "success")
    elif action == "unignore":
        count = db.set_articles_ignored(article_ids, False)
        flash(f"{count} Artikel wieder eingeblendet.", "success")
    else:
        flash("Unbekannte Bulk-Aktion.", "warning")

    return redirect(request.referrer or url_for("newsfeed"))


@app.route("/artikel/<int:article_id>/update-fields", methods=["POST"])
def update_artikel_fields(article_id):
    """Save manually-edited dashboard fields for a pinned article."""
    ai_summary      = request.form.get("ai_summary", "").strip()
    ai_implications = request.form.get("ai_implications", "").strip()
    tags_raw        = request.form.get("tags", "").strip()
    geschaeftsfeld  = request.form.get("geschaeftsfeld", "").strip()
    category        = request.form.get("category", "").strip()
    radar_sector_submitted = "radar_sector" in request.form
    radar_sector    = request.form.get("radar_sector", "").strip()

    if geschaeftsfeld not in ("Leben", "Kranken", "Sonstiges"):
        geschaeftsfeld = None
    if category not in CATEGORIES or category == "alle":
        category = None
    valid_radar_sectors = ai.get_radar_preset_sectors()
    if radar_sector and radar_sector not in valid_radar_sectors:
        radar_sector = None

    tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]

    db.update_article_manual_fields(
        article_id,
        ai_summary=ai_summary or None,
        ai_implications=ai_implications or None,
        geschaeftsfeld=geschaeftsfeld,
        category=category,
        radar_sector=radar_sector or None,
        update_radar_sector=radar_sector_submitted,
    )
    db.set_article_tags(article_id, tags)
    db.delete_article_chunks(article_id)
    return redirect(request.referrer or url_for("curated_articles"))


@app.route("/api/assistant/ask", methods=["POST"])
def api_assistant_ask():
    payload = request.get_json(silent=True) or request.form
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "Bitte eine Frage eingeben."}), 400
    if not ai.is_configured():
        return jsonify({
            "ok": False,
            "error": "Bitte zuerst den OpenRouter API-Schlüssel in den Einstellungen hinterlegen.",
        }), 400
    try:
        result = ai.answer_pinned_question(question)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/assistant/reindex", methods=["POST"])
@editor_required
def api_assistant_reindex():
    try:
        result = ai.refresh_pinned_article_chunks()
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _radar_filter_state(source):
    category = (source.get("kategorie") or "").strip()
    if category == "alle" or category not in CATEGORIES:
        category = ""

    geschaeftsfeld = (source.get("gf") or "").strip()
    if geschaeftsfeld not in ("Leben", "Kranken", "Sonstiges"):
        geschaeftsfeld = ""

    days_raw = (source.get("zeitraum") or "all").strip()
    days = int(days_raw) if days_raw in ("30", "90") else 0

    return {
        "category": category,
        "geschaeftsfeld": geschaeftsfeld,
        "days": days,
    }


def _radar_url_args(filters, **extra):
    args = {}
    if filters.get("category"):
        args["kategorie"] = filters["category"]
    if filters.get("geschaeftsfeld"):
        args["gf"] = filters["geschaeftsfeld"]
    if filters.get("days"):
        args["zeitraum"] = str(filters["days"])
    args.update({k: v for k, v in extra.items() if v not in (None, "")})
    return args


def _radar_filters_from_run(run):
    try:
        raw_filters = json.loads(run["filters_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raw_filters = {}
    return _normalize_radar_filters(raw_filters)


def _normalize_radar_filters(raw_filters):
    if not isinstance(raw_filters, dict):
        raw_filters = {}
    category = raw_filters.get("category") or ""
    if category not in CATEGORIES or category == "alle":
        category = ""
    geschaeftsfeld = raw_filters.get("geschaeftsfeld") or ""
    if geschaeftsfeld not in ("Leben", "Kranken", "Sonstiges"):
        geschaeftsfeld = ""
    try:
        days = int(raw_filters.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    if days not in (0, 30, 90):
        days = 0
    return {
        "category": category,
        "geschaeftsfeld": geschaeftsfeld,
        "days": days,
    }


def _radar_filters_from_job(job):
    try:
        raw_filters = json.loads(job["filters_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raw_filters = {}
    return _normalize_radar_filters(raw_filters)


def _run_trendradar_job(job_id):
    try:
        job = db.get_radar_job(job_id)
        if not job:
            return
        radar_filters = _radar_filters_from_job(job)
        db.mark_radar_job_running(job_id, "KI analysiert gepinnte Artikel und clustert Trends.")
        articles = db.get_pinned_articles_for_radar(
            category=radar_filters["category"] or None,
            geschaeftsfeld=radar_filters["geschaeftsfeld"] or None,
            days=radar_filters["days"] or None,
        )
        if not articles:
            raise ValueError("Keine gepinnten Artikel für diese Radar-Filter gefunden.")
        if not ai.is_configured():
            raise ValueError("Bitte zuerst den OpenRouter API-Schlüssel in den Einstellungen hinterlegen.")

        result = ai.generate_trend_radar(articles, radar_filters)
        run_id = db.save_radar_run(
            result,
            radar_filters,
            article_count=len(articles),
            model_used=result.get("model_used"),
        )
        db.mark_radar_job_succeeded(
            job_id,
            run_id,
            f"Trendradar erstellt: {len(result['topics'])} Themen aus {len(articles)} Artikeln.",
        )
    except Exception as exc:
        db.mark_radar_job_failed(job_id, exc)


def _start_trendradar_job(job_id):
    thread = threading.Thread(target=_run_trendradar_job, args=(job_id,), daemon=True)
    thread.start()


def _radar_payload(run):
    if not run:
        return None
    try:
        sectors = json.loads(run["sectors_json"] or "[]")
    except json.JSONDecodeError:
        sectors = []
    topic_rows = db.get_radar_topics(run["id"])
    topics = []
    for topic in topic_rows:
        try:
            article_ids = json.loads(topic["article_ids"] or "[]")
        except json.JSONDecodeError:
            article_ids = []
        articles = []
        for article in db.get_articles_by_ids(article_ids):
            articles.append({
                "id": article["id"],
                "title": article["title"],
                "url": article["url"],
                "source_name": article["source_name"],
                "date": (article["published_at"] or article["fetched_at"] or "")[:10],
                "category": article["category"],
                "geschaeftsfeld": article["geschaeftsfeld"],
                "tags": article["tags"],
            })
        topics.append({
            "id": topic["id"],
            "name": topic["name"],
            "sector": topic["sector"],
            "horizon": topic["horizon"],
            "summary": topic["summary"] or "",
            "evidence": topic["evidence"] or "",
            "confidence": topic["confidence"],
            "article_count": len(articles),
            "articles": articles,
        })
    if not sectors:
        sectors = sorted({topic["sector"] for topic in topics})
    return {
        "id": run["id"],
        "label": run["label"],
        "created_at": run["created_at"],
        "model": run["model"],
        "article_count": run["article_count"],
        "sectors": sectors,
        "topics": topics,
    }


@app.route("/trendradar")
def trendradar():
    from datetime import date as date_type, timedelta

    weeks = request.args.get("wochen", "12")
    weeks = int(weeks) if weeks in ("4", "8", "12", "26") else 12
    radar_filters = _radar_filter_state(request.args)
    radar_job = None
    job_id = request.args.get("job", "").strip()
    if job_id:
        job = db.get_radar_job(job_id)
        if job:
            job_filters = _radar_filters_from_job(job)
            if job["status"] == "succeeded" and job["run_id"]:
                flash(job["message"] or "Trendradar wurde erstellt.", "success")
                return redirect(url_for("trendradar", **_radar_url_args(job_filters, run=job["run_id"])))
            if job["status"] == "failed":
                flash(f"Trendradar-Erstellung fehlgeschlagen: {job['error'] or job['message']}", "danger")
                return redirect(url_for("trendradar", **_radar_url_args(job_filters)))
            radar_job = job
            radar_filters = job_filters
    selected_run = None
    run_id = request.args.get("run", "").strip()
    if run_id.isdigit():
        selected_run = db.get_radar_run(int(run_id))
        if selected_run:
            radar_filters = _radar_filters_from_run(selected_run)
    if not selected_run:
        selected_run = db.get_latest_radar_run(
            category=radar_filters["category"] or None,
            geschaeftsfeld=radar_filters["geschaeftsfeld"] or None,
            days=radar_filters["days"] or None,
        )
    radar_articles = db.get_pinned_articles_for_radar(
        category=radar_filters["category"] or None,
        geschaeftsfeld=radar_filters["geschaeftsfeld"] or None,
        days=radar_filters["days"] or None,
    )
    radar_run = _radar_payload(selected_run)
    recent_radar_runs = db.get_recent_radar_runs(limit=8)

    # Build the complete sequence of ISO-week Mondays for the chosen window
    today    = date_type.today()
    monday   = today - timedelta(days=today.weekday())
    week_dates  = [monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    week_keys   = [d.isoformat() for d in week_dates]          # '2024-01-08'
    week_labels = [f"KW {d.strftime('%V')}" for d in week_dates]

    # ── Category trend ───────────────────────────────────────────────────────
    CAT_COLORS = {
        "markt":           "#0d6efd",
        "wettbewerber":    "#dc3545",
        "eigene_produkte": "#198754",
        "sonstige":        "#6c757d",
    }
    cat_rows = db.get_category_trend(weeks)
    cat_map = {}
    for row in cat_rows:
        wk  = str(row["week_monday"])[:10]
        cat = row["category"]
        cat_map.setdefault(cat, {})[wk] = row["count"]

    cat_datasets = []
    for cat_key, cat_label in CATEGORIES.items():
        if cat_key == "alle":
            continue
        data = [cat_map.get(cat_key, {}).get(wk, 0) for wk in week_keys]
        if not any(data):
            continue
        color = CAT_COLORS.get(cat_key, "#6c757d")
        cat_datasets.append({
            "label":           cat_label,
            "data":            data,
            "borderColor":     color,
            "backgroundColor": color + "22",
            "tension":         0.35,
            "fill":            True,
            "pointRadius":     3,
            "pointHoverRadius": 5,
        })

    # ── Alert trend ──────────────────────────────────────────────────────────
    alert_rows = db.get_alert_trend(weeks)
    alert_map  = {str(r["week_monday"])[:10]: r["count"] for r in alert_rows}
    alert_data = [alert_map.get(wk, 0) for wk in week_keys]

    # ── Top tags (pinned articles) ───────────────────────────────────────────
    tag_rows   = db.get_top_tags(weeks, limit=12)
    tag_labels = [r["tag"]   for r in tag_rows]
    tag_counts = [r["count"] for r in tag_rows]

    # ── Source stats ─────────────────────────────────────────────────────────
    source_rows = db.get_source_stats(weeks, limit=12)

    # ── Summary KPIs ─────────────────────────────────────────────────────────
    stats = db.get_trend_stats(weeks)

    return render_template(
        "trendradar.html",
        weeks=weeks,
        week_labels=week_labels,
        cat_datasets=cat_datasets,
        alert_data=alert_data,
        tag_labels=tag_labels,
        tag_counts=tag_counts,
        source_rows=source_rows,
        stats=stats,
        categories=CATEGORIES,
        radar_filters=radar_filters,
        radar_articles_count=len(radar_articles),
        radar_run=radar_run,
        radar_job=radar_job,
        recent_radar_runs=recent_radar_runs,
        ai_configured=ai.is_configured(),
        radar_url_args=_radar_url_args,
    )


@app.route("/trendradar/regenerate", methods=["POST"])
@editor_required
def trendradar_regenerate():
    radar_filters = _radar_filter_state(request.form)
    articles = db.get_pinned_articles_for_radar(
        category=radar_filters["category"] or None,
        geschaeftsfeld=radar_filters["geschaeftsfeld"] or None,
        days=radar_filters["days"] or None,
    )
    if not articles:
        flash("Keine gepinnten Artikel für diese Radar-Filter gefunden.", "warning")
        return redirect(url_for("trendradar", **_radar_url_args(radar_filters)))
    if not ai.is_configured():
        flash("Bitte zuerst den OpenRouter API-Schlüssel in den Einstellungen hinterlegen.", "warning")
        return redirect(url_for("trendradar", **_radar_url_args(radar_filters)))
    job_id = uuid.uuid4().hex
    db.create_radar_job(job_id, radar_filters, len(articles))
    _start_trendradar_job(job_id)
    flash(f"Trendradar-Erstellung gestartet: {len(articles)} Artikel werden im Hintergrund analysiert.", "info")
    return redirect(url_for("trendradar", **_radar_url_args(radar_filters, job=job_id)))


@app.route("/trendradar/job/<job_id>")
@editor_required
def trendradar_job_status(job_id):
    job = db.get_radar_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Radar-Job nicht gefunden."}), 404

    radar_filters = _radar_filters_from_job(job)
    payload = {
        "ok": True,
        "id": job["id"],
        "status": job["status"],
        "message": job["message"] or "",
        "error": job["error"] or "",
        "article_count": job["article_count"],
        "run_id": job["run_id"],
    }
    if job["status"] == "succeeded" and job["run_id"]:
        payload["redirect_url"] = url_for("trendradar", **_radar_url_args(radar_filters, run=job["run_id"]))
    elif job["status"] == "failed":
        payload["redirect_url"] = url_for("trendradar", **_radar_url_args(radar_filters))
    return jsonify(payload)


@app.route("/trendradar/topic/<int:topic_id>", methods=["PATCH", "POST"])
@editor_required
def trendradar_update_topic(topic_id):
    payload = request.get_json(silent=True) or request.form
    sector = (payload.get("sector") or "").strip()
    horizon = (payload.get("horizon") or "").strip()
    if not sector:
        return jsonify({"ok": False, "error": "Sektor fehlt."}), 400
    if horizon not in ("Act", "Prepare", "Monitor"):
        return jsonify({"ok": False, "error": "Ungültiger Horizont."}), 400

    topic = db.update_radar_topic(topic_id, sector, horizon)
    if not topic:
        return jsonify({"ok": False, "error": "Thema nicht gefunden."}), 404
    return jsonify({
        "ok": True,
        "topic": {
            "id": topic["id"],
            "run_id": topic["run_id"],
            "name": topic["name"],
            "sector": topic["sector"],
            "horizon": topic["horizon"],
        },
    })


@app.route("/trendradar/delete/<int:run_id>", methods=["POST"])
@editor_required
def trendradar_delete(run_id):
    db.delete_radar_run(run_id)
    flash("Trendradar gelöscht.", "success")
    return redirect(url_for("trendradar"))


@app.route("/quellen")
def quellen():
    sources = db.get_sources()
    return render_template("quellen.html", sources=sources, categories=CATEGORIES)


@app.route("/quellen/add", methods=["POST"])
def add_quelle():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    src_type = request.form.get("type", "rss")
    category_hint = request.form.get("category_hint", "sonstige")
    scraper_config = request.form.get("scraper_config", "").strip() or None

    if not name or not url:
        flash("Name und URL sind erforderlich.", "danger")
        return redirect(url_for("quellen"))

    db.add_source(name, url, src_type, category_hint, scraper_config)
    flash(f'Quelle "{name}" wurde hinzugefuegt.', "success")
    return redirect(url_for("quellen"))


@app.route("/quellen/<int:source_id>/loeschen", methods=["POST"])
def delete_quelle(source_id):
    db.delete_source(source_id)
    flash("Quelle wurde gelöscht.", "success")
    return redirect(url_for("quellen"))


@app.route("/quellen/<int:source_id>/toggle", methods=["POST"])
def toggle_quelle(source_id):
    db.toggle_source(source_id)
    return redirect(url_for("quellen"))


@app.route("/api/fetch", methods=["POST"])
def api_fetch_all():
    total = rss_fetcher.fetch_all() + scraper_fetcher.fetch_all()
    flash(f"{total} neue Artikel importiert.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/api/fetch/<int:source_id>", methods=["POST"])
def api_fetch_one(source_id):
    source = db.get_source(source_id)
    if source is None:
        flash("Quelle nicht gefunden.", "warning")
        return redirect(url_for("quellen"))
    n = 0
    try:
        if source["type"] == "rss":
            n = rss_fetcher.fetch_source(source)
        elif source["type"] == "scraper":
            n = scraper_fetcher.fetch_source(source)
    except Exception as e:
        flash(f"Fehler beim Abruf: {e}", "danger")
        return redirect(url_for("quellen"))
    flash(f'{n} neue Artikel von "{source["name"]}" importiert.', "success")
    return redirect(url_for("quellen"))


@app.route("/bericht")
def bericht():
    from datetime import date as date_type
    today = date_type.today().isoformat()
    mode = request.args.get("mode", "daily")
    if mode not in ("daily", "weekly"):
        mode = "daily"
    date_param = request.args.get("date", today)
    report_key = date_param if mode == "daily" else f"{date_param}_weekly"
    report_row = db.get_report(report_key)
    report = json.loads(report_row["content"]) if report_row else None
    recent = db.get_recent_reports(limit=14)
    return render_template(
        "bericht.html",
        report=report,
        report_row=report_row,
        recent=recent,
        today=today,
        mode=mode,
        date_param=date_param,
    )


@app.route("/bericht/erstellen", methods=["POST"])
def bericht_erstellen():
    from datetime import date as date_type, timedelta
    mode = request.form.get("mode", "daily")
    if mode not in ("daily", "weekly"):
        mode = "daily"
    target_date = request.form.get("date", date_type.today().isoformat())
    report_key = target_date if mode == "daily" else f"{target_date}_weekly"

    if mode == "daily":
        articles = db.get_articles_for_report(target_date)
        period_label = target_date
    else:
        articles = db.get_articles_for_week_report(target_date)
        end = date_type.fromisoformat(target_date)
        start = (end - timedelta(days=6)).isoformat()
        period_label = f"{start} bis {target_date}"

    if not articles:
        flash("Keine Artikel für diesen Zeitraum gefunden.", "warning")
        return redirect(url_for("bericht", mode=mode, date=target_date))
    try:
        result = ai.generate_daily_report(articles, period_label, mode=mode)
        db.save_report(report_key, json.dumps(result, ensure_ascii=False), len(articles))
        label = "Tagesbericht" if mode == "daily" else "Wochenbericht"
        flash(f"{label} für {period_label} erstellt ({len(articles)} gepinnte Artikel).", "success")
    except Exception as e:
        flash(f"Bericht-Erstellung fehlgeschlagen: {e}", "danger")
    return redirect(url_for("bericht", mode=mode, date=target_date))


@app.route("/export/pdf")
@editor_required
def export_pdf():
    days = int(request.args.get("tage", 7))
    try:
        pdf_bytes = exporter.generate_pdf(days=days)
    except Exception as e:
        flash(f"PDF-Export fehlgeschlagen: {e}", "danger")
        return redirect(url_for("dashboard"))
    from datetime import datetime
    filename = f"digest_{datetime.now().strftime('%Y%m%d')}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/einstellungen", methods=["GET", "POST"])
@editor_required
def einstellungen():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_keyword":
            cat = request.form.get("category", "")
            kw = request.form.get("keyword", "").strip()
            if cat and kw:
                db.add_keyword(cat, kw)
                categorizer.invalidate()
                flash(f'Stichwort "{kw}" hinzugefuegt.', "success")
        elif action == "delete_keyword":
            kid = request.form.get("keyword_id")
            if kid:
                db.delete_keyword(int(kid))
                categorizer.invalidate()
                flash("Stichwort gelöscht.", "success")
        elif action == "add_alert_rule":
            name = request.form.get("rule_name", "").strip()
            keywords = request.form.get("rule_keywords", "").strip()
            if name and keywords:
                db.add_alert_rule(name, keywords)
                db.recheck_all_alerts()
                flash(f'Alert-Regel "{name}" gespeichert und auf alle Artikel angewendet.', "success")
        elif action == "delete_alert_rule":
            rid = request.form.get("rule_id")
            if rid:
                db.delete_alert_rule(int(rid))
                db.recheck_all_alerts()
                flash("Alert-Regel gelöscht.", "success")
        elif action == "toggle_alert_rule":
            rid = request.form.get("rule_id")
            if rid:
                db.toggle_alert_rule(int(rid))
                db.recheck_all_alerts()
        elif action == "save_api_key":
            key = request.form.get("openrouter_api_key", "").strip()
            if key:
                db.set_setting("openrouter_api_key", key)
            flash("Einstellungen gespeichert.", "success")
        elif action == "save_ai_models":
            model_settings = {
                "openrouter_model_article_fetch": request.form.get("article_fetch_model", "").strip(),
                "openrouter_model_article_summary": request.form.get("article_summary_model", "").strip(),
                "openrouter_model_daily_report": request.form.get("daily_report_model", "").strip(),
                "openrouter_model_assistant": request.form.get("assistant_model", "").strip(),
            }
            for key, value in model_settings.items():
                if value:
                    db.set_setting(key, value)
            flash("KI-Modelle gespeichert.", "success")
        elif action == "save_embedding_settings":
            key = request.form.get("openai_embedding_api_key", "").strip()
            model = request.form.get("openai_embedding_model", "").strip()
            base_url = request.form.get("openai_embedding_base_url", "").strip()
            if key:
                db.set_setting("openai_embedding_api_key", key)
            if model:
                db.set_setting("openai_embedding_model", model)
            db.set_setting("openai_embedding_base_url", base_url)
            flash("Embedding-Einstellungen gespeichert.", "success")
        elif action == "save_radar_sectors":
            raw = request.form.get("radar_preset_sectors", "")
            sectors = [s.strip() for s in raw.splitlines() if s.strip()]
            sector_setting = "\n".join(sectors)
            previous_sector_setting = db.get_setting("radar_preset_sectors", "")
            db.set_setting("radar_preset_sectors", sector_setting)
            cleared_count = 0
            if previous_sector_setting.strip() != sector_setting.strip():
                cleared_count = db.clear_article_radar_sectors()
            if sectors:
                flash(f"{len(sectors)} Radar-Sektoren gespeichert.", "success")
            else:
                flash("Sektoren-Vorgabe geleert – KI wählt Sektoren frei.", "success")
            if cleared_count:
                flash(
                    f"{cleared_count} gespeicherte Artikel-Sektorzuordnungen zurückgesetzt.",
                    "info",
                )
        return redirect(url_for("einstellungen"))

    keywords = db.get_keywords()
    return render_template(
        "einstellungen.html",
        keywords=keywords,
        categories={k: v for k, v in CATEGORIES.items() if k != "alle"},
        alert_rules=db.get_alert_rules(),
        ai_configured=ai.is_configured(),
        openrouter_key_saved=bool(db.get_setting("openrouter_api_key")),
        model_settings=ai.get_model_settings(),
        model_choices=ai.get_model_choices(),
        feature_models=ai.get_feature_models(),
        embedding_key_saved=bool(db.get_setting("openai_embedding_api_key") or os.environ.get("OPENAI_API_KEY")),
        embedding_model=ai.get_embedding_model(),
        embedding_base_url=db.get_setting("openai_embedding_base_url", "") or os.environ.get("OPENAI_EMBEDDING_BASE_URL", ""),
        radar_preset_sectors=ai.get_radar_preset_sectors(),
    )


# --- Template helpers ---

@app.template_filter("split_tags")
def split_tags_filter(value):
    if not value:
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


@app.template_filter("datum")
def datum_filter(value):
    if not value:
        return "–"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(value[:19], fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return value[:10]


@app.template_filter("source_logo")
def source_logo_filter(value):
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    domain = parsed.netloc or parsed.path
    domain = domain.lower().removeprefix("www.")
    if not domain:
        return ""
    return f"https://www.google.com/s2/favicons?domain={quote_plus(domain)}&sz=64"


@app.context_processor
def inject_globals():
    return {
        "CATEGORIES": CATEGORIES,
        "ai_configured": ai.is_configured(),
        "NO_FULLTEXT": _NO_FULLTEXT,
        "current_user": current_user(),
        "can_edit": can_edit(),
    }


# Run on startup regardless of how the app is launched (gunicorn or direct)
db.init_db()
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=port)
