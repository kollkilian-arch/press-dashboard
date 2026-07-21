import json
import unittest
from unittest import mock

import ai


class TrendRadarGenerationTest(unittest.TestCase):
    def _article(self, article_id):
        return {
            "id": article_id,
            "title": f"Artikel {article_id}",
            "source_name": "Quelle",
            "published_at": "2026-07-10",
            "category": "markt",
            "geschaeftsfeld": "Kranken",
            "radar_sector": "Technologie",
            "tags": "ki,markt",
            "ai_summary": "Kurze Analyse zum gemeinsamen Signal.",
            "content_snippet": "",
        }

    def test_trendradar_retries_with_compact_prompt_after_length_finish(self):
        payload = {
            "title": "KI-Trendradar",
            "sectors": ["Technologie"],
            "topics": [{
                "name": "Automatisierung im Vertrieb",
                "sector": "Technologie",
                "horizon": "Prepare",
                "summary": "Mehrere Signale zeigen Automatisierungspotenzial.",
                "evidence": "Artikel 1 bis 3",
                "confidence": 80,
                "article_ids": [1, 2, 3],
            }],
        }

        with mock.patch.object(ai, "_get_api_key", return_value="key"), \
             mock.patch.object(ai, "get_radar_preset_sectors", return_value=[]), \
             mock.patch.object(ai, "_get_configured_model", return_value="model-a") as get_model, \
             mock.patch.object(ai, "_get_article_summary_fallback_models", return_value=[]), \
             mock.patch.object(ai, "_call", side_effect=[
                 ai.ModelOutputTruncatedError("finish_reason=length"),
                 json.dumps(payload),
             ]) as call:
            result = ai.generate_trend_radar([self._article(1), self._article(2), self._article(3)])

        self.assertEqual(call.call_count, 2)
        get_model.assert_called_with("trend_radar")
        self.assertEqual(result["topics"][0]["name"], "Automatisierung im Vertrieb")
        self.assertEqual(result["topics"][0]["article_ids"], [1, 2, 3])

    def test_standard_prompt_keeps_full_quality_constraints(self):
        payload = {
            "title": "KI-Trendradar",
            "sectors": ["Technologie"],
            "topics": [{
                "name": "Automatisierung im Vertrieb",
                "sector": "Technologie",
                "horizon": "Prepare",
                "summary": "Mehrere Signale zeigen Automatisierungspotenzial.",
                "evidence": "Artikel 1 bis 3",
                "confidence": 80,
                "article_ids": [1, 2, 3],
            }],
        }

        with mock.patch.object(ai, "_get_api_key", return_value="key"), \
             mock.patch.object(ai, "get_radar_preset_sectors", return_value=[]), \
             mock.patch.object(ai, "_get_configured_model", return_value="model-a") as get_model, \
             mock.patch.object(ai, "_get_article_summary_fallback_models", return_value=[]), \
             mock.patch.object(ai, "_call", return_value=json.dumps(payload)) as call:
            ai.generate_trend_radar([self._article(1), self._article(2), self._article(3)])

        get_model.assert_called_with("trend_radar")
        prompt = call.call_args.args[0]
        self.assertIn("5-12 topics", prompt)
        self.assertIn("Topic-Namen maximal 95 Zeichen", prompt)
        self.assertIn("Vermeide Recency Bias", prompt)
        self.assertNotIn("Maximal 6 article_ids", prompt)
        self.assertEqual(call.call_args.kwargs["temperature"], 0.1)

    def test_previous_radar_is_used_as_continuity_context(self):
        payload = {
            "title": "KI-Trendradar",
            "change_summary": "Stabil mit einem aktualisierten Thema.",
            "dropped_topics": ["Altes Thema"],
            "sectors": ["Technologie"],
            "topics": [{
                "name": "Automatisierung im Vertrieb",
                "sector": "Technologie",
                "horizon": "Prepare",
                "summary": "Mehrere Signale zeigen Automatisierungspotenzial.",
                "evidence": "Artikel 1 bis 3",
                "confidence": 80,
                "change_type": "updated",
                "previous_topic": "Automatisierung",
                "article_ids": [1, 2, 3],
            }],
        }
        previous_radar = {
            "label": "KI-Trendradar",
            "created_at": "2026-07-09 08:00:00",
            "sectors": ["Technologie"],
            "topics": [{
                "name": "Automatisierung",
                "sector": "Technologie",
                "horizon": "Monitor",
                "summary": "Alte Zusammenfassung.",
                "evidence": "Alte Signale.",
                "article_ids": [7, 8, 9],
            }],
        }

        with mock.patch.object(ai, "_get_api_key", return_value="key"), \
             mock.patch.object(ai, "get_radar_preset_sectors", return_value=[]), \
             mock.patch.object(ai, "_get_configured_model", return_value="model-a"), \
             mock.patch.object(ai, "_get_article_summary_fallback_models", return_value=[]), \
             mock.patch.object(ai, "_call", return_value=json.dumps(payload)) as call:
            result = ai.generate_trend_radar(
                [self._article(1), self._article(2), self._article(3)],
                previous_radar=previous_radar,
            )

        prompt = call.call_args.args[0]
        self.assertIn("VORHERIGER RADAR ALS KONTINUITAETSVORSCHLAG", prompt)
        self.assertIn("Automatisierung", prompt)
        self.assertEqual(result["change_summary"], "Stabil mit einem aktualisierten Thema.")
        self.assertEqual(result["dropped_topics"], ["Altes Thema"])
        self.assertEqual(result["topics"][0]["change_type"], "updated")
        self.assertEqual(result["topics"][0]["previous_topic"], "Automatisierung")

    def test_full_gpt_41_setting_is_aliased_to_mini(self):
        with mock.patch.object(ai.db, "get_setting", return_value="openai/gpt-4.1"):
            self.assertEqual(ai._get_configured_model("trend_radar"), "openai/gpt-4.1-mini")

    def test_management_summary_uses_topic_summaries_and_change_context(self):
        radar = {
            "label": "KI-Trendradar",
            "created_at": "2026-07-21 10:00:00",
            "article_count": 9,
            "change_summary": "Neue Signale verdichten sich im Vertrieb.",
            "topics": [{
                "name": "Automatisierung im Vertrieb",
                "sector": "Technologie",
                "horizon": "Prepare",
                "summary": "Mehrere Signale zeigen Automatisierungspotenzial im Vertrieb.",
                "evidence": "Nicht im Bullet direkt nötig.",
                "confidence": 80,
                "article_count": 4,
            }],
        }
        payload = {
            "bullets": [
                "Vertriebsautomatisierung verdichtet sich als Vorbereitungsthema.",
                "KI-Unterstützung sollte priorisiert in Beratungsprozessen geprüft werden.",
                "Signale bleiben quellengebunden und erfordern fachliche Bewertung.",
                "Veränderungsnotiz kann als Kontext für Priorisierung dienen.",
                "Management sollte die wichtigsten Handlungsfelder kompakt sehen.",
            ],
        }

        with mock.patch.object(ai, "_get_api_key", return_value="key"), \
             mock.patch.object(ai, "_get_configured_model", return_value="model-a") as get_model, \
             mock.patch.object(ai, "_call", return_value=json.dumps(payload)) as call:
            result = ai.generate_radar_management_summary(radar)

        self.assertEqual(result, payload["bullets"])
        get_model.assert_called_with("trend_radar")
        prompt = call.call_args.args[0]
        self.assertIn("Neue Signale verdichten sich im Vertrieb.", prompt)
        self.assertIn("Mehrere Signale zeigen Automatisierungspotenzial im Vertrieb.", prompt)
        self.assertIn("Erstelle 5-6 Bullet Points", prompt)
        self.assertEqual(call.call_args.kwargs["json_mode"], True)

    def test_management_summary_normalizes_bullets(self):
        data = {
            "bullets": [
                "- Erstes Thema priorisieren.",
                "1. Zweites Thema vorbereiten.",
                "• Drittes Thema beobachten.",
                "Viertes Thema bewerten.",
                "Fünftes Thema einordnen.",
                "",
            ],
        }

        self.assertEqual(ai._normalize_management_summary(data), [
            "Erstes Thema priorisieren.",
            "Zweites Thema vorbereiten.",
            "Drittes Thema beobachten.",
            "Viertes Thema bewerten.",
            "Fünftes Thema einordnen.",
        ])


if __name__ == "__main__":
    unittest.main()
