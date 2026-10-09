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
MODULES = ("registry", "financials", "financial_history", "roles", "locations", "website", "social_profiles", "jobs", "site_jobs", "public_activity", "news_mentions")
OPTIONAL_MODULES = frozenset({"jobs", "site_jobs", "public_activity", "news_mentions"})
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
STRONG_WEBSITE_PROOFS = {"organisation_number", "subunit_organisation_number", "registry_email", "registry_phone"}


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
    live = record.get("contact_fields_source") or {}
    live_record = {"source_url": live.get("url"), "source_class": "official_registry_live", "retrieved_at": live.get("retrieved_at"),
                   "content_sha256": live.get("content_sha256"), "source_row_key": record.get("source_row_key")} if live else None

    def source_for(column: str) -> Mapping[str, Any]:
        """Columns filled from the live entity endpoint cite it, not the bulk snapshot."""
        return live_record if live_record and column in (live.get("fields") or []) else record

    for field, column in REGISTRY_FIELDS:
        value = str(raw.get(column) or "").strip()
        if not value:
            envelope.claim(field, None, "not_available", [envelope.cite(source_for(column), f"{column}: (empty)")], note="Empty in the official registry snapshot")
            continue
        typed: Any = int(value) if field == "registered_employees" and value.isdigit() else value
        envelope.claim(field, typed, "available", [envelope.cite(source_for(column), f"{column}: {value}")], confidence=1.0)
    address = {part: str(raw.get(f"forretningsadresse.{part}") or "").strip() or None for part in ADDRESS_PARTS}
    address_source = source_for("forretningsadresse.adresse")
    if address["adresse"] or address["poststed"]:
        span = ", ".join(value for value in address.values() if value)
        envelope.claim("business_address", address, "available", [envelope.cite(address_source, f"forretningsadresse: {span}")], confidence=1.0)
    else:
        envelope.claim("business_address", None, "not_available", [envelope.cite(address_source, "forretningsadresse: (empty)")])
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


def canonical_social_url(url: str, platform: str | None) -> str:
    """Lower-case host and handle; YouTube paths keep their case (channel ids are case-sensitive)."""
    parts = str(url).split("/", 3)
    if len(parts) < 4:
        return str(url).lower()
    head = "/".join(parts[:3]).lower()
    return f"{head}/{parts[3] if platform == 'youtube' else parts[3].lower()}"


def _social(envelope: _Envelope, record: Mapping[str, Any] | None) -> None:
    state = availability(record)
    profiles = ((record or {}).get("value") or {}).get("profiles") or []
    if state != "available" or not profiles:
        envelope.unavailable("social_profile", record, "not_available" if state == "available" else state)
        return
    seen: set[str] = set()
    for item in profiles:
        url = canonical_social_url(str(item.get("url")), item.get("platform"))
        if url in seen:  # the same profile linked with different capitalisation
            continue
        seen.add(url)
        envelope.claim(
            "social_profile",
            url,
            "available",
            [envelope.cite(record, item.get("claim_span") or item.get("url"))],
            confidence=0.95,
            platform=item.get("platform"),
            signal_type="profile_handle",
            relation="linked_from_verified_company_website",
        )


def _jobs(envelope: _Envelope, record: Mapping[str, Any] | None) -> None:
    if record is None:  # connector not run in this configuration
        return
    state = availability(record)
    ads = ((record or {}).get("value") or {}).get("ads") or []
    if state != "available" or not ads:
        envelope.unavailable("hiring_signal", record, "not_available" if state == "available" else state)
        return
    for ad in ads:
        ad_record = {"source_url": ad.get("source_url"), "source_class": "nav_public_job_feed", "retrieved_at": ad.get("retrieved_at"),
                     "content_sha256": ad.get("content_sha256"), "method": "employer_organisation_number_match"}
        envelope.claim(
            "hiring_signal",
            ad.get("url") or ad.get("source_url"),
            "available",
            [envelope.cite(ad_record, ad.get("claim_span"))],
            confidence=0.99,
            signal_type="job_posting",
            title=ad.get("title"),
            published_at=ad.get("published"),
            expires_at=ad.get("expires"),
            posting={key: ad.get(key) for key in ("application_url", "positions", "occupations", "work_locations", "employer_name", "employer_organisation_number") if ad.get(key)},
            effective_at=ad.get("published"),
            relation="registered_subunit_employer" if ad.get("employer_is_subunit") else "exact_employer",
        )


