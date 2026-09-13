import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.proof import (  # noqa: E402
    assess_site_identity,
    legal_name_span,
    normalise_phone,
    page_proof_spans,
    page_proofs,
    phone_share_counts,
    registry_identifiers,
)

ROW = {"organisasjonsnummer": "985589003", "epostadresse": "Post@arkjv.no", "telefon": "57 69 89 50", "mobil": ""}


class ProofTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ids = registry_identifiers(ROW)

    def test_identifiers_are_normalised(self) -> None:
        self.assertEqual(self.ids, {"organisation_number": "985589003", "email": "post@arkjv.no", "phones": ["57698950"]})
        self.assertEqual(normalise_phone("+47 915 00 000"), "91500000")
        self.assertEqual(normalise_phone("123"), "")

    def test_formatted_org_number_in_footer(self) -> None:
        self.assertEqual(page_proofs(self.ids, "<footer>Org.nr: 985 589 003 MVA</footer>"), {"organisation_number"})
        self.assertEqual(page_proofs(self.ids, "<p>NO985.589.003</p>"), {"organisation_number"})

    def test_claim_span_is_readable_text_around_the_identifier(self) -> None:
        spans = page_proof_spans(self.ids, "<div class='f'><p>Arkitektfirma Jon Vikøren AS</p><p>Org.nr: 985 589 003</p></div>")
        self.assertEqual(set(spans), {"organisation_number"})
        self.assertIn("985 589 003", spans["organisation_number"])
        self.assertIn("Arkitektfirma Jon Vikøren AS", spans["organisation_number"])
        self.assertNotIn("<", spans["organisation_number"])

    def test_org_number_inside_longer_number_is_not_proof(self) -> None:
        self.assertEqual(page_proofs(self.ids, "id=19855890031"), set())

    def test_tel_link_with_country_prefix_and_entity_encoded_email(self) -> None:
        html = '<a href="tel:+4757698950">Ring</a> <a href="mailto:post&#64;arkjv.no">E-post</a>'
        self.assertEqual(page_proofs(self.ids, html), {"registry_phone", "registry_email"})

    def test_different_mailbox_on_same_domain_is_not_email_proof(self) -> None:
        self.assertEqual(page_proofs(self.ids, "kontakt: firmapost@arkjv.no"), set())

    def test_phone_digits_scattered_across_page_are_not_proof(self) -> None:
        self.assertEqual(page_proofs(self.ids, "<span>5769</span><div>2024</div><span>8950</span>"), set())

    def test_shared_administrator_phone_is_ignored(self) -> None:
        counts = phone_share_counts([ROW] * 5)
        self.assertEqual(page_proofs(self.ids, "Tlf 57 69 89 50", shared_phones=counts), set())

    def test_legal_name_span_is_verbatim_and_requires_every_distinctive_word(self) -> None:
        html = "<html><title>Rosenborg Murhåndtverk</title><script>var x='x';</script><p>Vi mur i Trondheim</p></html>"
        span = legal_name_span("ROSENBORG MURHÅNDTVERK AS", html)
        self.assertIn("Rosenborg Murhåndtverk", span)
        self.assertIsNone(legal_name_span("ROSENBORG TAKTEKKING AS", html))
        self.assertIsNone(legal_name_span("AS", html))
        self.assertIsNone(legal_name_span("VAR AS", "<script>var x</script>"))

    def test_registry_declared_site_with_legal_name_is_exact(self) -> None:
        declared = assess_site_identity(set(), registry_declared=True, name_on_site=True)
        self.assertEqual((declared["status"], declared["publishable"]), ("exact", True))
        self.assertEqual(declared["proofs"], ["legal_name_on_site", "registry_declared_website"])
        self.assertEqual(assess_site_identity(set(), registry_declared=False, name_on_site=True)["status"], "weak")
        self.assertEqual(assess_site_identity(set(), registry_declared=True, name_on_site=False)["status"], "unverified")

    def test_shared_domain_is_exact_only_for_the_fully_named_entity(self) -> None:
        parent = assess_site_identity({"organisation_number"}, "administrator_or_group", full_name_domain=True)
        self.assertTrue(parent["publishable"])
        declared_parent = assess_site_identity(set(), "administrator_or_group", registry_declared=True, name_on_site=True, full_name_domain=True)
        self.assertTrue(declared_parent["publishable"])
        name_only = assess_site_identity(set(), "administrator_or_group", name_on_site=True, full_name_domain=True)
        self.assertEqual(name_only["status"], "related")

    def test_assessment_publishes_only_strong_proof_on_non_administrator_domain(self) -> None:
        self.assertTrue(assess_site_identity({"registry_phone"})["publishable"])
        self.assertEqual(assess_site_identity({"email_domain"})["status"], "weak")
        self.assertFalse(assess_site_identity(set())["publishable"])
        related = assess_site_identity({"organisation_number"}, relation="administrator_or_group")
        self.assertEqual((related["status"], related["publishable"]), ("related", False))


if __name__ == "__main__":
    unittest.main()
