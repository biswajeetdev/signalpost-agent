"""Hiring signals from NAV's public job board search (arbeidsplassen.nav.no, robots.txt allows the search
and ad pages), one search per company on its own threads. It needs no feed token and no full feed
read, so it still works on days the NAV feed is slow or down, and it lists ads NAV imports from FINN.

A search hit is only a candidate: it must be ACTIVE, unexpired and name this exact legal name as its
employer; its ad page must then state an employer organisation number. That number is matched when
the record is attached, against the entity and its registered subunits (the rule the NAV feed uses)."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Mapping

from .evidence import evidence, utc_now
from .jobs_nav import _subunit_numbers
from .news_search import Fetch, _robots_allowed, default_fetch

SEARCH_URL = "https://arbeidsplassen.nav.no/stillinger/api/search?q={query}"
AD_URL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
SOURCE_CLASS = "nav_job_board_search"
MAX_AD_PAGES = 5  # name-matched hits read per company
MIN_INTERVAL_SECONDS = 2.0  # run-wide: the board answers bursts with HTTP 429 for minutes
EMPLOYER = re.compile(r'"employer":\{"orgnr":"(\d{9})","name":"([^"]*)"')


def _norm(name: str) -> str:
    return " ".join(str(name or "").casefold().split())


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def matching_hits(body: Mapping[str, Any], legal_name: str, now: datetime) -> list[Mapping[str, Any]]:
    """Active, unexpired hits whose employer (or business) name is exactly the legal name."""
    wanted = _norm(legal_name)
    hits = []
    for hit in ((body.get("hits") or {}).get("hits") or []):
        source = hit.get("_source") or {}
        names = {_norm((source.get("employer") or {}).get("name")), _norm(source.get("businessName"))}
        expires = _moment(source.get("expires"))
        if source.get("status") == "ACTIVE" and wanted in names and (expires is None or expires > now) and source.get("uuid"):
            hits.append(source)
    return hits


def employer_on_page(page: str) -> tuple[str, str] | None:
    """(organisation number, name) of the employer as the ad page states it."""
    match = EMPLOYER.search(page.replace('\\"', '"'))
    return (match.group(1), match.group(2)) if match else None


class RateLimited(Exception):
    """The board answered HTTP 429: stop searching it for the rest of the run."""


def board_candidates(legal_name: str, *, spend: Any, fetch: Fetch = default_fetch, now: datetime | None = None) -> dict[str, Any]:
    """Candidate ads for one legal name: {"state": ..., "ads": [...], "note": ...}. Never raises."""
    now = now or datetime.now(timezone.utc)
    url = SEARCH_URL.format(query=urllib.parse.quote(f'"{legal_name}"'))
    try:
        if not _robots_allowed(url, fetch, spend):
            return {"state": "blocked", "ads": [], "note": "Job board search not permitted by robots.txt or unreachable"}
        status, body = fetch(url, spend)
        if status == 429:
            raise RateLimited("Job board search rate-limited (HTTP 429)")
        if status != 200:
            return {"state": "failed", "ads": [], "note": f"Job board search HTTP {status}"}
        hits = matching_hits(json.loads(body.decode("utf-8", errors="replace")), legal_name, now)
        ads = []
        for hit in hits[:MAX_AD_PAGES]:
            page_url = AD_URL.format(uuid=hit["uuid"])
            if not _robots_allowed(page_url, fetch, spend):
                continue
            retrieved_at = utc_now()
            page_status, page = fetch(page_url, spend)
            if page_status == 429:
                raise RateLimited("Job board ad page rate-limited (HTTP 429)")
            employer = employer_on_page(page.decode("utf-8", errors="replace")) if page_status == 200 else None
            if not employer:
                continue
            ads.append({
                "uuid": hit["uuid"],
                "title": hit.get("title"),
                "employer_name": employer[1],
                "employer_organisation_number": employer[0],
                "published": hit.get("published"),
                "expires": hit.get("expires"),
                "url": page_url,
                "source_url": page_url,
                "source_class": SOURCE_CLASS,
                "retrieved_at": retrieved_at,
                "content_sha256": hashlib.sha256(page).hexdigest(),
                "claim_span": f"employer.orgnr: {employer[0]}; title: {hit.get('title')}",
            })
        return {"state": "ok", "ads": ads, "note": f"{len(hits)} active name-matched ad(s) on the job board"}
    except RateLimited:
        raise
    except Exception as exc:  # budget, network or malformed response: no claim either way
        return {"state": "failed", "ads": [], "note": f"Job board search failed: {type(exc).__name__}: {str(exc)[:120]}"}


def attach_board_ads(jobs: Mapping[str, Any] | None, candidates: Mapping[str, Any] | None, org: str, locations: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The company's jobs record with job-board ads whose employer is the entity or a registered subunit
    added (deduplicated by ad id). Returns None when nothing changes."""
    if not candidates or candidates.get("state") != "ok":
        return None
    employers = {org, *_subunit_numbers(locations)}
    matched = [{**ad, "employer_is_subunit": ad["employer_organisation_number"] != org}
               for ad in candidates["ads"] if ad["employer_organisation_number"] in employers]
    current = dict(jobs or {})
    existing = list(((current.get("value") or {}).get("ads")) or []) if current.get("status") == "available" else []
    seen = {ad.get("uuid") for ad in existing}
    added = [ad for ad in matched if ad["uuid"] not in seen]
    if added:
        note = "; ".join(part for part in (current.get("note") if existing else None, f"{len(added)} ad(s) from the NAV job board search") if part)
        if existing:
            return {**current, "value": {"ads": existing + added}, "note": note}
        return evidence("jobs", "available", SOURCE_CLASS, SEARCH_URL.format(query=""), value={"ads": added}, note=note)
    if current.get("status") in (None, "failed"):
        # The feed could not answer, but the board search did: no active ad names this entity.
        return evidence("jobs", "not_available", SOURCE_CLASS, SEARCH_URL.format(query=""),
                        note="; ".join(part for part in (current.get("note"), candidates.get("note"), "no active job-board ad names this entity or a registered subunit as employer") if part))
    return None