def _site_jobs(envelope: _Envelope, record: Mapping[str, Any] | None, seen_urls: set[str]) -> None:
    """The verified company website's careers page, and postings it lists (JobPosting data or ad links)."""
    if availability(record) != "available":
        return
    value = (record or {}).get("value") or {}
    for ad in value.get("ads") or []:
        if ad.get("url") in seen_urls:
            continue
        seen_urls.add(ad.get("url"))
        page_record = {"source_url": ad.get("source_url"), "source_class": "company_owned_website", "retrieved_at": ad.get("retrieved_at"),
                       "content_sha256": ad.get("content_sha256"), "method": ad.get("extraction")}
        envelope.claim(
            "hiring_signal",
            ad.get("url"),
            "available",
            [envelope.cite(page_record, ad.get("claim_span"))],
            confidence=0.95,
            signal_type="job_posting",
            title=ad.get("title"),
            published_at=ad.get("published"),
            expires_at=ad.get("expires"),
            effective_at=ad.get("published"),
            relation="listed_on_verified_company_website",
        )
    careers = value.get("careers_page")
    if careers and careers.get("url") not in seen_urls:
        page_record = {"source_url": careers.get("source_url"), "source_class": "company_owned_website", "retrieved_at": careers.get("retrieved_at"),
                       "content_sha256": careers.get("content_sha256"), "method": careers.get("extraction")}
        envelope.claim(
            "hiring_signal",
            careers.get("url"),
            "available",
            [envelope.cite(page_record, careers.get("claim_span"))],
            confidence=0.95,
            signal_type="careers_page",
            title="Careers page",
            relation="linked_from_verified_company_website",
        )


def _activity(envelope: _Envelope, record: Mapping[str, Any] | None, news: Mapping[str, Any] | None = None) -> None:
    if record is None and news is None:
        return
    state = availability(record) if record is not None else "not_available"
    items = ((record or {}).get("value") or {}).get("items") or [] if state == "available" else []
    news_items = ((news or {}).get("value") or {}).get("items") or [] if availability(news) == "available" else []
    if not items and not news_items:
        envelope.unavailable("dated_news", record or news, "not_available" if state == "available" else state)
        return
    seen = {str(item.get("url")) for item in items}
    for item in news_items:
        if str(item.get("url")) in seen:
            continue
        seen.add(str(item.get("url")))
        page_record = {"source_url": item.get("source_url"), "source_class": "news_publisher_page", "retrieved_at": item.get("retrieved_at"),
                       "content_sha256": item.get("content_sha256"), "method": "exact_legal_name_in_publisher_headline"}
        envelope.claim(
            "dated_news",
            item.get("url"),
            "available",
            [envelope.cite(page_record, item.get("claim_span"))],
            confidence=0.85,
            signal_type="news_mention",
            title=item.get("title"),
            published_at=item.get("date"),
            effective_at=item.get("date"),
            publisher=item.get("publisher"),
            relation="exact_legal_name_in_news_title",
        )
    for item in items:
        page_record = {"source_url": item.get("source_url"), "source_class": "company_owned_website", "retrieved_at": item.get("retrieved_at"),
                       "content_sha256": item.get("content_sha256"), "method": item.get("extraction")}
        envelope.claim(
            "dated_news",
            item.get("url"),
            "available",
            [envelope.cite(page_record, item.get("claim_span"))],
            confidence=0.9,
            signal_type="public_post",
            title=item.get("title"),
            published_at=item.get("date"),
            effective_at=item.get("date"),
            relation="published_on_verified_company_website",
        )


def _nok(amount: Any) -> str:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    for size, unit in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if abs(value) >= size:
            return f"NOK {value / size:.1f}{unit}"
    return f"NOK {value:.0f}"


