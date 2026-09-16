import json
import re
import unittest
from unittest import mock

import database as db
from fetchers import fonds, scraper


def item(uid, title="Ein neuer Artikel", query=""):
    return (f'<li class="li-news-list-view"><a class="a-news-list-view" '
            f'href="newssingle.php?uid={uid}{query}"><h2>{title}</h2>'
            '<p>Ein Teaser zum Artikel.</p></a></li>')


def page(items, next_page=None):
    return ('<ul class="ul-news-list-view">' + items + '</ul>' +
            (f'<a href="/news.php?cp={next_page}">Weitere News</a>' if next_page else ''))


class FondsSourceTest(unittest.TestCase):
    source = {"id": 7, "name": "FONDS professionell", "type": "scraper",
              "category_hint": "markt", "workspace": "core",
              "scraper_config": '{"adapter":"fonds_mobile","max_pages":2}'}

    def test_parses_dates_links_and_follows_pagination(self):
        html = page('<li class="li-news-list-divider">Dienstag, 15. Sep. 2026</li>' +
                    item(253600, query="&nt=0&cp=1"), 2)
        entries, next_page = fonds._parse_page(html)
        self.assertEqual(next_page, 2)
        self.assertEqual(entries[0]["url"], 'https://m.fondsprofessionell.de/newssingle.php?uid=253600&rd=1')
        self.assertEqual(entries[0]["published_at"], '2026-09-15 00:00:00')
        self.assertEqual(entries[0]["content_snippet"], 'Ein Teaser zum Artikel.')

    def test_top_news_do_not_inherit_unrelated_date(self):
        entries, _ = fonds._parse_page(page(item(1)) + page(
            '<li class="li-news-list-divider">14. Sep. 2026</li>' + item(2)))
        self.assertIsNone(entries[0]["published_at"])
        self.assertEqual(entries[1]["published_at"], '2026-09-14 00:00:00')

    def test_rejects_consent_page_instead_of_reporting_success(self):
        with mock.patch.object(fonds.requests, 'get', return_value=mock.Mock(text='<h1>Consent</h1>')):
            with mock.patch.object(db, 'update_last_fetched') as update:
                with self.assertRaises(ValueError):
                    fonds.fetch_source(self.source, {})
        update.assert_not_called()

    def test_automatic_scraper_dispatch_pagination_and_repeat_import(self):
        first = page(item(1) + item(1, query='&nt=1'), 2)
        second = page('<li class="li-news-list-divider">15. Sep. 2026</li>' + item(1) + item(2), 3)
        responses = [mock.Mock(text=first), mock.Mock(text=second)]
        with (
            mock.patch.object(fonds.requests, 'get', side_effect=responses * 2) as get,
            mock.patch.object(db, 'get_sources', return_value=[self.source]),
            mock.patch.object(db, 'add_article', side_effect=[(1, True), (2, True), (1, False), (2, False)]) as add,
            mock.patch.object(db, 'get_db'),
            mock.patch.object(db, 'check_and_alert') as alert,
            mock.patch.object(db, 'update_last_fetched') as update,
            mock.patch.object(fonds, 'classify', return_value='markt'),
            mock.patch.object(fonds.text_fetcher, 'fetch_article_details') as details,
        ):
            self.assertEqual(scraper.fetch_all(), 2)
            self.assertEqual(scraper.fetch_all(), 0)
        self.assertEqual(get.call_count, 4)
        self.assertTrue(all('&rd=1' in call.args[0] for call in get.call_args_list))
        self.assertEqual(add.call_count, 4)
        self.assertEqual(alert.call_count, 2)
        self.assertEqual(update.call_count, 2)
        details.assert_not_called()
        self.assertEqual(add.call_args_list[0].args[5], '2026-09-15 00:00:00')

    def test_top_news_reuses_stored_date_or_fetches_missing_metadata(self):
        with (
            mock.patch.object(fonds.requests, 'get', return_value=mock.Mock(text=page(item(1) + item(2)))),
            mock.patch.object(db, 'find_duplicate_article', side_effect=[{"published_at": "2026-09-14 12:00:00"}, None]),
            mock.patch.object(fonds.text_fetcher, 'fetch_article_details', return_value={
                "published_at": "2026-09-15 10:15:00", "content_snippet": "Vollständige Einleitung",
            }) as details,
            mock.patch.object(db, 'add_article', return_value=(1, False)) as add,
            mock.patch.object(db, 'update_last_fetched'),
            mock.patch.object(fonds, 'classify', return_value='markt'),
        ):
            fonds.fetch_source(self.source, {})
        details.assert_called_once()
        self.assertEqual(add.call_args_list[0].args[5], '2026-09-14 12:00:00')
        self.assertEqual(add.call_args_list[1].args[5], '2026-09-15 10:15:00')

    def test_source_is_registered_as_managed_scraper(self):
        source, = db.MANAGED_SOURCE_MIGRATIONS['sources_fonds_mobile_20260916']
        self.assertEqual(source[2], 'scraper')
        self.assertEqual(json.loads(source[4])['adapter'], 'fonds_mobile')


class FondsDuplicateTest(unittest.TestCase):
    desktop = 'https://www.fondsprofessionell.de/news/unternehmen/headline/depot-253600/'
    mobile = 'https://m.fondsprofessionell.de/newssingle.php?uid=253600&rd=1'

    def test_desktop_mobile_and_tracking_variants_have_same_identity(self):
        for url in (self.desktop, self.desktop + '?utm_source=test', self.mobile,
                    self.mobile.replace('&rd=1', '&nt=1&cp=2')):
            self.assertEqual(db.normalize_article_url(url), self.mobile)

    def test_finds_legacy_desktop_record_even_with_changed_title(self):
        conn = mock.Mock()
        existing = {'id': 42, 'url': self.desktop, 'title': 'Alter Titel'}
        conn.execute.return_value.fetchone.side_effect = [None, existing]
        result = db._find_duplicate_article(conn, 'Neuer Titel', self.mobile)
        self.assertEqual(result, existing)
        pattern = conn.execute.call_args.args[1][0]
        for url in (self.desktop, self.desktop + '?utm_source=test', self.mobile,
                    self.mobile.replace('?uid', '?cp=1&uid')):
            self.assertRegex(url, pattern)
        for url in (self.desktop.replace('253600', '1253600'),
                    self.mobile.replace('253600', '2536001'),
                    self.desktop.replace('.de/', '.de.example.org/')):
            self.assertIsNone(re.search(pattern, url))


if __name__ == '__main__':
    unittest.main()
