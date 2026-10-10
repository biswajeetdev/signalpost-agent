"""Website candidates from the 1881.no directory (robots.txt allows the organisation-number search).
Candidates only: a domain is never published unless the site itself proves the entity (organisation
number, registry email or registry phone on the page, or the registry-declared / unique-name rules),
exactly like every other candidate source. Nothing read on 1881 is stored as a claim or evidence.

One lookup per company on its own threads from the start of the run, in input order; a worker uses
the candidates if they are ready and never waits for them."""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from typing import Any

from .candidates import registered_domain
from .news_search import Fetch, _robots_allowed, default_fetch

LOOKUP_URL = "https://www.1881.no/?query={org}"
MIN_INTERVAL_SECONDS = 1.0
MAX_CANDIDATES = 3
# Domains 1881 links from most company pages (its own services, partners, advertisers, social sites):
# never this company's website. Seen on 3+ of 164 lookups on 10 Oct 2026; the run also counts repeats.
BOILERPLATE = frozenset({
    "anbudstorget.no", "black-friday-norge.no", "blomster.no", "eiendomspriser.no", "facebook.com", "fixa.no",
    "google.com", "hjemmesidehuset.no", "linkedin.com", "prisradar.no", "regnskapstall.no", "tfinans.no",
    "tjenestetorget.no", "tjenestetorvet.dk", "youtube.com", "fonecta1881group.no", "instagram.com", "x.com",
    "twitter.com", "apple.com", "microsoft.com", "aarsleffrail.no",
})
REPEAT_LIMIT = 3  # a domain linked from this many different companies' pages is directory furniture


class RateLimited(Exception):
    """The directory answered HTTP 429: stop using it for the rest of the run."""


def page_domains(page: str) -> set[str]:
    domains = set()
    for href in re.findall(r'href="(https?://[^"]+)"', page):
        domain = registered_domain(href)
        if domain and "1881" not in domain and domain not in BOILERPLATE:
            domains.add(domain)
    return domains


def lookup(org: str, *, spend: Any, fetch: Fetch = default_fetch) -> set[str] | None:
    """Outbound domains on the company's directory page; None when the directory could not answer."""
    url = LOOKUP_URL.format(org=org)
    if not _robots_allowed(url, fetch, spend):
        return None
    status, body = fetch(url, spend)
    if status == 429:
        raise RateLimited("1881.no rate-limited (HTTP 429)")
    if status != 200:
        return None
    return page_domains(body.decode("utf-8", errors="replace"))


class DirectoryPrefetcher:
    def __init__(self, organisations: list[str], budget: Any, *, fetch: Fetch = default_fetch, threads: int = 2,
                 margin_seconds: float = 300.0, min_interval: float = MIN_INTERVAL_SECONDS) -> None:
        self.organisations = organisations
        self.budget = budget
        self.fetch = self._paced(fetch, min_interval)
        self.threads = threads
        self.margin_seconds = margin_seconds
        self.results: dict[str, set[str] | None] = {}
        self.seen: Counter[str] = Counter()
        self.note: str | None = None
        self.failures = 0
        self.done = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._next = 0
        self._running = 0

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

    def start(self) -> "DirectoryPrefetcher":
        self._running = self.threads
        for index in range(self.threads):
            threading.Thread(target=self._run, name=f"directory-prefetch-{index}", daemon=True).start()
        if not self.threads:
            self.done.set()
        return self

    def _take(self) -> str | None:
        with self._lock:
            # Website discovery happens inside the batch: lookups that finish later than this are useless.
            if self._stop.is_set() or self._next >= len(self.organisations) or self.budget.seconds_left() < self.margin_seconds:
                return None
            self._next += 1
            return self.organisations[self._next - 1]

    def _run(self) -> None:
        try:
            while (org := self._take()) is not None:
                try:
                    found = lookup(org, spend=lambda org=org: self.budget.spend(org, "directory"), fetch=self.fetch)
                except RateLimited as exc:  # circuit breaker
                    self.note = f"{exc}; stopped after {len(self.results)} companies"
                    self._stop.set()
                    break
                except Exception:
                    found = None
                with self._lock:
                    self.results[org] = found
                    if found is None:
                        self.failures += 1
                    else:
                        self.seen.update(found)
        finally:
            with self._lock:
                self._running -= 1
                if self._running == 0:
                    self.done.set()

    def stop(self) -> None:
        self._stop.set()

    def candidates(self, org: str) -> list[str]:
        """Candidate domains for this company if its lookup is done (never waits)."""
        with self._lock:
            found = self.results.get(org) or set()
            return sorted(domain for domain in found if self.seen[domain] < REPEAT_LIMIT)[:MAX_CANDIDATES]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"looked_up": len(self.results), "of": len(self.organisations), "failed": self.failures, "note": self.note}