def synthesis(claims: list[Mapping[str, Any]], changes: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Decision-useful summary restating only available claims; every point cites their evidence ids."""
    available: dict[str, list[Mapping[str, Any]]] = {}
    for claim in claims:
        if claim.get("availability") == "available":
            available.setdefault(str(claim["field"]), []).append(claim)

    def first(field: str) -> Mapping[str, Any] | None:
        return (available.get(field) or [None])[0]

    def ids(*items: Mapping[str, Any] | None) -> list[str]:
        return sorted({evidence_id for item in items if item for evidence_id in item.get("evidence_ids") or []})

    points: list[dict[str, Any]] = []
    name, form, industry, address = first("legal_name"), first("legal_form"), first("industry"), first("business_address")
    if name:
        text = f"{name['value']}"
        if form:
            text += f" ({form['value']})"
        if industry:
            text += f" operates in {str(industry['value']).lower()}"
        if address and isinstance(address.get("value"), dict):
            place = address["value"].get("poststed") or address["value"].get("kommune")
            if place:
                text += f", registered in {str(place).title()}"
        points.append({"topic": "identity", "text": text + ".", "evidence_ids": ids(name, form, industry, address)})
    for flag, label in (("bankrupt", "is registered as bankrupt"), ("under_liquidation", "is registered as under liquidation")):
        claim = first(flag)
        if claim and claim.get("value") is True:
            points.append({"topic": "risk", "text": f"The company {label}.", "evidence_ids": ids(claim)})
    leaders = [claim for claim in available.get("role", []) if isinstance(claim.get("value"), dict) and str(claim["value"].get("role") or "").lower() in {"daglig leder", "styrets leder", "ceo", "chair", "dagl", "leder"}]
    if leaders:
        def holder(value: Any) -> str:
            return ", ".join(map(str, value)) if isinstance(value, list) else str(value)

        described = "; ".join(f"{claim['value'].get('role')}: {holder(claim['value'].get('name'))}" for claim in leaders[:3])
        points.append({"topic": "leadership", "text": f"Leadership in the official register — {described}.", "evidence_ids": ids(*leaders[:3])})
    revenue, result, equity = first("financials.revenue"), first("financials.annual_result"), first("financials.equity")
    figures = [(label, claim) for label, claim in (("revenue", revenue), ("annual result", result), ("equity", equity)) if claim]
    if figures:
        period = ((figures[0][1].get("reporting_period") or {}).get("end") or "")[:4]
        described = ", ".join(f"{label} {_nok((claim.get('value') or {}).get('amount'))}" for label, claim in figures)
        points.append({"topic": "financials", "text": f"Latest filed accounts{f' ({period})' if period else ''}: {described}.", "evidence_ids": ids(*(claim for _, claim in figures))})
    employees = first("registered_employees")
    if employees:
        points.append({"topic": "size", "text": f"{employees['value']} employees registered in the official register.", "evidence_ids": ids(employees)})
    workplaces = available.get("registered_workplace", [])
    if workplaces:
        points.append({"topic": "locations", "text": f"{len(workplaces)} registered workplace(s).", "evidence_ids": ids(*workplaces[:5])})
    website = first("official_website")
    if website:
        points.append({"topic": "web", "text": f"Verified official website: {website['value']}.", "evidence_ids": ids(website)})
    hiring = available.get("hiring_signal", [])
    jobs = [claim for claim in hiring if claim.get("signal_type") == "job_posting"]
    careers = [claim for claim in hiring if claim.get("signal_type") == "careers_page"]
    if jobs:
        titles = ", ".join(str(claim.get("title")) for claim in jobs[:3])
        points.append({"topic": "hiring", "text": f"Hiring: {len(jobs)} active job ad(s) naming this entity as employer ({titles}).", "evidence_ids": ids(*jobs[:3])})
    elif careers:
        points.append({"topic": "hiring", "text": f"Recruits through a careers page linked from its website: {careers[0]['value']}.", "evidence_ids": ids(careers[0])})
    socials = available.get("social_profile", [])
    if socials:
        platforms = ", ".join(sorted({str(claim.get("platform")) for claim in socials}))
        points.append({"topic": "profiles", "text": f"Social profiles linked from its website: {platforms}.", "evidence_ids": ids(*socials[:5])})
    activity = available.get("dated_news", [])
    if activity:
        latest = max(activity, key=lambda claim: str(claim.get("published_at") or ""))
        where = "in the news" if latest.get("signal_type") == "news_mention" else "on its website"
        points.append({"topic": "activity", "text": f"Most recent dated news {where}: \"{latest.get('title')}\" ({latest.get('published_at')}).", "evidence_ids": ids(latest)})
    changes = list(changes)
    if changes:
        points.append({"topic": "changes", "text": f"{len(changes)} material change(s) since the previous run.", "evidence_ids": []})
    unknowns = [label for field, label in (
        ("official_website", "no verified official website"),
        ("financials.revenue", "no revenue figure in the latest filed accounts"),
        ("hiring_signal", "no hiring signal found"),
        ("dated_news", "no dated news found"),
    ) if field not in available]
    return {
        "text": " ".join(point["text"] for point in points),
        "points": points,
        "unknowns": unknowns,
        "method": "deterministic template over available claims; no model",
    }


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
    changes = list(changes)
    envelope = _Envelope()
    _registry(envelope, records.get("registry"))
    _financials(envelope, records.get("financials"))
    _financial_history(envelope, records.get("financial_history"))
    _listed(envelope, records.get("roles"), module="roles", key="roles", field="role", span=lambda item: f"{item.get('role')}: {item.get('name')}")
    _listed(envelope, records.get("locations"), module="locations", key="locations", field="registered_workplace", span=lambda item: f"Underenhet {item.get('organisation_number')} {item.get('name')}")
    _website(envelope, records.get("website"), records.get("registry"))
    _social(envelope, records.get("social_profiles"))
    site_jobs = records.get("site_jobs")
    site_value = (site_jobs or {}).get("value") or {}
    site_has_ads = availability(site_jobs) == "available" and bool(site_value.get("ads") or site_value.get("careers_page"))
    nav_record = records.get("jobs")
    if site_has_ads and availability(nav_record) != "available":
        nav_record = None  # the site's own postings answer the hiring question; no contradictory "not found" claim
    _jobs(envelope, nav_record)
    _site_jobs(envelope, site_jobs, {str(c["value"]) for c in envelope.claims if c["field"] == "hiring_signal" and c.get("value")})
    _activity(envelope, records.get("public_activity"), records.get("news_mentions"))
    return {
        "organisation_number": profile["organisation_number"],
        "legal_name": profile.get("name"),
        "run": {"run_id": run_id, "started_at": started_at, "completed_at": completed_at, "terminal_status": "completed"},
        "modules": {module: availability(records.get(module)) for module in MODULES if module in records or module not in OPTIONAL_MODULES},
        "claims": envelope.claims,
        "evidence": sorted(envelope.evidence.values(), key=lambda item: item["id"]),
        "changes": list(changes),
        "summary": synthesis(envelope.claims, changes),
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
