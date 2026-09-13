from __future__ import annotations

import html as html_lib
import re
import unicodedata
from collections import Counter
from typing import Any, Iterable, Mapping

from .candidates import FILLER_TOKENS, FOLDS, LEGAL_FORM_TOKENS, fold_name

# An identifier the official registry holds for exactly this entity, found on the site itself.
STRONG_PROOFS = frozenset({"organisation_number", "registry_email", "registry_phone"})
# A phone registered for this many entities belongs to an accountant or administrator.
SHARED_PHONE_THRESHOLD = 5
METHOD = "registry_identifier_on_site_v3"
MAX_TEXT_CHARS = 300_000
_SEPARATOR = r"[\s.\- ]?"


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


def _claim_span(text: str, match: re.Match[str], context: int = 60) -> str:
    window = text[max(0, match.start() - context): match.end() + context]
    return " ".join(re.sub(r"<[^>]*>?", " ", window).split())[:240]


def page_proof_spans(identifiers: Mapping[str, Any], page_html: str, *, shared_phones: Mapping[str, int] | None = None) -> dict[str, str]:
    """Registry identifiers found on the page, each with the surrounding text that proves it."""
    text = html_lib.unescape(page_html or "")
    lowered = text.lower()
    spans: dict[str, str] = {}
    org = identifiers.get("organisation_number") or ""
    if len(org) == 9 and (match := _digits_pattern(org).search(text)):
        spans["organisation_number"] = _claim_span(text, match)
    email = identifiers.get("email") or ""
    if email and (match := re.search(r"(?<![\w.+-])" + re.escape(email) + r"(?![\w-])", lowered)):
        spans["registry_email"] = _claim_span(lowered, match)
    for phone in identifiers.get("phones") or []:
        if shared_phones and shared_phones.get(phone, 0) >= SHARED_PHONE_THRESHOLD:
            continue
        if match := _digits_pattern(phone, country_prefix=True).search(text):
            spans["registry_phone"] = _claim_span(text, match)
            break
    return spans


def page_proofs(identifiers: Mapping[str, Any], page_html: str, *, shared_phones: Mapping[str, int] | None = None) -> set[str]:
    return set(page_proof_spans(identifiers, page_html, shared_phones=shared_phones))


def distinctive_name_tokens(name: str) -> list[str]:
    tokens = re.split(r"[^a-z0-9]+", fold_name(name.replace("&", " og ")))
    return [token for token in tokens if len(token) > 1 and token not in LEGAL_FORM_TOKENS and token not in FILLER_TOKENS]


def name_key(name: str) -> str:
    """Order-insensitive key of a legal name's distinctive words, for namesake counting."""
    return " ".join(sorted(set(distinctive_name_tokens(name))))


def name_key_counts(rows: Iterable[Mapping[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        key = name_key(str(row.get("navn") or ""))
        if key:
            counts[key] += 1
    return counts


def _visible_text(page_html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", page_html or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html_lib.unescape(text).split())[:MAX_TEXT_CHARS]


def _fold_with_index(text: str) -> tuple[str, list[int]]:
    """Folded text plus, for each folded character, its index in the original text."""
    folded: list[str] = []
    index: list[int] = []
    for position, character in enumerate(text):
        lowered = character.lower()
        replacement = FOLDS[0].get(lowered)
        if replacement is None:
            replacement = unicodedata.normalize("NFKD", lowered).encode("ascii", "ignore").decode()
        folded.extend(replacement)
        index.extend([position] * len(replacement))
    return "".join(folded), index


def legal_name_span(name: str, page_html: str, context: int = 60) -> str | None:
    """Verbatim text around the legal name when every distinctive name word is on the page."""
    tokens = distinctive_name_tokens(name)
    if not tokens:
        return None
    text = _visible_text(page_html)
    folded, index = _fold_with_index(text)
    anchor: re.Match[str] | None = None
    for token in sorted(tokens, key=len, reverse=True):
        match = re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", folded)
        if not match:
            return None
        anchor = anchor or match
    start, end = index[anchor.start()], index[anchor.end() - 1] + 1
    return " ".join(text[max(0, start - context): end + context].split())[:240]


def assess_site_identity(
    proofs: Iterable[str],
    relation: str = "candidate",
    *,
    registry_declared: bool = False,
    name_on_site: bool = False,
    full_name_domain: bool = False,
    unique_legal_name: bool = False,
) -> dict[str, Any]:
    """Publishable only with an official tie to this exact entity.

    Exact when a registry identifier (org number, registry email, registry phone) is on the site,
    or when the entity declared the site in the official register and its legal name is on it,
    or when the domain is the entity's full legal name, that name belongs to no other registry
    entity, and the legal name is on the site.
    A domain shared by many entities is exact only for the entity it is fully named after.
    Partial-name domains, shared email domains and addresses never publish on their own.
    """
    found = set(proofs)
    strong = bool(found & STRONG_PROOFS)
    declared = registry_declared and name_on_site
    unique_name_domain = unique_legal_name and full_name_domain and name_on_site
    if name_on_site:
        found.add("legal_name_on_site")
    if declared:
        found.add("registry_declared_website")
    if unique_name_domain:
        found.add("unique_legal_name_domain")
    if relation == "administrator_or_group" and not (full_name_domain and (strong or declared)):
        status, publishable = "related", False
    elif strong or declared or unique_name_domain:
        status, publishable = "exact", True
    elif found:
        status, publishable = "weak", False
    else:
        status, publishable = "unverified", False
    return {"status": status, "publishable": publishable, "proofs": sorted(found), "method": METHOD}
