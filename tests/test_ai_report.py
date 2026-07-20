import json
import unittest
from unittest import mock

import ai


class ReportGenerationTest(unittest.TestCase):
    def _article(self, article_id=1):
        return {
            "id": article_id,
            "title": "BaFin konkretisiert KI-Erwartungen an Versicherer",
            "source_name": "Beispiel Quelle",
            "published_at": "2026-07-17",
            "fetched_at": "2026-07-17",
            "category": "markt",
            "geschaeftsfeld": "Sonstiges",
            "radar_sector": "Technologie, KI & Digitalisierung",
            "tags": "bafin,ki,governance",
            "ai_summary": "BaFin fordert nachvollziehbare Kontrollen fuer KI-Prozesse.",
            "ai_implications": "",
            "content_snippet": "",
            "url": "https://example.com/artikel",
        }

    def test_weekly_report_requests_and_keeps_action_title(self):
        payload = {
            "action_title": "BaFin und KI-Governance bestimmen die Pressewoche",
            "zusammenfassung": "BaFin-Erwartungen an KI-Prozesse stehen im Fokus.",
            "abschnitte": [{
                "titel": "Technologie, KI & Digitalisierung",
                "sektor": "Technologie, KI & Digitalisierung",
                "kategorie": "markt",
                "inhalt": "Der Artikel beschreibt strengere Anforderungen an KI-Prozesse.",
                "source_ids": [1],
            }],
            "top_themen": ["KI-Governance", "BaFin"],
            "einschaetzung": "Versicherer sollten KI-Kontrollen sauber belegen.",
        }

        with mock.patch.object(ai, "_get_api_key", return_value="key"), \
             mock.patch.object(ai, "get_radar_preset_sectors", return_value=[]), \
             mock.patch.object(ai, "_get_configured_model", return_value="model-a"), \
             mock.patch.object(ai, "_call", return_value=json.dumps(payload)) as call:
            result = ai.generate_daily_report([self._article()], "2026-07-11 bis 2026-07-17", mode="weekly")

        prompt = call.call_args.args[0]
        self.assertIn('"action_title"', prompt)
        self.assertIn("Mail-Betreff", prompt)
        self.assertEqual(result["action_title"], "BaFin und KI-Governance bestimmen die Pressewoche")

    def test_report_action_title_falls_back_to_top_themes(self):
        payload = {
            "zusammenfassung": "BaFin-Erwartungen an KI-Prozesse stehen im Fokus.",
            "abschnitte": [],
            "top_themen": ["KI-Governance", "BaFin"],
            "einschaetzung": "",
        }

        with mock.patch.object(ai, "_get_api_key", return_value="key"), \
             mock.patch.object(ai, "get_radar_preset_sectors", return_value=[]), \
             mock.patch.object(ai, "_get_configured_model", return_value="model-a"), \
             mock.patch.object(ai, "_call", return_value=json.dumps(payload)):
            result = ai.generate_daily_report([self._article()], "2026-07-11 bis 2026-07-17", mode="weekly")

        self.assertEqual(result["action_title"], "KI-Governance und BaFin")


if __name__ == "__main__":
    unittest.main()
