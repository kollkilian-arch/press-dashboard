import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from text_fetcher import _extract_published_at, fetch_article_details, fetch_full_text


class FakeResponse:
    def __init__(self, text, url, content_type="text/html; charset=utf-8"):
        self.text = text
        self.url = url
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        return None


class PublishedDateExtractionTest(unittest.TestCase):
    def test_prefers_article_json_ld_over_related_time_tags(self):
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "NewsArticle",
                "headline": "Zurich erweitert Berufsunfähigkeitsschutz",
                "datePublished": "2026-07-03 06:25:00 +02:00"
              }
            </script>
          </head>
          <body>
            <h1>Zurich erweitert Berufsunfähigkeitsschutz</h1>
            <section>
              <h2>Lesen Sie auch</h2>
              <time datetime="2017-05-31 05:05:10+02:00">31.05.2017</time>
            </section>
          </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(_extract_published_at(soup), "2026-07-03 06:25:00")

    def test_reads_german_visible_publication_date_near_heading(self):
        html = """
        <html>
          <body>
            <div class="opener_content">
              <h1>Zurich erweitert Berufsunfähigkeitsschutz</h1>
              <div class="date">Veröffentlichung: 03.07.2026, 06:07 Uhr</div>
            </div>
          </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(_extract_published_at(soup), "2026-07-03 06:07:00")


class ArticleFetchQualityTest(unittest.TestCase):
    def test_rejects_consent_redirect_as_fulltext(self):
        html = """
        <html>
          <head>
            <title>FONDS professionell</title>
            <meta name="description" content="Willkommen bei FONDS professionell">
          </head>
          <body>
            <main>
              <h1>Willkommen bei FONDS professionell</h1>
              <p>Nutzen Sie fondsprofessionell.de mit Ihrer Zustimmung zur Verwendung von Cookies
              fuer Webanalyse und Werbemassnahmen.</p>
              <p>Datenschutzinformation: Zur Bereitstellung unserer Dienste nutzen wir Technologien
              von Partnern (4). Personenbezogene Daten werden fuer Zwecke der Datenverarbeitung
              verarbeitet.</p>
              <p>Speichern von oder Zugriff auf Informationen auf einem Endgeraet. Auswahl
              akzeptieren, alles akzeptieren oder nichts akzeptieren.</p>
              <p>Wie gewohnt mit Werbung lesen oder werbefrei lesen.</p>
            </main>
          </body>
        </html>
        """
        final_url = "https://www.fondsprofessionell.de/consent/?url=/versicherungen/news/foo/"

        with patch("text_fetcher.requests.get", return_value=FakeResponse(html, final_url)):
            details = fetch_article_details("https://www.fondsprofessionell.de/versicherungen/news/foo/")

        self.assertEqual(details["fetch_status"], "blocked_by_consent")
        self.assertEqual(details["fetch_reason"], "consent_or_legal_interstitial")
        self.assertIsNone(details["full_text"])
        self.assertEqual(details["title"], "")

    def test_does_not_reject_real_article_with_article_schema(self):
        html = """
        <html>
          <head>
            <title>Private Krankenversicherung: Preisspruenge nehmen weiter zu</title>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "NewsArticle",
                "headline": "Private Krankenversicherung: Preisspruenge nehmen weiter zu",
                "datePublished": "2026-07-13T08:00:00+02:00"
              }
            </script>
          </head>
          <body>
            <article>
              <h1>Private Krankenversicherung: Preisspruenge nehmen weiter zu</h1>
              <p>Die private Krankenversicherung verzeichnet erneut deutliche Beitragsanpassungen.
              Nach Angaben aus dem Markt steigen die Praemien in mehreren Tarifgenerationen.</p>
              <p>Cookies werden nur in einem kurzen Footer-Hinweis erwaehnt und sind nicht der
              Hauptinhalt dieses Artikels.</p>
            </article>
          </body>
        </html>
        """
        url = "https://www.fondsprofessionell.de/versicherungen/news/headline/private-krankenversicherung-preisspruenge-nehmen-weiter-zu-252093/"

        with patch("text_fetcher.requests.get", return_value=FakeResponse(html, url)):
            details = fetch_article_details(url)

        self.assertEqual(details["fetch_status"], "ok")
        self.assertIn("private Krankenversicherung", details["full_text"])

    def test_fetch_full_text_returns_none_for_blocked_consent_page(self):
        html = """
        <html><body><main>
          <p>Datenschutzeinstellungen Cookies personenbezogene Daten Partnern (4)
          Zwecke der Datenverarbeitung Auswahl akzeptieren alles akzeptieren
          nichts akzeptieren personalisierte Werbung.</p>
        </main></body></html>
        """
        final_url = "https://www.fondsprofessionell.de/consent/?url=/foo/"

        with patch("text_fetcher.requests.get", return_value=FakeResponse(html, final_url)):
            full_text = fetch_full_text("https://www.fondsprofessionell.de/foo/")

        self.assertIsNone(full_text)

    def test_sends_cookie_header_when_configured(self):
        html = """
        <html><body><article>
          <p>Dies ist ein ausreichend langer Artikeltext mit relevanten Informationen
          zur privaten Krankenversicherung und zu Beitragsanpassungen im Markt.</p>
        </article></body></html>
        """
        captured = {}

        def fake_get(*args, **kwargs):
            captured.update(kwargs)
            return FakeResponse(html, "https://example.test/news/article")

        with patch("text_fetcher.requests.get", side_effect=fake_get):
            fetch_article_details("https://example.test/news/article", cookie_header="session=abc; consent=yes")

        self.assertEqual(captured["headers"]["Cookie"], "session=abc; consent=yes")


if __name__ == "__main__":
    unittest.main()
