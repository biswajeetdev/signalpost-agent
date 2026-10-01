"""Filing-history fetches on their own thread. The annual-account copy endpoint is paced run-wide (one
request start per 2.1 s), so fetching it inside company workers made every worker wait in that queue:
throughput was capped at one company per 2.1 s. Here one thread walks the batch in input order from
the start of the run while workers do everything else; the results are attached after the batch."""
from __future__ import annotations

import threading
from typing import Any, Callable, Iterable

from .budget import BudgetExhausted, RequestBudget
from .evidence import evidence
from .official import fetch_official_modules

HISTORY_SOURCE = "official_annual_account_copies"
HISTORY_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"


class HistoryPrefetcher:
    def __init__(self, organisations: Iterable[str], budget: RequestBudget, fetcher_for: Callable[[RequestBudget, str], Callable[[str], Any]],
                 *, seconds_per_call: float = 2.1, margin_seconds: float = 60.0) -> None:
        self.organisations = list(organisations)
        self.budget = budget
        self.fetcher_for = fetcher_for
        self.seconds_per_call = seconds_per_call
        self.margin_seconds = margin_seconds
        self.results: dict[str, dict[str, Any]] = {}
        self.done = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> "HistoryPrefetcher":
        threading.Thread(target=self._run, name="history-prefetch", daemon=True).start()
        return self

    def _run(self) -> None:
        try:
            for org in self.organisations:
                if self._stop.is_set() or self.budget.seconds_left() < self.margin_seconds + self.seconds_per_call:
                    break
                try:
                    records, _ = fetch_official_modules(org, {"financial_history"}, fetcher=self.fetcher_for(self.budget, org))
                except BudgetExhausted:
                    break
                with self._lock:
                    self.results[org] = records["financial_history"]
        finally:
            self.done.set()

    def stop(self) -> None:
        self._stop.set()

    def record(self, org: str) -> dict[str, Any] | None:
        with self._lock:
            return self.results.get(org)

    def final_record(self, org: str) -> dict[str, Any]:
        """The fetched record, or an explicit failure when the paced stream did not reach this company."""
        found = self.record(org)
        if found is not None:
            return found
        return evidence("financial_history", "failed", HISTORY_SOURCE, HISTORY_URL.format(org=org),
                        note="Deferred: the rate-limited annual-account copy endpoint did not reach this company within the run time budget")
