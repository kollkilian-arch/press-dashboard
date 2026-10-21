import unittest
from unittest import mock

from apscheduler.schedulers.background import BackgroundScheduler

import database as db


class ArticleImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            mock.patch.object(db, "init_db"),
            mock.patch.object(db, "app_user_count", return_value=1),
            mock.patch.object(BackgroundScheduler, "start"),
        ):
            import app
        cls.module = app

    def test_url_failure_shows_actual_reason_and_does_not_call_ai_or_save(self):
        reason = "Die HTTPS-Zertifikatsprüfung der Website ist fehlgeschlagen."
        with (
            mock.patch.object(self.module, "current_user", return_value={"role": "editor"}),
            mock.patch.object(db, "find_duplicate_article", return_value=None),
            mock.patch.object(db, "add_article") as save,
            mock.patch.object(self.module.text_fetcher, "fetch_article_details", return_value={
                "fetch_error": reason,
            }) as fetch,
            mock.patch.object(self.module.ai, "is_configured", return_value=True),
            mock.patch.object(self.module.ai, "extract_article_object") as extract,
            mock.patch.object(self.module.ai, "analyse_article_for_pin") as analyse,
        ):
            client = self.module.app.test_client()
            response = client.post("/artikel/add", data={"url": "https://example.com/article"})
            with client.session_transaction() as session:
                messages = session.get("_flashes", [])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages, [("danger", f"URL konnte nicht eingelesen werden: {reason}")])
        fetch.assert_called_once_with("https://example.com/article", include_error=True)
        extract.assert_not_called()
        analyse.assert_not_called()
        save.assert_not_called()

    def test_supplied_title_can_still_be_saved_with_a_fetch_warning(self):
        with (
            mock.patch.object(self.module, "current_user", return_value={"role": "editor"}),
            mock.patch.object(db, "find_duplicate_article", return_value=None),
            mock.patch.object(db, "add_article", return_value=(7, True)) as save,
            mock.patch.object(db, "get_article", return_value={"id": 7, "is_pinned": False}),
            mock.patch.object(db, "set_article_ignored"),
            mock.patch.object(db, "set_article_pinned"),
            mock.patch.object(self.module.text_fetcher, "fetch_article_details", return_value={
                "fetch_error": "Zeitüberschreitung beim Laden der Website.",
            }),
            mock.patch.object(self.module.ai, "is_configured", return_value=False),
        ):
            client = self.module.app.test_client()
            response = client.post("/artikel/add", data={
                "url": "https://example.com/article", "title": "Eigener Titel",
            })
            with client.session_transaction() as session:
                messages = session.get("_flashes", [])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(save.call_args.args[0], "Eigener Titel")
        self.assertIn(("warning", "URL konnte nicht eingelesen werden: Zeitüberschreitung beim Laden der Website."), messages)


if __name__ == "__main__":
    unittest.main()
