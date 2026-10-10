import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.contract import build_envelope, validate_envelope  # noqa: E402
from norway_company_agent.pipeline import careers_record, inner_page_profiles  # noqa: E402
from norway_company_agent.site_discovery import Page  # noqa: E402
from norway_company_agent.site_jobs import career_links, postings_on_page  # noqa: E402

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
HOME = '<nav><a href="/om-oss">Om oss</a><a href="/karriere">Karriere</a><a href="https://other.no/jobb">Jobb hos partner</a></nav>'
CAREERS = """<html><body>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Elektriker","datePosted":"2026-09-20","validThrough":"2026-10-30","url":"https://acme.no/karriere/elektriker"}</script>
<script type="application/ld+json">{"@type":"JobPosting","title":"Gammel stilling","datePosted":"2025-01-01","validThrough":"2025-02-01"}</script>
<ul><li><h3>Prosjektleder</h3><a href="https://acme.webcruiter.no/main/recruit/public/4801234567?&language=nb&use_position_site_header=0&culture_id=nb-NO&url_org_id=12345">Les mer</a></li>
<li><a href="https://www.finn.no/job/fulltime/ad.html?finnkode=412345678">Lagermedarbeider Oslo</a></li>
<li><a href="https://www.finn.no/job/browse.html">Alle stillinger på FINN</a></li></ul>
</body></html>"""


def page(url, html):
    return Page(url, url, 200, html, "a" * 64, "2026-10-01T00:00:00Z")


class SiteJobsTest(unittest.TestCase):
    def test_career_link_is_same_host(self):
        self.assertEqual(career_links("https://acme.no/", HOME), ["https://acme.no/karriere"])

    def test_postings_from_jsonld_and_ad_links_only(self):
        items = postings_on_page("https://acme.no/karriere", CAREERS, NOW)
        titles = sorted(item["title"] for item in items)
        self.assertEqual(titles, ["Elektriker", "Lagermedarbeider Oslo"])  # expired dropped; generic finn browse link dropped

    def test_record_and_envelope_claims_validate(self):
        fetch = lambda url: page(url, CAREERS)  # noqa: E731
        record = careers_record(page("https://acme.no/", HOME), fetch, lambda url: True)
        self.assertEqual(record["status"], "available")
        envelope = build_envelope({"organisation_number": "912345678", "evidence": {"site_jobs": record}},
                                  run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})
        hiring = [claim for claim in envelope["claims"] if claim["field"] == "hiring_signal"]
        jobs = [claim for claim in hiring if claim["signal_type"] == "job_posting"]
        self.assertEqual({claim["relation"] for claim in jobs}, {"listed_on_verified_company_website"})
        careers = [claim for claim in hiring if claim["signal_type"] == "careers_page"]
        self.assertEqual([claim["value"] for claim in careers], ["https://acme.no/karriere"])
        self.assertEqual([p for p in validate_envelope(envelope) if "hiring" in p], [])

    def test_careers_link_to_recruitment_host_is_a_hiring_signal(self):
        home = '<html><body><a href="https://acme.webcruiter.no/main/recruit/public/vacancies">Ledige stillinger</a>' \
               '<a href="https://www.facebook.com/acme">Facebook</a></body></html>'
        record = careers_record(page("https://acme.no/", home), lambda url: page(url, ""), lambda url: True)
        self.assertEqual(record["value"]["careers_page"]["url"], "https://acme.webcruiter.no/main/recruit/public/vacancies")
        self.assertIn("webcruiter", record["value"]["careers_page"]["claim_span"])

    def test_privacy_and_news_paths_are_not_careers_pages(self):
        home = '<html><body><a href="/personvern-jobbsokere">Personvern for jobbsøkere</a><a href="/nyheter/ny-jobb">Ny jobb</a></body></html>'
        self.assertEqual(career_links("https://acme.no/", home), [])

    def test_no_site_is_not_available(self):
        self.assertEqual(careers_record(None, None, None)["status"], "not_available")


