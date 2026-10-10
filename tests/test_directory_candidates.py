import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import news_search  # noqa: E402
from norway_company_agent.budget import RequestBudget  # noqa: E402
from norway_company_agent.candidates import website_candidates  # noqa: E402
from norway_company_agent.directory_candidates import DirectoryPrefetcher, page_domains  # noqa: E402


def page(*links):
    return ("<html>" + "".join(f'<a href="{link}">x</a>' for link in links) + "</html>").encode()


class DirectoryCandidatesTest(unittest.TestCase):
    def setUp(self):
        news_search._robots_answer.clear()

    def test_directory_furniture_is_dropped(self):
        html = page("https://www.ptg.no/", "https://kart.1881.no/x", "https://hjemmesidehuset.no/", "https://www.facebook.com/x").decode()
        self.assertEqual(page_domains(html), {"ptg.no"})

    def test_lookups_feed_candidates_and_repeats_are_suppressed(self):
        pages = {"1": page("https://www.ptg.no/", "https://shared-ad.no/"), "2": page("https://nordicwell.no/", "https://shared-ad.no/"),
                 "3": page("https://shared-ad.no/")}

        def fetch(url, spend):
            spend()
            return (200, b"") if url.endswith("robots.txt") else (200, pages[url.rsplit("=", 1)[-1]])

        directory = DirectoryPrefetcher(["1", "2", "3"], RequestBudget(100, 3600), fetch=fetch, threads=1, min_interval=0, margin_seconds=0).start()
        self.assertTrue(directory.done.wait(5))
        self.assertEqual(directory.candidates("1"), ["ptg.no"])  # shared-ad.no linked from 3 companies' pages
        self.assertEqual(directory.candidates("unknown"), [])  # never waits, never raises
        self.assertEqual(directory.summary()["looked_up"], 3)

    def test_rate_limit_stops_lookups(self):
        calls = []

        def fetch(url, spend):
            calls.append(url)
            return (200, b"") if url.endswith("robots.txt") else (429, b"")

        directory = DirectoryPrefetcher(["1", "2", "3"], RequestBudget(100, 3600), fetch=fetch, threads=1, min_interval=0, margin_seconds=0).start()
        self.assertTrue(directory.done.wait(5))
        self.assertIn("429", directory.summary()["note"])
        self.assertEqual(len([url for url in calls if "query=" in url]), 1)

    def test_directory_domains_are_candidates_not_proofs(self):
        candidates = website_candidates({"navn": "PTG MULTI KULDE AS", "_directory_domains": ["ptg.no"]}, {})
        listed = [c for c in candidates if c["source"] == "directory_listing"]
        self.assertEqual([c["domain"] for c in listed], ["ptg.no"])
        self.assertEqual(listed[0]["relation"], "candidate")


if __name__ == "__main__":
    unittest.main()
