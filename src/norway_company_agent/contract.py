"""Terminal company envelope in the published output contract (OUTPUT_CONTRACT.md).

Every claim carries an availability state and evidence ids; every evidence entry carries its
source, retrieval time, content hash and the span that supports the claim. Missing values stay
`None` with an explicit state; they are never converted to zero.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

CONTRACT_STATES = frozenset({"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"})
_STATUS_TO_STATE = {
    "available": "available",
    "not_available": "not_available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
    "ambiguous": "ambiguous",
    "failed": "failed",
    "source_error": "failed",
    "not_fetched": "failed",
}
MODULES = ("registry", "financials", "financial_history", "roles", "locations", "website", "social_profiles")
REGISTRY_FIELDS = (
    ("legal_name", "navn"),
    ("legal_form", "organisasjonsform.kode"),
    ("industry_code", "naeringskode1.kode"),
    ("industry", "naeringskode1.beskrivelse"),
    ("registered_employees", "antallAnsatte"),
    ("registry_declared_website", "hjemmeside"),
)
REGISTRY_FLAGS = (("bankrupt", "konkurs"), ("under_liquidation", "underAvvikling"))
ADDRESS_PARTS = ("adresse", "postnummer", "poststed", "kommune")
FINANCIAL_FIELDS = ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt")
STRONG_WEBSITE_PROOFS = {"organisation_number", "registry_email", "registry_phone"}


def availability(record: Mapping[str, Any] | None) -> str:
    return _STATUS_TO_STATE.get(str((record or {}).get("status")), "failed") if record else "failed"


class _Envelope:
    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = []
        self.evidence: dict[str, dict[str, Any]] = {}
        self.errors: list[dict[str, Any]] = []

    def cite(self, record: Mapping[str, Any], claim_span: str | None = None, **extra: Any) -> str:
        entry = {
            "source_url": record.get("source_url"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "retrieved_at": record.get("retrieved_at"),
            "content_sha256": record.get("content_sha256"),
            "claim_span": claim_span,
        }
        for key in ("source_row_key", "method"):
            if record.get(key):
                entry["extraction_method" if key == "method" else key] = record[key]
        entry.update({key: value for key, value in extra.items() if value is not None})
        digest = hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        evidence_id = "ev-" + digest[:16]
        self.evidence.setdefault(evidence_id, {"id": evidence_id, **entry})
        return evidence_id

    def claim(self, field: str, value: Any, state: str, evidence_ids: Iterable[str] = (), confidence: float | None = None, **extra: Any) -> None:
        item: dict[str, Any] = {"field": field, "value": value, "availability": state, "evidence_ids": list(evidence_ids)}
        if confidence is not None:
            item["confidence"] = confidence
        item.update({key: value for key, value in extra.items() if value is not None})
        self.claims.append(item)

    def unavailable(self, module: str, record: Mapping[str, Any] | None, state: str) -> None:
        note = (record or {}).get("note") or ("Module did not run" if record is None else None)
        self.claim(module, None, state, [self.cite(record)] if record and record.get("source_url") else [], note=note)
        if state == "failed":
            self.errors.append({"module": module, "message": note or "source failed"})


def _registry(envelope: _Envelope, record: Mapping[str, Any] | None) -> None:
    state = availability(record)
    if state != "available" or record is None:
        envelope.unavailable("legal_identity", record, state)
        return
    raw = record.get("value") or {}
    for field, column in REGISTRY_FIELDS:
        value = str(raw.get(column) or "").strip()
        if not value:
            envelope.claim(field, None, "not_available", [envelope.cite(record, f"{column}: (empty)")], note="Empty in the official registry snapshot")
            continue
        typed: Any = int(value) if field == "registered_employees" and value.isdigit() else value
        envelope.claim(field, typed, "available", [envelope.cite(record, f"{column}: {value}")], confidence=1.0)
    address = {part: str(raw.get(f"forretningsadresse.{part}") or "").strip() or None for part in ADDRESS_PARTS}
    if address["adresse"] or address["poststed"]:
        span = ", ".join(value for value in address.values() if value)
        envelope.claim("business_address", address, "available", [envelope.cite(record, f"forretningsadresse: {span}")], confidence=1.0)
    else:
        envelope.claim("business_address", None, "not_available", [envelope.cite(record, "forretningsadresse: (empty)")])
    for field, column in REGISTRY_FLAGS:
        value = str(raw.get(column) or "").strip().lower()
        if value in {"true", "false"}:
            envelope.claim(field, value == "true", "available", [envelope.cite(record, f"{column}: {value}")], confidence=1.0)


def _financials(envelope: _Envelope, record: Mapping[str, Any] | None) -> None:
    state = availability(record)
    records = ((record or {}).get("value") or {}).get("records") or []
    if state != "available" or not records:
        envelope.unavailable("financials", record, "not_available" if state == "available" else state)
        return
    latest_end = max(str((item.get("period") or {}).get("tilDato") or "") for item in records)
    for item in records:
        period = item.get("period") or {}
        if str(period.get("tilDato") or "") != latest_end:
            continue
        reporting_period = {"start": period.get("fraDato"), "end": period.get("tilDato")}
        evidence_id = envelope.cite(record, f"Regnskap {item.get('record_id')} {item.get('account_type')} {period.get('fraDato')}..{period.get('tilDato')}")
        for name in FINANCIAL_FIELDS:
            amount = item.get(name)
            envelope.claim(
                f"financials.{name}",
                None if amount is None else {"amount": amount, "currency": item.get("currency")},
                "not_available" if amount is None else "available",
                [evidence_id],
                confidence=None if amount is None else 1.0,
                reporting_period=reporting_period,
                account_type=item.get("account_type"),
            )


def _financial_history(envelope: _Envelope, record: Mapping[str, Any] | None) -> None:
    state = availability(record)
    years = ((record or {}).get("value") or {}).get("years") or []
    if state != "available" or not years:
        envelope.unavailable("financials.filed_years", record, "not_available" if state == "available" else state)
        return
    pdfs = ((record or {}).get("value") or {}).get("pdfs") or []
    envelope.claim("financials.filed_years", years, "available", [envelope.cite(record, "Filed annual-account years: " + ", ".join(years))], confidence=1.0, documents=pdfs)


def _listed(envelope: _Envelope, record: Mapping[str, Any] | None, *, module: str, key: str, field: str, span: Any) -> None:
    state = availability(record)
    items = ((record or {}).get("value") or {}).get(key) or []
    if module == "roles":
        items = [item for item in items if not item.get("inactive")]
    if state != "available" or not items:
        envelope.unavailable(module, record, "not_available" if state == "available" else state)
        return
    for item in items:
        envelope.claim(field, item, "available", [envelope.cite(record, span(item))], confidence=1.0, effective_at=item.get("last_changed"))


def _website(envelope: _Envelope, record: Mapping[str, Any] | None, registry: Mapping[str, Any] | None) -> None:
    state = availability(record)
    value = (record or {}).get("value") or {}
    if state != "available":
        envelope.unavailable("official_website", record, state)
        return
    proofs = set((value.get("identity_assessment") or {}).get("proofs") or [])
    evidence_ids = []
    for page in value.get("proof_pages") or []:
        page_record = {"source_url": page.get("url"), "source_class": "company_owned_website", "retrieved_at": page.get("retrieved_at"), "content_sha256": page.get("content_sha256"), "method": (record or {}).get("method")}
        for proof, span in sorted((page.get("claim_spans") or {}).items()):
            evidence_ids.append(envelope.cite(page_record, span, proof=proof))
    if "registry_declared_website" in proofs and registry:
        declared = str((registry.get("value") or {}).get("hjemmeside") or "")
        evidence_ids.append(envelope.cite(registry, f"hjemmeside: {declared}", proof="registry_declared_website"))
    if "unique_legal_name_domain" in proofs and registry:
        legal_name = str((registry.get("value") or {}).get("navn") or "")
        evidence_ids.append(envelope.cite(registry, f"navn: {legal_name} (no other entity with this distinctive name in the registry snapshot)", proof="unique_legal_name_domain"))
    if proofs & STRONG_WEBSITE_PROOFS:
        confidence = 0.99
    elif "registry_declared_website" in proofs:
        confidence = 0.95
    else:
        confidence = 0.9
    envelope.claim(
        "official_website",
        value.get("final_url"),
        "available",
        evidence_ids,
        confidence=confidence,
        identity_proofs=sorted(proofs),
        candidate_source=value.get("candidate_source"),
    )


def _social(envelope: _Envelope, record: Mapping[str, Any] | None) -> None:
    state = availability(record)
    profiles = ((record or {}).get("value") or {}).get("profiles") or []
    if state != "available" or not profiles:
        envelope.unavailable("social_profiles", record, "not_available" if state == "available" else state)
        return
    for item in profiles:
        envelope.claim(
            "social_profile",
            {"platform": item.get("platform"), "url": item.get("url")},
            "available",
            [envelope.cite(record, item.get("claim_span") or item.get("url"))],
            confidence=0.95,
            relation="linked_from_verified_company_website",
        )


def build_envelope(
    profile: Mapping[str, Any],
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
    operations: Mapping[str, Any],
    changes: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    records = profile.get("evidence") or {}
    envelope = _Envelope()
    _registry(envelope, records.get("registry"))
    _financials(envelope, records.get("financials"))
    _financial_history(envelope, records.get("financial_history"))
    _listed(envelope, records.get("roles"), module="roles", key="roles", field="role", span=lambda item: f"{item.get('role')}: {item.get('name')}")
    _listed(envelope, records.get("locations"), module="locations", key="locations", field="registered_workplace", span=lambda item: f"Underenhet {item.get('organisation_number')} {item.get('name')}")
    _website(envelope, records.get("website"), records.get("registry"))
    _social(envelope, records.get("social_profiles"))
    return {
        "organisation_number": profile["organisation_number"],
        "legal_name": profile.get("name"),
        "run": {"run_id": run_id, "started_at": started_at, "completed_at": completed_at, "terminal_status": "completed"},
        "modules": {module: availability(records.get(module)) for module in MODULES},
        "claims": envelope.claims,
        "evidence": sorted(envelope.evidence.values(), key=lambda item: item["id"]),
        "changes": list(changes),
        "errors": envelope.errors,
        "operations": dict(operations),
    }


def validate_envelope(envelope: Mapping[str, Any]) -> list[str]:
    """Hard-gate problems in one envelope; an empty list means it passes."""
    problems = []
    evidence = {item["id"]: item for item in envelope.get("evidence") or []}
    for item in evidence.values():
        if not item.get("source_url") or not item.get("retrieved_at"):
            problems.append(f"{item['id']}: evidence without source_url or retrieved_at")
    for module, state in (envelope.get("modules") or {}).items():
        if state not in CONTRACT_STATES:
            problems.append(f"module {module}: invalid state {state!r}")
    for claim in envelope.get("claims") or []:
        field, state = claim.get("field"), claim.get("availability")
        if state not in CONTRACT_STATES:
            problems.append(f"{field}: invalid state {state!r}")
        if any(evidence_id not in evidence for evidence_id in claim.get("evidence_ids") or []):
            problems.append(f"{field}: dangling evidence id")
        if state == "available":
            if claim.get("value") is None:
                problems.append(f"{field}: available claim without a value")
            if not claim.get("evidence_ids"):
                problems.append(f"{field}: available claim without evidence")
            if str(field).startswith("financials.") and field != "financials.filed_years" and not (claim.get("reporting_period") or {}).get("end"):
                problems.append(f"{field}: financial claim without reporting period")
        elif claim.get("value") not in (None, [], {}):
            problems.append(f"{field}: {state} claim carries a value")
    return problems
