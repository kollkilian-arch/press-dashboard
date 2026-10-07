import unittest
from unittest import mock

from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup

import database as db


TABLE = ('<table><caption>Vergleich</caption><thead><tr><th scope="col">Produkt</th>'
         '<th scope="col">Beitrag</th></tr></thead><tbody><tr><td>Basis</td>'
         '<td><strong>25 €</strong></td></tr></tbody></table>')


class TopicEditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            mock.patch.object(db, "init_db"),
            mock.patch.object(db, "app_user_count", return_value=1),
            mock.patch.object(BackgroundScheduler, "start"),
        ):
            import app
        cls.module = app

    def test_table_survives_saving_and_rendering(self):
        cleaned = self.module._clean_topic_html(TABLE)
        self.assertEqual(cleaned, TABLE)
        self.assertEqual(str(self.module.topic_html_filter(cleaned)), TABLE)

    def test_pasted_merged_cells_keep_safe_structure(self):
        html = ('<table style="width:900px" onclick="alert(1)"><tbody><tr>'
                '<th scope="row" rowspan="2" style="color:red">A</th>'
                '<td colspan="2" onmouseover="alert(1)">B<script>alert(1)</script></td>'
                '</tr></tbody></table>')
        soup = BeautifulSoup(self.module._clean_topic_html(html), "html.parser")
        self.assertEqual(soup.th.attrs, {"scope": "row", "rowspan": "2"})
        self.assertEqual(soup.td.attrs, {"colspan": "2"})
        self.assertEqual(soup.table.attrs, {})
        self.assertIsNone(soup.script)
        self.assertEqual(soup.td.text, "B")

    def test_invalid_spans_and_scope_are_removed(self):
        for span in ("0", "-1", "101", "2.5", "abc", "9" * 5000):
            with self.subTest(span=span):
                html = f'<table><tr><th colspan="{span}" rowspan="{span}" scope="invalid">A</th></tr></table>'
                soup = BeautifulSoup(self.module._clean_topic_html(html), "html.parser")
                self.assertEqual(soup.th.attrs, {})

    def test_section_save_persists_table(self):
        user = {"username": "editor", "role": "editor"}
        with (
            mock.patch.object(self.module, "current_user", return_value=user),
            mock.patch.object(db, "get_topic_section", return_value={"id": 7, "folder_id": 2}),
            mock.patch.object(db, "update_topic_section") as update,
        ):
            response = self.module.app.test_client().post(
                "/themen/section/7/save", data={"title": "Vergleich", "content_html": TABLE}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(update.call_args.args, (7, "Vergleich", TABLE))

    def test_product_update_save_persists_table(self):
        user = {"username": "editor", "role": "editor"}
        with (
            mock.patch.object(self.module, "current_user", return_value=user),
            mock.patch.object(db, "get_topic_section", return_value={"id": 7, "folder_id": 2}),
            mock.patch.object(db, "update_topic_product_update") as update,
        ):
            response = self.module.app.test_client().post(
                "/themen/product-update/7/save", data={
                    "title": "Vergleich", "competitor": "Beispiel", "product_type": "Tarif",
                    "update_date": "2026-10-07", "factual_summary": TABLE,
                }
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(update.call_args.kwargs["factual_summary"], TABLE)


if __name__ == "__main__":
    unittest.main()
