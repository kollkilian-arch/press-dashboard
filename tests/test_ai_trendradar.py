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
             mock.patch.object(ai, "_get_configured_model", return_value="model-a"), \
             mock.patch.object(ai, "_get_article_summary_fallback_models", return_value=[]), \
             mock.patch.object(ai, "_call", side_effect=[
                 ai.ModelOutputTruncatedError("finish_reason=length"),
                 json.dumps(payload),
             ]) as call:
            result = ai.generate_trend_radar([self._article(1), self._article(2), self._article(3)])

        self.assertEqual(call.call_count, 2)
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
             mock.patch.object(ai, "_get_configured_model", return_value="model-a"), \
             mock.patch.object(ai, "_get_article_summary_fallback_models", return_value=[]), \
             mock.patch.object(ai, "_call", return_value=json.dumps(payload)) as call:
            ai.generate_trend_radar([self._article(1), self._article(2), self._article(3)])

        prompt = call.call_args.args[0]
        self.assertIn("5-12 topics", prompt)
        self.assertIn("Vermeide Recency Bias", prompt)
        self.assertNotIn("Maximal 6 article_ids", prompt)


if __name__ == "__main__":
    unittest.main()
