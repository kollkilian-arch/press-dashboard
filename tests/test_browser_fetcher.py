import unittest

import browser_fetcher


class BrowserFetcherTest(unittest.TestCase):
    def test_normalizes_profile_directory_for_domain(self):
        profile = browser_fetcher.profile_dir_for_domain("www.FONDSprofessionell.de")

        self.assertEqual(profile.name, "www.fondsprofessionell.de")
        self.assertIn(".browser_profiles", str(profile))

    def test_extracts_domain_from_url(self):
        self.assertEqual(
            browser_fetcher.domain_from_url("https://www.fondsprofessionell.de/news/foo"),
            "fondsprofessionell.de",
        )


if __name__ == "__main__":
    unittest.main()
