import unittest
from unittest import mock

import ai


class ArticleSummaryNormalisationTest(unittest.TestCase):
    def test_bullet_summary_without_final_punctuation_is_preserved(self):
        raw = (
            "- Reform der privaten Altersvorsorge mit Starttermin 1. Januar 2027\n"
            "- Standarddepot als staatliche Standardloesung laut Artikel moeglicherweise verspaetet\n"
            "- Spuerbare Folgen fuer Sparer und Vermittlerkommunikation"
        )

        self.assertEqual(ai._clean_generated_summary(raw), raw)

    def test_malformed_zusammenfassung_json_extracts_full_bullet_summary(self):
        raw = (
            '{"zusammenfassung": "- Reform der privaten Altersvorsorge mit Starttermin 1. Januar 2027\\n'
            '- Standarddepot laut Artikel moeglicherweise nicht rechtzeitig fertig", '
            '"geschaeftsfeld": "Leben", "kategorie": "markt"}'
        )

        self.assertEqual(
            ai._clean_generated_summary(raw),
            (
                "- Reform der privaten Altersvorsorge mit Starttermin 1. Januar 2027\n"
                "- Standarddepot laut Artikel moeglicherweise nicht rechtzeitig fertig"
            ),
        )

    def test_normalize_pin_data_accepts_summary_alias_from_repair_path(self):
        result = ai._normalize_pin_data(
            {
                "summary": "- Altersvorsorgedepot-Zeitplan unsicher\n- Standardloesung unter Druck",
                "geschaeftsfeld": "Leben",
                "kategorie": "markt",
                "tags": ["altersvorsorge", "standarddepot"],
            },
            "Titel",
            "Snippet",
        )

        self.assertEqual(
            result["zusammenfassung"],
            "- Altersvorsorgedepot-Zeitplan unsicher\n- Standardloesung unter Druck",
        )
        self.assertEqual(result["geschaeftsfeld"], "Leben")
        self.assertEqual(result["kategorie"], "markt")


class EmbeddingModelSettingsTest(unittest.TestCase):
    def test_embedding_model_choices_include_requested_models(self):
        with mock.patch.object(ai, "get_embedding_model", return_value="custom/embed-model"):
            choices = dict(ai.get_embedding_model_choices())

        self.assertIn("openai/text-embedding-3-large", choices)
        self.assertIn("openai/text-embedding-3-small", choices)
        self.assertIn("google/gemini-embedding-001", choices)
        self.assertIn("perplexity/pplx-embed-v1-4b", choices)
        self.assertEqual(choices["custom/embed-model"], "Aktuell: custom/embed-model")

    def test_openai_provider_prefix_is_removed_for_direct_api(self):
        with mock.patch.object(ai, "_get_embedding_base_url", return_value=""):
            self.assertEqual(
                ai._embedding_api_model("openai/text-embedding-3-small"),
                "text-embedding-3-small",
            )

        with mock.patch.object(ai, "_get_embedding_base_url", return_value="https://openrouter.ai/api/v1"):
            self.assertEqual(
                ai._embedding_api_model("openai/text-embedding-3-small"),
                "openai/text-embedding-3-small",
            )


if __name__ == "__main__":
    unittest.main()