class SiteJobsRecallTest(unittest.TestCase):
    def test_bestilling_is_not_a_careers_page(self):
        home = '<a href="/bestilling">Bestilling</a><a href="/nettbutikk/bestillingsinfo">Info</a>'
        self.assertEqual(career_links("https://acme.no/", home), [])

    def test_careers_subdomain_counts_as_the_verified_site(self):
        home = '<a href="https://karriere.acme.no/">Karriere</a><a href="https://karriere.other.no/">Karriere hos andre</a>'
        self.assertEqual(career_links("https://www.acme.no/", home), ["https://karriere.acme.no/"])

    def test_microdata_jobposting_is_read(self):
        html = """<div itemscope itemtype="https://schema.org/JobPosting">
        <h2 itemprop="title">Regnskapsfører</h2><meta itemprop="datePosted" content="2026-09-25">
        <meta itemprop="validThrough" content="2026-11-01"><a itemprop="url" href="/jobb/regnskap">Les</a></div>"""
        items = postings_on_page("https://acme.no/karriere", html, NOW)
        self.assertEqual([(item["title"], item["extraction"]) for item in items], [("Regnskapsfører", "microdata_jobposting")])

    def test_new_ats_ad_patterns(self):
        html = """<a href="https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b">Backend Engineer</a>
        <a href="https://boards.greenhouse.io/acme/jobs/1234567">Sales Lead</a>
        <a href="https://www.jobbnorge.no/ledige-stillinger/stilling/245678/radgiver">Rådgiver</a>
        <a href="https://jobs.lever.co/acme">All jobs</a>"""
        titles = sorted(item["title"] for item in postings_on_page("https://acme.no/karriere", html, NOW))
        self.assertEqual(titles, ["Backend Engineer", "Rådgiver", "Sales Lead"])

    def test_recruitment_page_linked_from_the_site_is_read(self):
        home = '<a href="https://acme.teamtailor.com/jobs">Ledige stillinger</a>'
        ats = '<ul><li><a href="https://acme.teamtailor.com/jobs/5550001-lagerarbeider">Lagerarbeider Oslo</a></li></ul>'
        fetched = []

        def fetch(url):
            fetched.append(url)
            return page(url, ats)

        record = careers_record(page("https://acme.no/", home), fetch, lambda url: True)
        self.assertEqual(fetched, ["https://acme.teamtailor.com/jobs"])
        self.assertEqual(record["status"], "available")
        self.assertEqual([ad["title"] for ad in record["value"]["ads"]], ["Lagerarbeider Oslo"])

    def test_apply_button_text_is_not_a_job_title(self):
        html = '<li><h3>Laboratorieingeniør</h3><a href="https://web103.reachmee.com/ext/I022/2047/job?job_id=19">Send søknad (opens in new tab)</a></li>'
        self.assertEqual([item["title"] for item in postings_on_page("https://acme.no/karriere", html, NOW)], ["Laboratorieingeniør"])

    def test_profiles_on_the_contact_page_cite_that_page(self):
        home = page("https://acme.no/", '<a href="/kontakt">Kontakt oss</a>')
        contact = page("https://acme.no/kontakt", '<a href="https://www.facebook.com/acmenorge">Facebook</a><a href="https://www.facebook.com/sharer/sharer.php?u=x">Del</a>')
        record = inner_page_profiles({"status": "available"}, {"status": "not_available"}, home, lambda url: contact, lambda url: True)
        [profile] = record["value"]["profiles"]
        self.assertEqual((profile["platform"], profile["source_url"]), ("facebook", "https://acme.no/kontakt"))
        envelope = build_envelope({"organisation_number": "912345678", "evidence": {"social_profiles": record}},
                                  run_id="r", started_at="s", completed_at="c", operations={"requests": 1, "runtime_ms": 0})
        [claim] = [c for c in envelope["claims"] if c["field"] == "social_profile"]
        cited = [ev["source_url"] for ev in envelope["evidence"] if ev["id"] in claim["evidence_ids"]]
        self.assertEqual(cited, ["https://acme.no/kontakt"])
        self.assertIsNone(inner_page_profiles({"status": "available"}, {"status": "available"}, home, lambda url: self.fail("fetched"), lambda url: True))

    def test_one_level_deeper_when_landing_page_has_no_ads(self):
        home = '<a href="/karriere">Karriere</a>'
        landing = '<p>Jobb hos oss</p><a href="/karriere/ledige-stillinger">Se ledige stillinger</a>'
        listing = '<a href="https://www.finn.no/job/fulltime/ad.html?finnkode=412345679">Butikkmedarbeider Bergen</a>'
        pages = {"https://acme.no/karriere": landing, "https://acme.no/karriere/ledige-stillinger": listing}
        record = careers_record(page("https://acme.no/", home), lambda url: page(url, pages[url]), lambda url: True)
        self.assertEqual([ad["title"] for ad in record["value"]["ads"]], ["Butikkmedarbeider Bergen"])


if __name__ == "__main__":
    unittest.main()
