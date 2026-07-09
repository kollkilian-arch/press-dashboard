import json
from datetime import date, datetime, timedelta
from flask import render_template
import database as db
import ai


def generate_report_pdf(mode: str, target_date: str):
    from weasyprint import HTML

    report_key = target_date if mode == "daily" else f"{target_date}_weekly"
    report_row = db.get_report(report_key)
    if not report_row:
        return None

    report = json.loads(report_row["content"])
    if mode == "daily":
        articles = db.get_articles_for_report(target_date)
        period_label = target_date
    else:
        articles = db.get_articles_for_week_report(target_date)
        end = date.fromisoformat(target_date)
        start = (end - timedelta(days=6)).isoformat()
        period_label = f"{start} bis {target_date}"

    report = ai.attach_report_references(report, articles, max_sources=report_row["article_count"])

    html_str = render_template(
        "bericht_pdf.html",
        report=report,
        mode=mode,
        date_param=target_date,
        period_label=period_label,
        report_row=report_row,
        generated=datetime.now().strftime("%d.%m.%Y %H:%M"),
    )
    return HTML(string=html_str).write_pdf()
