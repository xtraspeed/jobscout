"""Retry classification and exponential backoff with full jitter."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})
#: Statuses we stop retrying immediately: the request itself is wrong.
FATAL_STATUS: Final = frozenset({400, 401, 403, 404, 405, 410, 451})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to retry, how long to wait, and what is worth retrying."""

    max_retries: int = 4
    base: float = 0.5
    cap: float = 30.0
    retry_statuses: frozenset[int] = RETRYABLE_STATUS
    retry_exceptions: tuple[type[BaseException], ...] = (TimeoutError, OSError)

    def should_retry_status(self, status_code: int) -> bool:
        return status_code in self.retry_statuses

    def should_retry_exception(self, exc: BaseException) -> bool:
        return isinstance(exc, self.retry_exceptions)

    def backoff(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Exponential backoff for ``attempt`` (0-based), capped and jittered.

        Uses "full jitter": ``random.uniform(0, min(cap, base * 2**attempt))``.
        Spreading retries avoids a thundering herd when a host recovers.
        """
        ceiling = min(self.cap, self.base * (2**attempt))
        source = rng or random
        return source.uniform(0.0, ceiling)


def retry_after_seconds(
    headers: dict[str, str] | None, *, now: float | None = None
) -> float | None:
    """Parse ``Retry-After`` as delta-seconds or an HTTP date.

    A server-supplied delay is authoritative: it is a politeness instruction,
    so we honour it and skip our own backoff entirely.
    """
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    value = raw.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None

    if now is not None:
        reference = datetime.fromtimestamp(now, tz=UTC)
    else:
        reference = datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - reference).total_seconds())
