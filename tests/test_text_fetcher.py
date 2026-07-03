import unittest

from bs4 import BeautifulSoup

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


if __name__ == "__main__":
    unittest.main()
