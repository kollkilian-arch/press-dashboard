import unittest
from unittest import mock

from bs4 import BeautifulSoup
import requests

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
        self.assertEqual(result["content_kind"], "teaser")
        self.assertEqual(result["title"], "Risikoleben neu aufgestellt")
        self.assertEqual(result["source_name"], "Versicherungsbote")

    def test_marks_a_saved_full_text_as_fallback_full_text(self):
        stored = {"full_text": "F" * 600, "content_snippet": "S" * 350}

        result = text_fetcher.merge_stored_article_fallback({}, stored)

        self.assertEqual(result["content_kind"], "stored_fulltext")


class FetchFailureTest(unittest.TestCase):
    def test_retries_a_403_once_with_browser_compatible_client(self):
        blocked_response = mock.Mock(status_code=403)
        blocked_error = requests.HTTPError()
        blocked_error.response = blocked_response
        blocked_response.raise_for_status.side_effect = blocked_error
        browser_response = mock.Mock(
            status_code=200,
            headers={"Content-Type": "text/html"},
            text="""
                <html><head><title>Artikel</title></head><body><article><p>
                Dieser ausreichend lange Artikeltext wird nach dem Browser-Abruf vollständig
                verarbeitet und enthält deutlich mehr als die erforderliche Mindestlänge.
                </p></article></body></html>
            """,
            url="https://example.com/article",
        )
        browser_client = mock.Mock()
        browser_client.get.return_value = browser_response

        with (
            mock.patch.object(text_fetcher.requests, "get", return_value=blocked_response),
            mock.patch.object(text_fetcher, "browser_requests", browser_client),
        ):
            result = text_fetcher.fetch_article_details(
                "https://example.com/article", include_error=True
            )

        self.assertEqual(result["content_kind"], "fulltext")
        self.assertIn("ausreichend lange Artikeltext", result["full_text"])
        browser_client.get.assert_called_once_with(
            "https://example.com/article",
            headers=text_fetcher.HEADERS,
            timeout=15,
            allow_redirects=True,
            impersonate="chrome",
        )

    def test_returns_auditable_timeout_reason_when_requested(self):
        with mock.patch.object(text_fetcher.requests, "get", side_effect=requests.Timeout):
            result = text_fetcher.fetch_article_details(
                "https://example.com/article", include_error=True
            )

        self.assertEqual(
            result,
            {"fetch_error": "Zeitüberschreitung beim Laden der Website."},
        )

    def test_keeps_legacy_empty_result_without_error_opt_in(self):
        with mock.patch.object(text_fetcher.requests, "get", side_effect=requests.Timeout):
            result = text_fetcher.fetch_article_details("https://example.com/article")

        self.assertEqual(result, {})

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


class MainTextExtractionTest(unittest.TestCase):
    def test_keeps_article_intro_inside_article_header(self):
        intro = (
            "Seit Juli 2026 bietet die LV 1871 ein rundum erneuertes Konzept "
            "für die Risikolebensversicherung."
        )
        body = (
            "Das Beitragsniveau wurde flächendeckend gesenkt und der "
            "Abschlussprozess deutlich verbessert."
        )
        html = f"""
        <html>
          <body>
            <header><p>Seitennavigation mit einem ausreichend langen Blindtext.</p></header>
            <article>
              <header class="article-header"><p class="intro">{intro}</p></header>
              <div class="post_chapter"><p>{body}</p></div>
              <footer><p>Autorenhinweis mit einem ausreichend langen Blindtext.</p></footer>
            </article>
          </body>
        </html>
        """

        result = text_fetcher._extract_main_text(BeautifulSoup(html, "html.parser"))

        self.assertEqual(result, f"{intro} {body}")

    def test_removes_page_header_when_no_article_container_exists(self):
        navigation = "Seitennavigation mit einem ausreichend langen Blindtext."
        body = "Der eigentliche Beitrag steht direkt innerhalb des Body-Elements."
        html = f"""
        <html>
          <body>
            <header><p>{navigation}</p></header>
            <div><p>{body}</p></div>
          </body>
        </html>
        """

        result = text_fetcher._extract_main_text(BeautifulSoup(html, "html.parser"))

        self.assertEqual(result, body)


if __name__ == "__main__":
    unittest.main()
