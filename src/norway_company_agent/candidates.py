from __future__ import annotations

import re
import unicodedata
import urllib.parse
from collections import Counter
from typing import Any, Iterable

import tldextract

# Consumer and ISP mail hosts: an address here says nothing about the company's own domain.
FREE_MAIL_DOMAINS = frozenset("""
gmail.com googlemail.com hotmail.com hotmail.no outlook.com outlook.no live.com live.no msn.com
yahoo.com yahoo.no icloud.com me.com mail.com gmx.com protonmail.com proton.me online.no
getmail.no frisurf.no broadpark.no c2i.net start.no altibox.no lyse.net telenor.no ebnett.no
epost.no enivest.net tele2.no chello.no bluewin.ch
""".split())
# A domain used by this many registry entities belongs to an administrator (housing
# co-op manager, accountant, franchise) and is never an exact company site.
SHARED_DOMAIN_THRESHOLD = 5
LEGAL_FORM_TOKENS = frozenset("as asa ans da enk sa ba nuf ks bbl brl sf hf fli spa iks kf".split())
FILLER_TOKENS = frozenset("stiftelsen stiftelse sameiet borettslag og i the holding norge norway group gruppen".split())
FOLDS = ({"æ": "ae", "ø": "o", "å": "a"}, {"æ": "e", "ø": "o", "å": "aa"})


def registered_domain(value: str | None) -> str:
    value = str(value or "").strip().lower()
    if not value:
        return ""
    host = value.split("@")[-1] if "@" in value and "//" not in value else urllib.parse.urlparse(value if "//" in value else "//" + value).hostname or ""
    return tldextract.extract(host).top_domain_under_public_suffix or ""


def email_domain(email: str | None) -> str:
    email = str(email or "").strip().lower()
    return registered_domain(email) if "@" in email else ""


def email_domain_counts(rows: Iterable[dict[str, Any]]) -> Counter[str]:
    """Count registry entities per company-owned email domain (one pass over the bulk snapshot)."""
    counts: Counter[str] = Counter()
    for row in rows:
        domain = email_domain(row.get("epostadresse"))
        if domain and domain not in FREE_MAIL_DOMAINS:
            counts[domain] += 1
    return counts


def name_domain_labels(name: str, limit: int = 6) -> list[str]:
    """Deterministic domain labels from a legal name, most specific first."""
    ordered: list[str] = []
    for mapping in FOLDS:
        folded = name.lower()
        for source, target in mapping.items():
            folded = folded.replace(source, target)
        folded = unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode()
        tokens = [token for token in re.split(r"[^a-z0-9]+", folded.replace("&", " og ")) if token]
        without_form = [token for token in tokens if token not in LEGAL_FORM_TOKENS]
        core = [token for token in without_form if token not in FILLER_TOKENS]
        for sequence_source in (core, without_form):
            for sequence in (sequence_source, sequence_source[:2], sequence_source[1:], sequence_source[:1], sequence_source[-2:]):
                for label in ("".join(sequence), "-".join(sequence)):
                    if len(label) >= 3 and label not in ordered:
                        ordered.append(label)
    return ordered[:limit]


def website_candidates(row: dict[str, Any], shared_counts: Counter[str], *, guess_limit: int = 6) -> list[dict[str, str]]:
    """Ordered, de-duplicated website candidates. Candidates are never evidence."""
    found: dict[str, dict[str, str]] = {}

    def add(domain: str, source: str, relation: str = "candidate") -> None:
        if domain and domain not in found:
            found[domain] = {"domain": domain, "source": source, "relation": relation}

    add(registered_domain(row.get("hjemmeside") or row.get("website")), "registry_website")
    mail = email_domain(row.get("epostadresse"))
    if mail and mail not in FREE_MAIL_DOMAINS:
        shared = shared_counts.get(mail, 0) >= SHARED_DOMAIN_THRESHOLD
        add(mail, "registry_email_domain", "administrator_or_group" if shared else "candidate")
    for label in name_domain_labels(str(row.get("navn") or row.get("name") or ""), guess_limit):
        for suffix in ("no", "com"):
            add(f"{label}.{suffix}", "name_guess")
    return list(found.values())
