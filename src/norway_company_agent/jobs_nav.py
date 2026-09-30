"""Hiring signals from NAV's public job-vacancy feed (arbeidsplassen.nav.no, open data).

One index per run: the feed is read from a look-back cutoff with If-Modified-Since, keeping the
latest state of every ad. Ads still ACTIVE become candidates when their business name matches a
company's legal name or one of its registered workplaces (name keys only generate candidates).
An ad is published only after its detail record names the company, or one of its registered
subunits, as employer by organisation number. Name similarity alone never publishes.
"""
from __future__ import annotations

import re
import threading
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Callable, Iterable, Mapping

from .budget import BudgetExhausted
from .evidence import evidence, utc_now
from .http import FetchResult, fetch_json
from .proof import name_key

FEED_ROOT = "https://pam-stilling-feed.nav.no"
TOKEN_URL = FEED_ROOT + "/api/publicToken"
FEED_URL = FEED_ROOT + "/api/v1/feed"
SOURCE_CLASS = "nav_public_job_feed"
MAX_ADS_PER_COMPANY = 25
MAX_DETAIL_FETCHES_PER_COMPANY = 30


def _public_token(on_attempt: Callable[[], None] | None = None) -> str | None:
    """NAV publishes a rotating public token for the feed; it is not an account credential."""
    if on_attempt:
        on_attempt()
    request = urllib.request.Request(TOKEN_URL, headers={"User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8", "replace")
    except Exception:
        return None
    match = re.search(r"eyJ[\w\-]+\.[\w\-]+\.[\w\-]+", text)
    return match.group(0) if match else None


class NavJobIndex:
    """Active NAV ads for one run, keyed by business-name key; details fetched on demand and cached."""

    def __init__(
        self,
        *,
        lookback_days: int = 60,
        max_pages: int = 450,
        spend: Callable[[str], None] | None = None,
        fetcher: Callable[..., FetchResult] = fetch_json,
        token: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self.lookback_days = lookback_days
        self.max_pages = max_pages
        self._spend = spend or (lambda purpose: None)
        self._fetch = fetcher
        self._token = token
        self._now = now
        self.by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.state = "not_built"
        self.note: str | None = None
        self.pages = 0
        self.active_ads = 0
        self.built_at: str | None = None
        self._details: dict[str, FetchResult] = {}
        self._lock = threading.Lock()
        self.ready = threading.Event()

    def build_in_background(self) -> "NavJobIndex":
        """Read the feed on its own thread so company work starts at once; `ready` is set when done."""
        def run() -> None:
            try:
                self.build()
            except Exception as exc:  # budget exhausted or network failure mid-read
                self.state, self.note = "failed", f"NAV feed read failed: {type(exc).__name__}: {str(exc)[:160]}"
            finally:
                self.ready.set()

        threading.Thread(target=run, name="nav-job-feed", daemon=True).start()
        return self

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def build(self) -> "NavJobIndex":
        token = self._token or _public_token(lambda: self._spend("jobs_feed"))
        if not token:
            self.state, self.note = "failed", "NAV public feed token unavailable"
            return self
        self._token = token
        since = (self._now or datetime.now(timezone.utc)) - timedelta(days=self.lookback_days)
        headers = {**self._headers(), "If-Modified-Since": format_datetime(since, usegmt=True)}
        latest: dict[str, dict[str, Any]] = {}
        url: str | None = FEED_URL
        while url and self.pages < self.max_pages:
            result = self._fetch(url, attempts=2, headers=headers, on_attempt=lambda: self._spend("jobs_feed"))
            if result.status != 200 or not isinstance(result.body, dict):
                if not latest:
                    self.state, self.note = "failed", f"NAV feed page failed: {result.error or result.status}"
                    return self
                self.note = f"Feed read stopped early after {self.pages} pages: {result.error or result.status}"
                break
            self.pages += 1
            items = result.body.get("items") or []
            for item in items:
                entry = item.get("_feed_entry") or {}
                uuid = entry.get("uuid") or item.get("id")
                if uuid:
                    latest[uuid] = {**entry, "uuid": uuid, "feed_url": item.get("url"), "feed_retrieved_at": result.retrieved_at}
            next_url = result.body.get("next_url")
            if not items or not next_url:
                break
            url = FEED_ROOT + next_url if next_url.startswith("/") else next_url
            headers = self._headers()
        for entry in latest.values():
            if entry.get("status") != "ACTIVE":
                continue
            key = name_key(str(entry.get("businessName") or ""))
            if key:
                self.by_key[key].append(entry)
                self.active_ads += 1
        self.built_at = utc_now()
        self.state = "available"
        self.ready.set()
        return self

    def detail(self, entry: Mapping[str, Any], spend: Callable[[], None] | None = None) -> FetchResult:
        uuid = str(entry["uuid"])
        with self._lock:
            cached = self._details.get(uuid)
        if cached is not None:
            return cached
        path = entry.get("feed_url") or f"/api/v1/feedentry/{uuid}"
        result = self._fetch(FEED_ROOT + path if path.startswith("/") else path, attempts=2, headers=self._headers(),
                             on_attempt=spend or (lambda: self._spend("jobs_detail")))
        with self._lock:
            self._details[uuid] = result
        return result


def _subunit_numbers(locations: Mapping[str, Any] | None) -> dict[str, str]:
    items = ((locations or {}).get("value") or {}).get("locations") or []
    return {str(item.get("organisation_number")): str(item.get("name") or "") for item in items if item.get("organisation_number")}


def company_jobs(
    org: str,
    legal_name: str,
    locations: Mapping[str, Any] | None,
    index: NavJobIndex | None,
    *,
    spend: Callable[[], None] | None = None,
    wait_seconds: float | None = None,
) -> dict[str, Any]:
    """Evidence record for the company's active NAV ads, matched by employer organisation number."""
    if index is not None and not index.ready.is_set() and index.state == "not_built":
        if wait_seconds is not None and not index.ready.wait(max(wait_seconds, 0.0)):
            record = evidence("jobs", "failed", SOURCE_CLASS, FEED_URL, note="Deferred: NAV job feed still being read; filled after the batch if it finishes in time")
            record["deferred"] = True
            return record
    if index is None or index.state != "available":
        return evidence("jobs", "failed", SOURCE_CLASS, FEED_URL, note=(index.note if index else None) or "NAV job feed not read in this run")
    subunits = _subunit_numbers(locations)
    employer_numbers = {org, *subunits}
    keys = {name_key(legal_name), *(name_key(name) for name in subunits.values())} - {""}
    candidates: dict[str, Mapping[str, Any]] = {}
    for key in keys:
        for entry in index.by_key.get(key, []):
            candidates[entry["uuid"]] = entry
    ads: list[dict[str, Any]] = []
    rejected = 0
    budget_note = None
    ordered = sorted(candidates.values(), key=lambda item: str(item.get("sistEndret") or ""), reverse=True)
    for attempt, entry in enumerate(ordered):
        # Cap detail fetches, not only accepted ads: a generic name key must not drain the company allowance.
        if len(ads) >= MAX_ADS_PER_COMPANY or attempt >= MAX_DETAIL_FETCHES_PER_COMPANY:
            break
        try:
            result = index.detail(entry, spend)
        except BudgetExhausted:
            budget_note = "request budget exhausted before every candidate ad was checked"
            break
        body = result.body if isinstance(result.body, dict) else {}
        content = body.get("ad_content") or {}
        employer = content.get("employer") or {}
        employer_org = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
        if result.status != 200 or body.get("status") not in (None, "ACTIVE") or employer_org not in employer_numbers:
            rejected += 1
            continue
        locations_out = [
            {key: place.get(key) for key in ("city", "municipal", "county", "postalCode") if place.get(key)}
            for place in content.get("workLocations") or []
        ]
        ads.append({
            "uuid": entry["uuid"],
            "title": content.get("title") or entry.get("title"),
            "employer_name": employer.get("name"),
            "employer_organisation_number": employer_org,
            "employer_is_subunit": employer_org != org,
            "published": content.get("published"),
            "expires": content.get("expires"),
            "updated": content.get("updated"),
            "positions": content.get("positioncount"),
            "occupations": [item.get("level2") or item.get("level1") for item in content.get("occupationCategories") or [] if item],
            "work_locations": locations_out,
            "url": content.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{entry['uuid']}",
            "application_url": content.get("applicationUrl") or content.get("sourceurl"),
            "employer_homepage": employer.get("homepage"),
            "source_url": result.url,
            "retrieved_at": result.retrieved_at,
            "content_sha256": result.content_sha256,
            "claim_span": f"employer.orgnr: {employer_org}; title: {content.get('title') or entry.get('title')}",
        })
    note_parts = [f"NAV feed read {index.pages} pages from a {index.lookback_days}-day look-back ({index.active_ads} active ads)"]
    if rejected:
        note_parts.append(f"{rejected} name-matched ads rejected: employer organisation number did not match")
    if budget_note:
        note_parts.append(budget_note)
        if not ads:
            return evidence("jobs", "failed", SOURCE_CLASS, FEED_URL, note="; ".join(note_parts), retrieved_at=index.built_at)
    if not ads:
        return evidence("jobs", "not_available", SOURCE_CLASS, FEED_URL, note="; ".join(note_parts + ["no active ad names this entity as employer"]), retrieved_at=index.built_at)
    return evidence("jobs", "available", SOURCE_CLASS, FEED_URL, value={"ads": ads}, note="; ".join(note_parts), retrieved_at=index.built_at)


def employer_homepages(record: Mapping[str, Any] | None) -> list[str]:
    """Employer homepages NAV lists for ads already matched to this exact entity: website candidates only."""
    ads = ((record or {}).get("value") or {}).get("ads") or []
    return sorted({str(ad["employer_homepage"]) for ad in ads if ad.get("employer_homepage") and not ad.get("employer_is_subunit")})


def iter_ads(record: Mapping[str, Any] | None) -> Iterable[Mapping[str, Any]]:
    return ((record or {}).get("value") or {}).get("ads") or []
