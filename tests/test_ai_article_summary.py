import unittest

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


if __name__ == "__main__":
    unittest.main()
