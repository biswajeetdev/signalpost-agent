from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Callable

# Locked evaluator budget: every attempt, retry and redirect hop is one outbound request.
RUN_REQUEST_LIMIT = 2000
RUN_SECONDS_LIMIT = 45 * 60


class BudgetExhausted(RuntimeError):
    """Raised before an outbound request that would exceed the run or company allowance."""


class RequestBudget:
    def __init__(
        self,
        max_requests: int = RUN_REQUEST_LIMIT,
        max_seconds: float = RUN_SECONDS_LIMIT,
        *,
        reserve_fraction: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_requests = max_requests
        self.deadline = clock() + max_seconds
        self.clock = clock
        # Held back so every company can still emit a terminal envelope near the limit.
        self.reserve = int(max_requests * reserve_fraction)
        self.used = 0
        self.by_company: Counter[str] = Counter()
        self.by_purpose: Counter[str] = Counter()
        # Companies still due a call to a paced (rate-limited) endpoint; set by the runner.
        self.paced_pending = 0
        # Wall-clock cap on one company's non-essential (website) requests, so one slow site cannot hold a
        # whole chunk; official registry calls are exempt. None disables it.
        self.company_seconds: float | None = None
        self._company_started: dict[str, float] = {}
        self._lock = threading.Lock()

    def take_paced_slot(self, seconds_per_call: float, margin_seconds: float = 90.0) -> bool:
        """Claim one company's paced call only if the whole remaining paced backlog still fits the
        wall-clock budget; otherwise skip it so the run finishes on time rather than timing out."""
        with self._lock:
            pending = max(self.paced_pending, 1)
            self.paced_pending = max(self.paced_pending - 1, 0)
        return self.seconds_left() - margin_seconds >= pending * seconds_per_call

    def remaining(self) -> int:
        with self._lock:
            return self.max_requests - self.used

    def seconds_left(self) -> float:
        return self.deadline - self.clock()

    def spend(self, company: str, purpose: str, *, allowance: int | None = None, essential: bool = False) -> None:
        """Count one outbound request, or raise without counting it.

        `essential` requests (official registry calls) may draw on the reserve; discovery may not.
        """
        with self._lock:
            ceiling = self.max_requests if essential else self.max_requests - self.reserve
            if self.used >= ceiling:
                raise BudgetExhausted(f"run request budget exhausted ({self.used}/{self.max_requests})")
            if self.clock() >= self.deadline:
                raise BudgetExhausted("run wall-clock budget exhausted")
            if allowance is not None and not essential and self.by_company[company] >= allowance:
                raise BudgetExhausted(f"company allowance exhausted ({allowance})")
            # Only per-company website requests (those with an allowance) are time-capped; run-level readers
            # such as the NAV feed ('_run') and job-detail lookups are not company website work.
            if not essential and allowance is not None and self.company_seconds is not None:
                # The clock starts at the company's first website request (official calls, including the
                # history prefetch that runs ahead of workers, never start or stop it).
                started = self._company_started.setdefault(company, self.clock())
                if self.clock() - started > self.company_seconds:
                    raise BudgetExhausted(f"company time allowance exhausted ({self.company_seconds:.0f}s)")
            self.used += 1
            self.by_company[company] += 1
            self.by_purpose[purpose] += 1

    def charge_prior(self, requests: int, seconds: float) -> None:
        """Count requests and wall-clock already spent in reused (checkpointed) chunks against this run."""
        with self._lock:
            self.used += requests
            self.deadline -= seconds

    def report(self) -> dict[str, object]:
        with self._lock:
            counts = sorted(self.by_company.values())
            return {
                "requests": self.used,
                "limit": self.max_requests,
                "by_purpose": dict(self.by_purpose),
                "per_company_p50": counts[len(counts) // 2] if counts else 0,
                "per_company_max": counts[-1] if counts else 0,
            }


class RobotsCache:
    """One robots.txt fetch per scheme+host for the whole run."""

    def __init__(self) -> None:
        self._parsers: dict[str, object] = {}
        self._lock = threading.Lock()

    def get(self, key: str, loader: Callable[[], object]) -> object:
        with self._lock:
            if key in self._parsers:
                return self._parsers[key]
        parser = loader()
        with self._lock:
            return self._parsers.setdefault(key, parser)
