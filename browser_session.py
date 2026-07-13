"""
Open a persistent browser profile for one source domain.

Usage:
  python3 browser_session.py fondsprofessionell.de https://www.fondsprofessionell.de/

Accept consent or log in in the opened browser, then press Enter in the terminal.
Future app fetches for that domain can reuse the stored profile when browser mode
is enabled in settings.
"""
import sys

import browser_fetcher


def main(argv):
    if len(argv) < 3:
        print("Usage: python3 browser_session.py <domain> <url>")
        return 2

    domain = argv[1]
    url = argv[2]
    print(f"Opening browser profile for {domain}.")
    print("Accept consent or log in, then return here and press Enter.")
    browser_fetcher.run_profile_setup(url, domain=domain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
