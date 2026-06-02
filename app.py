import os
import json
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


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(_fetch_job, "interval", hours=4, id="auto_fetch")


# --- Routes ---

@app.route("/")
def dashboard():
    category     = request.args.get("kategorie", "alle")
    search       = request.args.get("q", "").strip()
    von          = request.args.get("von", "")
    bis          = request.args.get("bis", "")
    tag          = request.args.get("tag", "").strip()
    priority     = request.args.get("prio", "").strip()
    alerted_only = request.args.get("alerts") == "1"

    articles = db.get_articles(
        category=category if category != "alle" else None,
        search=search or None,
        von=von or None,
        bis=bis or None,
        tag=tag or None,
        priority=priority or None,
        alerted_only=alerted_only,
    )
    unread = db.count_unread()
    all_tags = db.get_all_tags()
    alert_count = len(db.get_articles(alerted_only=True, limit=500))
    return render_template(
        "dashboard.html",
        articles=articles,
        categories=CATEGORIES,
        active_category=category,
        unread=unread,
        search=search,
        von=von,
        bis=bis,
        active_tag=tag,
        all_tags=all_tags,
        active_prio=priority,
        alerted_only=alerted_only,
        alert_count=alert_count,
    )


@app.route("/artikel/<int:article_id>")
def artikel(article_id):
    article = db.get_article(article_id)
    if article is None:
        flash("Artikel nicht gefunden.", "warning")
        return redirect(url_for("dashboard"))
    db.mark_read(article_id)
    return render_template("artikel.html", article=article, categories=CATEGORIES)


@app.route("/artikel/add", methods=["POST"])
def add_artikel():
    title = request.form.get("title", "").strip()
    url = request.form.get("url", "").strip()
    source_name = request.form.get("source_name", "Manuell").strip()
    content_snippet = request.form.get("content_snippet", "").strip()[:500]
    category = request.form.get("category", "sonstige")
    published_at = request.form.get("published_at", "").strip() or None

    raw_tags = request.form.get("tags", "")
    tags = [t.strip().lower() for t in raw_tags.split(",") if t.strip()]

    if not title:
        flash("Titel ist erforderlich.", "danger")
        return redirect(url_for("dashboard"))

    db.add_article(title, url or None, source_name, content_snippet, category, published_at, tags=tags)
    flash("Artikel wurde hinzugefügt.", "success")
    return redirect(url_for("dashboard"))


@app.route("/artikel/<int:article_id>/analyse", methods=["POST"])
def analyse_artikel(article_id):
    article = db.get_article(article_id)
    if article is None:
        flash("Artikel nicht gefunden.", "warning")
        return redirect(url_for("dashboard"))

    # Try to fetch the full article text from the source URL
    full_text = None
    if article["url"]:
        full_text = text_fetcher.fetch_full_text(article["url"])

    text_for_ai = full_text or article["content_snippet"] or ""
    source_label = "Volltext von Quelle" if full_text else "RSS-Vorschau"

    try:
        result = ai.analyse_article(article["title"], text_for_ai)
        db.update_article_ai(
            article_id,
            result["summary"],
            result["category"],
            result.get("priority"),
        )
        db.set_article_tags(article_id, result["tags"])
        categorizer.invalidate()
        flash(f"KI-Analyse abgeschlossen ({source_label} gelesen).", "success")
    except Exception as e:
        flash(f"KI-Analyse fehlgeschlagen: {e}", "danger")
    return redirect(request.referrer or url_for("artikel", article_id=article_id))


@app.route("/artikel/<int:article_id>/pin", methods=["POST"])
def pin_artikel(article_id):
    db.toggle_pin(article_id)
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/artikel/<int:article_id>/loeschen", methods=["POST"])
def delete_artikel(article_id):
    db.delete_article(article_id)
    flash("Artikel wurde gelöscht.", "success")
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
    from datetime import datetime, date as date_type
    today = date_type.today().isoformat()
    report_row = db.get_report(today)
    report = json.loads(report_row["content"]) if report_row else None
    recent = db.get_recent_reports(limit=7)
    articles_today = db.get_articles_for_report(today)
    return render_template(
        "bericht.html",
        report=report,
        report_row=report_row,
        recent=recent,
        today=today,
        article_count=len(articles_today),
    )


@app.route("/bericht/erstellen", methods=["POST"])
def bericht_erstellen():
    from datetime import date as date_type
    target_date = request.form.get("date", date_type.today().isoformat())
    articles = db.get_articles_for_report(target_date)
    if not articles:
        flash("Keine Artikel fuer diesen Tag gefunden. Bitte zuerst Quellen aktualisieren.", "warning")
        return redirect(url_for("bericht"))
    try:
        result = ai.generate_daily_report(articles, target_date)
        db.save_report(target_date, json.dumps(result, ensure_ascii=False), len(articles))
        flash(f"Tagesbericht fuer {target_date} erstellt ({len(articles)} Artikel analysiert).", "success")
    except Exception as e:
        flash(f"Bericht-Erstellung fehlgeschlagen: {e}", "danger")
    return redirect(url_for("bericht"))


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
            model = request.form.get("openrouter_model", "").strip()
            if key:
                db.set_setting("openrouter_api_key", key)
            if model:
                db.set_setting("openrouter_model", model)
            flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("einstellungen"))

    keywords = db.get_keywords()
    return render_template(
        "einstellungen.html",
        keywords=keywords,
        categories={k: v for k, v in CATEGORIES.items() if k != "alle"},
        alert_rules=db.get_alert_rules(),
        ai_configured=ai.is_configured(),
        openrouter_key_saved=bool(db.get_setting("openrouter_api_key")),
        openrouter_model=db.get_setting("openrouter_model") or ai.DEFAULT_MODEL,
        available_models=ai.AVAILABLE_MODELS,
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


@app.context_processor
def inject_globals():
    pinned = db.get_pinned_articles()
    return {
        "CATEGORIES": CATEGORIES,
        "ai_configured": ai.is_configured(),
        "g_pinned": pinned,
    }


# Run on startup regardless of how the app is launched (gunicorn or direct)
db.init_db()
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=port)
