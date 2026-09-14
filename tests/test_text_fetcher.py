import unittest

from bs4 import BeautifulSoup

import text_fetcher
from text_fetcher import _extract_published_at


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


class StoredArticleFallbackTest(unittest.TestCase):
    def test_uses_newsfeed_snippet_when_live_fetch_has_no_body(self):
        stored = {
            "title": "Risikoleben neu aufgestellt",
            "source_name": "Versicherungsbote",
            "published_at": "2026-09-14 00:00:00",
            "content_snippet": "A" * 350,
            "full_text": "",
        }

        result = text_fetcher.merge_stored_article_fallback({}, stored)

        self.assertEqual(result["full_text"], "A" * 350)
        self.assertEqual(result["title"], "Risikoleben neu aufgestellt")
        self.assertEqual(result["source_name"], "Versicherungsbote")

    def test_keeps_good_live_body_and_only_fills_missing_metadata(self):
        fetched = {"title": "Live-Titel", "full_text": "L" * 600}
        stored = {
            "title": "Alter Titel",
            "source_name": "Versicherungsbote",
            "content_snippet": "S" * 350,
            "full_text": "",
        }

        result = text_fetcher.merge_stored_article_fallback(fetched, stored)

        self.assertEqual(result["title"], "Live-Titel")
        self.assertEqual(result["full_text"], "L" * 600)
        self.assertEqual(result["source_name"], "Versicherungsbote")


if __name__ == "__main__":
    unittest.main()
