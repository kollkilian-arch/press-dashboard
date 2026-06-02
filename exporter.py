from datetime import datetime, timedelta
from flask import render_template
import database as db


CATEGORY_LABELS = {
    "eigene_produkte": "Eigene Produkte",
    "markt": "Markt",
    "wettbewerber": "Wettbewerber",
    "sonstige": "Sonstige",
}


def generate_pdf(days: int = 7):
    from weasyprint import HTML

    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    articles = db.get_articles(von=since, limit=500)

    grouped: dict[str, list] = {}
    for cat in CATEGORY_LABELS:
        grouped[cat] = [a for a in articles if a["category"] == cat]

    html_str = render_template(
        "digest_pdf.html",
        grouped=grouped,
        category_labels=CATEGORY_LABELS,
        days=days,
        generated=datetime.now().strftime("%d.%m.%Y %H:%M"),
    )
    return HTML(string=html_str).write_pdf()
