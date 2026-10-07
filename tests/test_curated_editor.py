import unittest
from unittest import mock

from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup

import database as db


ARTICLE = {
    "id": 7, "workspace": "core", "title": 'Titel <script> & "Test"',
    "published_at": "2026-10-07 12:00:00", "fetched_at": "2026-10-06 09:00:00",
    "geschaeftsfeld": "Leben", "category": "markt", "tags": "test, markt",
    "radar_sector": "Vorsorge", "ai_summary": "Erste Zeile\n\n  Zweite Zeile",
    "ai_implications": "<b>Literal</b>\nNächste Zeile", "ai_generated": 1,
    "ai_model": "test-model", "source_name": "Test", "source_logo_ref": None,
    "url": "https://example.com", "full_text": "Not part of the edit response",
}


class CuratedEditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            mock.patch.object(db, "init_db"),
            mock.patch.object(db, "app_user_count", return_value=1),
            mock.patch.object(BackgroundScheduler, "start"),
        ):
            import app
        cls.module = app

    def render(self, role="editor", query="", articles=None):
        with (
            mock.patch.object(self.module, "current_user", return_value={"username": "test", "role": role}),
            mock.patch.object(db, "get_pinned_articles", return_value=articles or [ARTICLE]),
            mock.patch.object(db, "get_sources", return_value=[]),
            mock.patch.object(self.module.ai, "is_configured", return_value=True),
            mock.patch.object(self.module.ai, "get_radar_preset_sectors", return_value=["Vorsorge"]),
        ):
            response = self.module.app.test_client().get("/kuratierte-artikel?workspace=core" + query)
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.data, "html.parser")

    def test_rows_contain_visible_text_but_no_edit_controls(self):
        soup = self.render(articles=[ARTICLE, {**ARTICLE, "id": 8}])
        for row in soup.select("tr[id^=article-row-]"):
            self.assertFalse(row.select("input, select, textarea, .em"))
            self.assertIn(ARTICLE["title"], row.get_text())
            self.assertIn("Erste Zeile", row.get_text())
            self.assertIn("Zweite Zeile", row.get_text())
            self.assertEqual(row["data-edit-url"], f'/api/artikel/{row["id"].rsplit("-", 1)[-1]}/edit-fields')
        self.assertEqual(len(soup.select("template#curated-editor-fields")), 1)
        self.assertIsNone(soup.select_one("#article-row-7 script"))

    def test_read_only_page_omits_editor_templates_and_urls(self):
        soup = self.render(role="viewer")
        self.assertIsNone(soup.select_one("#curated-editor-fields"))
        self.assertIsNone(soup.select_one("[data-row-edit]"))
        self.assertNotIn("data-edit-url", soup.select_one("#article-row-7").attrs)
        lines = soup.select(".implications-text [data-text-line]")
        self.assertEqual([line.get_text() for line in lines], ["<b>Literal</b>", "Nächste Zeile"])

    def test_optional_sector_is_only_created_when_shown(self):
        self.assertIsNone(self.render().select_one('[data-edit-field="radar_sector"]'))
        soup = self.render(query="&internal_sector=1")
        self.assertIsNotNone(soup.select_one('[data-edit-field="radar_sector"]'))
        self.assertIsNotNone(soup.select_one('template select[name="radar_sector"] option[value="Vorsorge"]'))

    def get_fields(self, user, article=ARTICLE):
        with (
            mock.patch.object(self.module, "current_user", return_value=user),
            mock.patch.object(db, "get_article", return_value=article),
        ):
            return self.module.app.test_client().get("/api/artikel/7/edit-fields")

    def test_api_preserves_exact_values_without_sending_fulltext(self):
        response = self.get_fields({"role": "editor"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["fields"]["ai_summary"], ARTICLE["ai_summary"])
        self.assertEqual(response.json["fields"]["ai_implications"], ARTICLE["ai_implications"])
        self.assertEqual(response.json["fields"]["published_at"], "2026-10-07")
        self.assertEqual(response.json["fields"]["title"], ARTICLE["title"])
        self.assertNotIn("full_text", response.json["fields"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_sentinel_and_date_fallback_match_old_editor(self):
        response = self.get_fields({"role": "editor"}, {**ARTICLE, "ai_summary": db.NO_FULLTEXT, "published_at": None})
        self.assertEqual(response.json["fields"]["ai_summary"], "")
        self.assertEqual(response.json["fields"]["published_at"], "2026-10-06")

    def test_api_requires_login_and_workspace_edit_access(self):
        self.assertEqual(self.get_fields(None).status_code, 401)
        self.assertEqual(self.get_fields({"role": "viewer"}).status_code, 403)
        travel = {**ARTICLE, "workspace": "travel_health"}
        self.assertEqual(self.get_fields({"role": "editor", "travel_health_access": "none"}, travel).status_code, 403)
        self.assertEqual(self.get_fields({"role": "viewer", "travel_health_access": "viewer"}, travel).status_code, 403)
        self.assertEqual(self.get_fields({"role": "viewer", "travel_health_access": "editor"}, travel).status_code, 200)
        self.assertEqual(self.get_fields({"role": "admin"}, travel).status_code, 200)
        self.assertEqual(self.get_fields({"role": "editor"}, None).status_code, 404)

    def test_save_keeps_hidden_sector_and_normal_post_behavior(self):
        fields = self.get_fields({"role": "editor"}).json["fields"]
        del fields["radar_sector"]
        with (
            mock.patch.object(self.module, "current_user", return_value={"role": "editor"}),
            mock.patch.object(db, "get_article", return_value=ARTICLE),
            mock.patch.object(self.module.ai, "get_radar_preset_sectors", return_value=["Vorsorge"]),
            mock.patch.object(db, "update_article_manual_fields") as update,
            mock.patch.object(db, "set_article_tags") as tags,
            mock.patch.object(db, "delete_article_chunks"),
        ):
            response = self.module.app.test_client().post(
                "/artikel/7/update-fields", data=fields,
                headers={"Referer": "http://localhost/kuratierte-artikel?q=test"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/kuratierte-artikel?q=test"))
        self.assertFalse(update.call_args.kwargs["update_radar_sector"])
        self.assertEqual(update.call_args.kwargs["ai_summary"], ARTICLE["ai_summary"])
        self.assertEqual(update.call_args.kwargs["ai_implications"], ARTICLE["ai_implications"])
        tags.assert_called_once_with(7, ["test", "markt"])

    def test_compacting_preserves_escaped_content_and_text_spacing(self):
        markup = '<td>\n <div>Literal &lt;script&gt; &amp; text</div>\n <span>A B\nC</span>\n </td>'
        result = str(self.module.compact_table_markup(markup))
        self.assertEqual(result, '<td><div>Literal &lt;script&gt; &amp; text</div><span>A B\nC</span></td>')


if __name__ == "__main__":
    unittest.main()
