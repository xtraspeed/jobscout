"""Per-domain politeness: a token bucket plus a concurrency semaphore.

The clock and sleep functions are injected so tests can advance time
deterministically instead of actually waiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from jobscout.observability import get_logger
from jobscout.observability.metrics import RATE_LIMIT_WAIT

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

_log = get_logger(__name__)


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def host_of(url: str) -> str:
    """Hostname used as the politeness key (port included when non-default)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return url.lower()
    if parts.port and parts.port not in {80, 443}:
        return f"{host}:{parts.port}"
    return host


class TokenBucket:
    """Classic token bucket.

    ``rate`` is tokens per second and ``capacity`` the burst ceiling. Each
    :meth:`acquire` consumes one token, sleeping just long enough to refill
    when the bucket is empty, so a burst of N requests is allowed immediately
    and steady-state traffic is smoothed to ``rate``.
    """

    __slots__ = ("_capacity", "_clock", "_rate", "_sleep", "_tokens", "_updated")

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = _real_sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate
        self._capacity = capacity if capacity is not None else max(1.0, rate)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self._capacity
        self._updated = clock()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def available(self) -> float:
        """Tokens currently in the bucket (for tests/metrics)."""
        return self._tokens

    async def acquire(self, amount: float = 1.0) -> float:
        """Consume ``amount`` tokens, sleeping if needed. Returns seconds waited."""
        if amount > self._capacity:
            raise ValueError("amount exceeds bucket capacity")
        waited = 0.0
        while True:
            self._refill()
            if self._tokens >= amount:
                self._tokens -= amount
                return waited
            deficit = amount - self._tokens
            pause = deficit / self._rate
            waited += pause
            await self._sleep(pause)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)


@dataclass(slots=True)
class HostLimiter:
    """Concurrency semaphore plus rate bucket for one host."""

    semaphore: asyncio.Semaphore
    bucket: TokenBucket


class DomainLimiter:
    """Lazily-created per-host limiters, shared across the whole run.

    Hosts are created on first use so a crawl touching three domains does not
    pay for a limiter per host in the configuration.
    """

    def __init__(
        self,
        *,
        concurrency: int = 2,
        rate: float = 1.0,
        capacity: float | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = _real_sleep,
    ) -> None:
        self._concurrency = concurrency
        self._rate = rate
        self._capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._limiters: dict[str, HostLimiter] = {}

    def _build(self, rate: float) -> HostLimiter:
        return HostLimiter(
            semaphore=asyncio.Semaphore(self._concurrency),
            bucket=TokenBucket(rate, self._capacity, clock=self._clock, sleep=self._sleep),
        )

    def configure(
        self, host: str, *, rate: float | None = None, concurrency: int | None = None
    ) -> None:
        """Override rate/concurrency for one host (used for robots crawl-delay).

        Applied from ``robots.txt`` before that host's first request. Rebuilding
        the limiter is safe there because no request is in flight yet.
        """
        if rate is None and concurrency is None:
            return
        existing = self._limiters.get(host)
        bucket_rate = (
            rate if rate is not None else (existing.bucket.rate if existing else self._rate)
        )
        self._limiters[host] = self._build(bucket_rate)
        if concurrency is not None:
            self._limiters[host].semaphore = asyncio.Semaphore(concurrency)

    def get(self, host: str) -> HostLimiter:
        """Return (creating if needed) the limiter for a host."""
        limiter = self._limiters.get(host)
        if limiter is None:
            limiter = self._build(self._rate)
            self._limiters[host] = limiter
        return limiter

    async def acquire(self, url: str, *, adapter: str = "unknown") -> None:
        """Wait for both a rate token and a concurrency slot for ``url``'s host."""
        host = host_of(url)
        limiter = self.get(host)
        waited = await limiter.bucket.acquire()
        if waited:
            RATE_LIMIT_WAIT.labels(adapter=adapter).observe(waited)
            _log.debug("ratelimit.wait", host=host, waited_seconds=round(waited, 3))
        async with limiter.semaphore:
            return

    @property
    def hosts(self) -> list[str]:
        return sorted(self._limiters)
