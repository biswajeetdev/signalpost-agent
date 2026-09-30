from __future__ import annotations

import json
import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass
class FetchResult:
    url: str
    status: int
    elapsed_ms: int
    bytes_received: int
    body: Any = None
    error: str | None = None
    content_sha256: str | None = None
    retrieved_at: str | None = None
    effective_at: str | None = None


MAX_FETCH_SECONDS = 20.0
MAX_JSON_BYTES = 50_000_000


class SlowResponse(OSError):
    """A server kept the connection alive but did not finish the body within the wall-clock limit."""


def read_bounded(response: Any, limit: int, max_seconds: float = MAX_FETCH_SECONDS) -> bytes:
    """Read at most `limit` bytes, giving up after `max_seconds` of wall clock. The socket timeout only
    bounds each read, so a server trickling bytes could otherwise hold a worker indefinitely."""
    deadline = time.monotonic() + max_seconds
    chunks: list[bytes] = []
    size = 0
    while size < limit:
        if time.monotonic() > deadline:
            raise SlowResponse(f"body not complete within {max_seconds:.0f}s")
        chunk = response.read(min(65536, limit - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_json(url: str, *, timeout: float = 20.0, attempts: int = 3, on_attempt: Callable[[], None] | None = None, headers: dict[str, str] | None = None) -> FetchResult:
    """`on_attempt` runs before every attempt (retries included), e.g. to charge a request budget."""
    last_error = "request failed"
    for attempt in range(attempts):
        if on_attempt is not None:
            on_attempt()
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = read_bounded(response, MAX_JSON_BYTES, max(timeout * 3, MAX_FETCH_SECONDS))
                elapsed = int((time.monotonic() - started) * 1000)
                return FetchResult(url, response.status, elapsed, len(raw), json.loads(raw), content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raw = exc.read()
            if exc.code in {404, 410}:
                return FetchResult(url, exc.code, elapsed, len(raw), error=f"HTTP {exc.code}", content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:  # SlowResponse is an OSError
            last_error = type(exc).__name__
        if attempt + 1 < attempts:
            time.sleep(0.4 * (2**attempt))
    return FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=_utc_now())
