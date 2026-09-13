import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.candidates import (  # noqa: E402
    email_domain,
    email_domain_counts,
    full_name_labels,
    name_domain_labels,
    registered_domain,
    website_candidates,
)


class CandidateTest(unittest.TestCase):
    def test_registered_domain_normalises_urls_hosts_and_emails(self) -> None:
        self.assertEqual(registered_domain("www.aelektronikk.no"), "aelektronikk.no")
        self.assertEqual(registered_domain("https://shop.example.co.uk/path"), "example.co.uk")
        self.assertEqual(email_domain("Post@Mail.ARKJV.no"), "arkjv.no")
        self.assertEqual(email_domain("not-an-email"), "")

    def test_free_mail_is_not_counted_as_company_domain(self) -> None:
        rows = [{"epostadresse": "a@gmail.com"}, {"epostadresse": "b@firma.no"}, {"epostadresse": ""}]
        self.assertEqual(email_domain_counts(rows), Counter({"firma.no": 1}))

    def test_name_labels_fold_norwegian_letters_and_strip_legal_form(self) -> None:
        labels = name_domain_labels("FJELD OG VANN AS", limit=10)
        self.assertIn("fjeldogvann", labels)
        self.assertIn("fjeld-vann", labels)
        self.assertNotIn("fjeldogvannas", labels)
        self.assertIn("murhandtverk", name_domain_labels("ROSENBORG MURHÅNDTVERK AS", limit=10))

    def test_shared_email_domain_is_labelled_administrator(self) -> None:
        row = {"navn": "SAMEIET ST OLAV", "epostadresse": "post@bate.no", "hjemmeside": ""}
        candidates = website_candidates(row, Counter({"bate.no": 520}))
        bate = next(item for item in candidates if item["domain"] == "bate.no")
        self.assertEqual(bate["relation"], "administrator_or_group")

    def test_full_name_guesses_before_partial_and_partial_only_on_no(self) -> None:
        row = {"navn": "TRØNDELAG BETONG AS", "epostadresse": "", "hjemmeside": ""}
        candidates = website_candidates(row, Counter())
        forms = [(item["domain"], item["name_form"]) for item in candidates]
        self.assertEqual(forms[0], ("trondelagbetong.no", "full"))
        first_com = next(index for index, (domain, _) in enumerate(forms) if domain.endswith(".com"))
        first_partial = next(index for index, (_, form) in enumerate(forms) if form == "partial")
        self.assertLess(first_com, first_partial)
        self.assertTrue(all(domain.endswith(".no") for domain, form in forms if form == "partial"))
        self.assertIn("trondelagbetong", full_name_labels("TRØNDELAG BETONG AS"))
        self.assertNotIn("betong", full_name_labels("TRØNDELAG BETONG AS"))

    def test_candidate_order_and_deduplication(self) -> None:
        row = {"navn": "ZAPTEC ASA", "epostadresse": "post@zaptec.com", "hjemmeside": "www.zaptec.no"}
        candidates = website_candidates(row, Counter({"zaptec.com": 1}))
        self.assertEqual([item["source"] for item in candidates[:2]], ["registry_website", "registry_email_domain"])
        domains = [item["domain"] for item in candidates]
        self.assertEqual(len(domains), len(set(domains)))
        self.assertEqual(candidates[1]["relation"], "candidate")


if __name__ == "__main__":
    unittest.main()