class BoardPrefetcher:
    """Job-board searches on their own threads from the start of the run, in input order; attached after
    the batch, once the registered subunits are known."""

    def __init__(self, companies: list[tuple[str, str]], budget: Any, *, fetch: Fetch = default_fetch, threads: int = 1,
                 margin_seconds: float = 60.0, min_interval: float = MIN_INTERVAL_SECONDS) -> None:
        self.companies = companies
        self.budget = budget
        self.fetch = self._paced(fetch, min_interval)
        self.note: str | None = None
        self.threads = threads
        self.margin_seconds = margin_seconds
        self.results: dict[str, dict[str, Any]] = {}
        self.done = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._next = 0
        self._running = 0

    def start(self) -> "BoardPrefetcher":
        self._running = self.threads
        for index in range(self.threads):
            threading.Thread(target=self._run, name=f"board-prefetch-{index}", daemon=True).start()
        if not self.threads:
            self.done.set()
        return self

    @staticmethod
    def _paced(fetch: Fetch, interval: float) -> Fetch:
        lock, last = threading.Lock(), [0.0]

        def paced(url: str, spend: Any) -> tuple[int, bytes]:
            with lock:
                slot = max(last[0] + interval, time.monotonic())
                last[0] = slot
            time.sleep(max(slot - time.monotonic(), 0.0))
            return fetch(url, spend)
        return paced

    def _take(self) -> tuple[str, str] | None:
        with self._lock:
            if self._stop.is_set() or self._next >= len(self.companies) or self.budget.seconds_left() < self.margin_seconds:
                return None
            self._next += 1
            return self.companies[self._next - 1]

    def _run(self) -> None:
        try:
            while (company := self._take()) is not None:
                org, name = company
                try:
                    found = board_candidates(name, spend=lambda org=org: self.budget.spend(org, "job_board"), fetch=self.fetch) if name else None
                except RateLimited as exc:  # circuit breaker: never keep hitting a board that asked us to slow down
                    self.note = f"{exc}; stream stopped after {len(self.results)} companies"
                    self._stop.set()
                    break
                with self._lock:
                    self.results[org] = found or {"state": "skipped", "ads": []}
        finally:
            with self._lock:
                self._running -= 1
                if self._running == 0:
                    self.done.set()

    def stop(self) -> None:
        self._stop.set()

    def record(self, org: str) -> dict[str, Any] | None:
        with self._lock:
            return self.results.get(org)
