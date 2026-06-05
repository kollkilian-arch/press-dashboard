import os
import json
from urllib.parse import quote_plus, urlparse
from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import database as db
import categorizer
import exporter
import ai
import text_fetcher
from fetchers import rss as rss_fetcher, scraper as scraper_fetcher

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

_NO_FULLTEXT = db.NO_FULLTEXT   # shared sentinel – defined once in database.py

CATEGORIES = {
    "alle":            "Alle",
    "eigene_produkte": "Eigene Produkte",
    "markt":           "Markt",
    "wettbewerber":    "Wettbewerber",
    "sonstige":        "Sonstige",
}


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

@app.route("/")
def dashboard():
    """Pinned-articles table — the curated dashboard."""
    search         = request.args.get("q", "").strip()
    tag            = request.args.get("tag", "").strip()
    von            = request.args.get("von", "")
    bis            = request.args.get("bis", "")
    source_id      = request.args.get("quelle", "").strip()
    geschaeftsfeld = request.args.get("gf", "").strip()

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
    return render_template(
        "dashboard.html",
        articles=articles,
        categories=CATEGORIES,
        sources=sources,
        search=search,
        active_tag=tag,
        von=von,
        bis=bis,
        active_source=source_id,
        active_gf=geschaeftsfeld,
        is_filtered=is_filtered,
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

    articles = db.get_articles(
        category=category if category != "alle" else None,
        search=search or None,
        von=von or None,
        bis=bis or None,
        tag=tag or None,
        source_id=int(source_id) if source_id.isdigit() else None,
        priority=priority or None,
        alerted_only=alerted_only,
    )
    unread = db.count_unread()
    all_tags = db.get_all_tags()
    sources = db.get_sources(active_only=True)
    alert_count = len(db.get_articles(alerted_only=True, limit=500))
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
    )


@app.route("/artikel/<int:article_id>")
def artikel(article_id):
    article = db.get_article(article_id)
    if article is None:
        flash("Artikel nicht gefunden.", "warning")
        return redirect(url_for("newsfeed"))
    db.mark_read(article_id)
    return render_template("artikel.html", article=article, categories=CATEGORIES)


@app.route("/artikel/add", methods=["POST"])
def add_artikel():
    title = request.form.get("title", "").strip()
    url = request.form.get("url", "").strip()
    source_name = request.form.get("source_name", "").strip()
    content_snippet = request.form.get("content_snippet", "").strip()[:500]
    category = request.form.get("category", "sonstige")
    published_at = request.form.get("published_at", "").strip() or None

    raw_tags = request.form.get("tags", "")
    tags = [t.strip().lower() for t in raw_tags.split(",") if t.strip()]

    fetched = {}
    fetch_warning = None
    if url and (not title or not source_name or not content_snippet or not published_at):
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

    article_id = db.add_article(title, url or None, source_name, content_snippet, category, published_at, tags=tags)
    if url and article_id and ai.is_configured():
        text_for_ai = fetched.get("full_text") or content_snippet or ""
        try:
            result = ai.analyse_article(title, text_for_ai)
            db.update_article_ai(
                article_id,
                result["summary"],
                result["category"],
                result.get("priority"),
                result.get("model_used"),
            )
            db.set_article_tags(article_id, result["tags"])
            categorizer.invalidate()
            flash("Artikel wurde aus der URL geladen und per KI analysiert.", "success")
        except Exception as e:
            flash(f"Artikel wurde hinzugefügt, KI-Analyse fehlgeschlagen: {e}", "warning")
    else:
        flash("Artikel wurde hinzugefügt.", "success")
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
        )
        db.set_article_tags(article_id, result["tags"])
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

    # Pinning (not unpinning) → fetch fulltext, then run AI
    if not currently_pinned:
        full_text = None
        if article["url"]:
            try:
                full_text = text_fetcher.fetch_full_text(article["url"])
            except Exception:
                pass

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
                )
                db.set_article_tags(article_id, result["tags"])
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
            )
            if ai.is_configured():
                flash(
                    "Volltext konnte nicht geladen werden – "
                    "keine KI-Zusammenfassung möglich. "
                    "Du kannst die Felder im Dashboard manuell ausfüllen.",
                    "info",
                )

    db.toggle_pin(article_id)
    return redirect(request.referrer or url_for("newsfeed"))


@app.route("/artikel/<int:article_id>/loeschen", methods=["POST"])
def delete_artikel(article_id):
    db.delete_article(article_id)
    flash("Artikel wurde gelöscht.", "success")
    return redirect(request.referrer or url_for("newsfeed"))


@app.route("/artikel/<int:article_id>/update-fields", methods=["POST"])
def update_artikel_fields(article_id):
    """Save manually-edited dashboard fields for a pinned article."""
    ai_summary      = request.form.get("ai_summary", "").strip()
    ai_implications = request.form.get("ai_implications", "").strip()
    tags_raw        = request.form.get("tags", "").strip()
    geschaeftsfeld  = request.form.get("geschaeftsfeld", "").strip()
    category        = request.form.get("category", "").strip()

    if geschaeftsfeld not in ("Leben", "Kranken", "Sonstiges"):
        geschaeftsfeld = None
    if category not in CATEGORIES or category == "alle":
        category = None

    tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]

    db.update_article_manual_fields(
        article_id,
        ai_summary=ai_summary or None,
        ai_implications=ai_implications or None,
        geschaeftsfeld=geschaeftsfeld,
        category=category,
    )
    db.set_article_tags(article_id, tags)
    return redirect(url_for("dashboard"))


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
            }
            for key, value in model_settings.items():
                if value:
                    db.set_setting(key, value)
            flash("KI-Modelle gespeichert.", "success")
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
    pinned = db.get_pinned_articles()
    return {
        "CATEGORIES": CATEGORIES,
        "ai_configured": ai.is_configured(),
        "g_pinned": pinned,
        "NO_FULLTEXT": _NO_FULLTEXT,
    }


# Run on startup regardless of how the app is launched (gunicorn or direct)
db.init_db()
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=port)
