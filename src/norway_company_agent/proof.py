from __future__ import annotations

import html as html_lib
import re
from collections import Counter
from typing import Any, Iterable, Mapping

# An identifier the official registry holds for exactly this entity, found on the site itself.
STRONG_PROOFS = frozenset({"organisation_number", "registry_email", "registry_phone"})
# A phone registered for this many entities belongs to an accountant or administrator.
SHARED_PHONE_THRESHOLD = 5
METHOD = "registry_identifier_on_site_v1"
_SEPARATOR = r"[\s.\- ]?"


def normalise_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0047"):
        digits = digits[4:]
    elif digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return digits if len(digits) == 8 else ""


def phone_share_counts(rows: Iterable[Mapping[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        for phone in {normalise_phone(row.get("telefon")), normalise_phone(row.get("mobil"))} - {""}:
            counts[phone] += 1
    return counts


def _digits_pattern(digits: str, *, country_prefix: bool = False) -> re.Pattern[str]:
    prefix = r"(?:(?:\+|00)47" + _SEPARATOR + r")?" if country_prefix else ""
    return re.compile(r"(?<!\d)" + prefix + _SEPARATOR.join(digits) + r"(?!\d)")


def registry_identifiers(row: Mapping[str, Any]) -> dict[str, Any]:
    """Identifiers from the official bulk row that can tie a website to the exact entity."""
    email = str(row.get("epostadresse") or "").strip().lower()
    phones = {normalise_phone(row.get("telefon")), normalise_phone(row.get("mobil"))} - {""}
    return {
        "organisation_number": re.sub(r"\D", "", str(row.get("organisasjonsnummer") or row.get("organisation_number") or "")),
        "email": email if "@" in email else "",
        "phones": sorted(phones),
    }


def page_proofs(identifiers: Mapping[str, Any], page_html: str, *, shared_phones: Mapping[str, int] | None = None) -> set[str]:
    text = html_lib.unescape(page_html or "")
    lowered = text.lower()
    proofs: set[str] = set()
    org = identifiers.get("organisation_number") or ""
    if len(org) == 9 and _digits_pattern(org).search(text):
        proofs.add("organisation_number")
    email = identifiers.get("email") or ""
    if email and re.search(r"(?<![\w.+-])" + re.escape(email) + r"(?![\w-])", lowered):
        proofs.add("registry_email")
    for phone in identifiers.get("phones") or []:
        if shared_phones and shared_phones.get(phone, 0) >= SHARED_PHONE_THRESHOLD:
            continue
        if _digits_pattern(phone, country_prefix=True).search(text):
            proofs.add("registry_phone")
            break
    return proofs


def assess_site_identity(proofs: Iterable[str], relation: str = "candidate") -> dict[str, Any]:
    """Publishable only when a registry identifier for this exact entity is on the site.

    Name similarity, shared email domains and addresses never publish on their own.
    """
    found = sorted(set(proofs))
    strong = [proof for proof in found if proof in STRONG_PROOFS]
    if relation == "administrator_or_group":
        status, publishable = "related", False
    elif strong:
        status, publishable = "exact", True
    elif found:
        status, publishable = "weak", False
    else:
        status, publishable = "unverified", False
    return {"status": status, "publishable": publishable, "proofs": found, "method": METHOD}
