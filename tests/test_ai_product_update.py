import json
import unittest
from unittest import mock

import ai


class ProductUpdateSourceAnalysisTest(unittest.TestCase):
    def _article(self):
        return {
            "url": "https://example.test/update",
            "title": "Versicherer erweitert Tarif",
            "source_name": "Example",
            "published_at": "2026-09-10",
            "full_text": "Der Versicherer erweitert den Tarif um eine neue Leistung.",
        }

    def _current(self, summary=""):
        return {
            "title": "Produktupdate",
            "competitor": "",
            "product_type": "PKV",
            "update_date": "",
            "factual_summary": summary,
        }

    def _analyse(self, response, current=None):
        with (
            mock.patch.object(ai, "_get_api_key", return_value="test-key"),
            mock.patch.object(ai, "_get_configured_model", return_value="test-model"),
            mock.patch.object(ai, "_get_article_summary_fallback_models", return_value=[]),
            mock.patch.object(ai, "_call", return_value=json.dumps(response)),
        ):
            return ai.analyse_product_update_source(
                self._article(), current or self._current()
            )

    def test_new_source_returns_complete_merged_bullets_and_metadata(self):
        result = self._analyse({
            "same_update": True,
            "has_new_information": True,
            "summary_bullets": ["Bisheriger Fakt", "Neue Leistung ab Oktober"],
            "new_facts": ["Neue Leistung ab Oktober"],
            "proposed_title": "Tariferweiterung",
            "competitor": "Beispiel AG",
            "product_type": "PKV",
            "update_date": "2026-10-01",
        })

        self.assertTrue(result["same_update"])
        self.assertTrue(result["has_new_information"])
        self.assertEqual(result["summary_bullets"][-1], "Neue Leistung ab Oktober")
        self.assertEqual(result["update_date"], "2026-10-01")

    def test_no_new_information_cannot_rewrite_existing_summary(self):
        result = self._analyse(
            {
                "same_update": True,
                "has_new_information": False,
                "summary_bullets": ["Umformulierter alter Fakt"],
                "new_facts": [],
                "proposed_title": "",
                "competitor": "",
                "product_type": "",
                "update_date": "",
            },
            current=self._current("<ul><li>Bestehender Fakt</li></ul>"),
        )

        self.assertFalse(result["has_new_information"])
        self.assertEqual(result["summary_bullets"], [])

    def test_string_false_and_invalid_date_are_normalized(self):
        result = self._analyse({
            "same_update": "false",
            "has_new_information": "false",
            "summary_bullets": [],
            "new_facts": [],
            "proposed_title": "",
            "competitor": "",
            "product_type": "",
            "update_date": "2026-13-40",
        })

        self.assertFalse(result["same_update"])
        self.assertEqual(result["update_date"], "")

    def test_pasted_text_does_not_require_a_website_url(self):
        article = self._article()
        article["url"] = ""
        article["title"] = "Text aus PDF"
        article["full_text"] = "Manuell extrahierter PDF-Text mit einer neuen Tarifleistung."
        response = {
            "same_update": True,
            "has_new_information": True,
            "summary_bullets": ["Neue Tarifleistung eingeführt"],
            "new_facts": ["Neue Tarifleistung eingeführt"],
            "proposed_title": "Tariferweiterung",
            "competitor": "Beispiel AG",
            "product_type": "PKV",
            "update_date": "2026-09-10",
        }
        with (
            mock.patch.object(ai, "_get_api_key", return_value="test-key"),
            mock.patch.object(ai, "_get_configured_model", return_value="test-model"),
            mock.patch.object(ai, "_get_article_summary_fallback_models", return_value=[]),
            mock.patch.object(ai, "_call", return_value=json.dumps(response)) as call,
        ):
            result = ai.analyse_product_update_source(article, self._current())

        self.assertTrue(result["has_new_information"])
        self.assertIn("Manuell extrahierter PDF-Text", call.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
