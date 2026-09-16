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


class FondsMobileFetchTest(unittest.TestCase):
    desktop_url = (
        "https://www.fondsprofessionell.de/news/unternehmen/headline/"
        "guenstigere-depots-per-app-volksbanken-steigen-in-preiskampf-ein-253600/"
    )
    mobile_url = "https://m.fondsprofessionell.de/newssingle.php?uid=253600&rd=1"
    html = """
        <html><head><title>Mobile Website</title></head><body>
          <nav>Navigation</nav>
          <div data-role="content">
            <div class="singleNewsContainer">
              <h3 class="mB6">15.09.2026, 10:15</h3>
              <h1>Günstigere Depots per App</h1>
              <div class="newsContentWrapper">
                <h3>Einleitung zum neuen Depotangebot.</h3>
                <div class="teaserWrapper"><p>Bildrechte und Bildunterschrift</p></div>
                <div class="newsContent">
                  <p>Der Artikel erklärt das neue Depotangebot der Volksbanken.</p>
                  <p>Auch kurze Absätze.</p>
                  <script>tracking()</script>
                  <div class="ad-banner">Werbung</div>
                </div>
              </div>
            </div>
            <h1>Weitere Artikel:</h1><p>Ein ganz anderer Artikel mit fremdem Inhalt.</p>
          </div>
        </body></html>
    """

    def response(self, html=None, url=None):
        return mock.Mock(
            status_code=200, headers={"Content-Type": "text/html; charset=utf-8"},
            text=self.html if html is None else html, url=url or self.mobile_url,
        )

    def test_fetches_mobile_copy_with_metadata_and_without_page_noise(self):
        with mock.patch.object(text_fetcher.requests, "get", return_value=self.response()) as get:
            result = text_fetcher.fetch_article_details(self.desktop_url, include_error=True)

        get.assert_called_once_with(
            self.mobile_url, headers=text_fetcher.HEADERS, timeout=15, allow_redirects=True,
        )
        self.assertEqual(result["title"], "Günstigere Depots per App")
        self.assertEqual(result["published_at"], "2026-09-15 10:15:00")
        self.assertEqual(result["source_name"], "FONDS professionell")
        self.assertEqual(result["content_snippet"], "Einleitung zum neuen Depotangebot.")
        self.assertEqual(result["content_kind"], "fulltext")
        self.assertEqual(result["full_text"], (
            "Einleitung zum neuen Depotangebot. "
            "Der Artikel erklärt das neue Depotangebot der Volksbanken. Auch kurze Absätze."
        ))

    def test_mobile_links_always_get_rd_one_and_keep_article_id(self):
        for query in ("uid=253600", "uid=253600&rd=0", "rd=1&uid=253600&cp=&nt=0"):
            with self.subTest(query=query):
                self.assertEqual(text_fetcher._fonds_mobile_url(
                    "https://m.fondsprofessionell.de/newssingle.php?" + query
                ), self.mobile_url)
        self.assertEqual(text_fetcher._fonds_mobile_url(
            self.desktop_url + "?utm_source=newsletter#artikel"
        ), self.mobile_url)

    def test_leaves_other_domains_and_non_article_urls_unchanged(self):
        for url in (
            "https://www.fondsprofessionell.de/",
            "https://www.fondsprofessionell.de/news/unternehmen/",
            self.desktop_url.replace(".de/", ".de.example.org/"),
            self.desktop_url.replace(".de/", ".at/"),
            "https://m.fondsprofessionell.de/newssingle.php?uid=abc",
            "https://m.fondsprofessionell.de/newssingle.php?uid=0",
            "https://m.fondsprofessionell.de/newssingle.php?uid=1&uid=2",
        ):
            with self.subTest(url=url):
                self.assertIsNone(text_fetcher._fonds_mobile_url(url))

    def test_does_not_treat_consent_or_missing_article_as_full_text(self):
        for response in (
            self.response(html="<main><h1>Einwilligung</h1><p>Cookies akzeptieren</p></main>"),
            self.response(url="https://www.fondsprofessionell.de/consent/?url=/"),
            self.response(url="https://m.fondsprofessionell.de/newssingle.php?uid=999&rd=1"),
            self.response(html=self.html.replace('class="newsContent"', 'class="missing"')),
        ):
            with self.subTest(response=response):
                with mock.patch.object(text_fetcher.requests, "get", return_value=response):
                    result = text_fetcher.fetch_article_details(self.desktop_url, include_error=True)
                self.assertIn("fetch_error", result)
                self.assertNotIn("full_text", result)

    def test_full_text_callers_use_mobile_copy_and_respect_length_limit(self):
        with mock.patch.object(text_fetcher.requests, "get", return_value=self.response()):
            result = text_fetcher.fetch_full_text(self.desktop_url, max_chars=20)
        self.assertEqual(result, "Einleitung zum neuen")

    def test_network_failure_preserves_manual_fallback(self):
        with mock.patch.object(text_fetcher.requests, "get", side_effect=requests.Timeout):
            self.assertEqual(text_fetcher.fetch_article_details(self.desktop_url), {})


if __name__ == "__main__":
    unittest.main()
