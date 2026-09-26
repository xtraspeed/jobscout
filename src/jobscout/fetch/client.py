"""The pooled, polite HTTP client every adapter goes through.

One :class:`httpx.AsyncClient` is shared by the whole run (HTTP/2, keep-alive,
connection pooling) — creating a client per request is the single most common
performance mistake in naive scrapers.

Per request the fetcher enforces, in order:

1. ``robots.txt`` (skipped if disabled),
2. the host circuit breaker,
3. the per-domain rate limiter and concurrency semaphore,
4. bounded retries with ``Retry-After``-aware exponential backoff,
5. raw-body archiving for later re-parsing.

Only idempotent GETs are ever issued, so retrying is always safe.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpx

from jobscout.config import Settings
from jobscout.errors import CircuitOpen, FetchError, RobotsDenied
from jobscout.fetch.breaker import CircuitBreaker
from jobscout.fetch.ratelimit import DomainLimiter, host_of
from jobscout.fetch.retry import RetryPolicy, retry_after_seconds
from jobscout.fetch.robots import RobotsTxt
from jobscout.fetch.snapshot import NullSnapshotStore, SnapshotStore
from jobscout.models import FetchedPage
from jobscout.observability import get_logger
from jobscout.observability.metrics import (
    PAGES_FETCHED,
    REQUEST_LATENCY,
    REQUESTS,
    RETRIES,
)

_log = get_logger(__name__)


@dataclass(slots=True)
class FetchStats:
    """Counters accumulated over a run, surfaced in ``crawl_runs``."""

    requests: int = 0
    retries: int = 0
    failures: int = 0
    robots_denied: int = 0
    circuit_skips: int = 0
    bytes_downloaded: int = 0
    statuses: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "failures": self.failures,
            "robots_denied": self.robots_denied,
            "circuit_skips": self.circuit_skips,
            "bytes_downloaded": self.bytes_downloaded,
            "statuses": dict(self.statuses),
        }


class HttpFetcher:
    """Polite async GET client. Use as an async context manager."""

    def __init__(
        self,
        settings: Settings,
        *,
        adapter: str = "unknown",
        snapshot_store: SnapshotStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.stats = FetchStats()
        # Jitter only needs to spread retries out, not resist prediction.
        self._rng = rng or random.Random()  # noqa: S311
        self._policy = RetryPolicy(
            max_retries=settings.max_retries,
            base=settings.backoff_base,
            cap=settings.backoff_cap,
        )
        self.limiter = DomainLimiter(
            concurrency=settings.per_domain_concurrency,
            rate=settings.per_domain_rate,
        )
        self.breaker = CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            reset_seconds=settings.circuit_reset_seconds,
        )
        self.snapshot_store: SnapshotStore = snapshot_store or NullSnapshotStore()
        self._client = httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(settings.request_timeout, connect=settings.connect_timeout),
            limits=httpx.Limits(
                max_connections=max(10, settings.global_concurrency * 2),
                max_keepalive_connections=max(5, settings.global_concurrency),
            ),
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "en",
                "Accept-Encoding": "gzip, deflate",
            },
            transport=transport,
        )
        self.robots = RobotsTxt(
            settings.user_agent,
            enabled=settings.respect_robots,
            default_delay=settings.crawl_delay_fallback,
            fetch=self._fetch_robots_body,
        )

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- robots -------------------------------------------------------------

    async def _fetch_robots_body(self, url: str) -> str | None:
        """Fetch ``robots.txt`` outside the normal politeness path.

        Bypasses the breaker and the limiter deliberately: robots.txt is the
        input to those policies, so routing it through them would be circular.
        Returns ``None`` for 4xx (no restrictions published).
        """
        try:
            response = await self._client.get(url, timeout=httpx.Timeout(10.0))
        except httpx.HTTPError as exc:
            _log.warning("robots.transport_error", url=url, error=str(exc))
            raise
        if response.status_code == 404 or response.status_code == 410:
            return None
        if response.status_code >= 500:
            _log.warning("robots.server_error", url=url, status=response.status_code)
            return "User-agent: *\nDisallow: /\n"
        if response.status_code >= 400:
            return None
        return response.text

    # -- fetching -----------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        adapter: str | None = None,
        kind: str = "html",
        headers: dict[str, str] | None = None,
        check_robots: bool = True,
    ) -> FetchedPage:
        """GET ``url`` with politeness, retries and archiving applied.

        ``kind`` is passed through to the archived page so a later re-parse can
        hand it back to the adapter with the same context.
        """
        label = adapter or self.adapter
        host = host_of(url)

        if check_robots and self.settings.respect_robots:
            verdict = await self.robots.check(url)
            if not verdict.allowed:
                self.stats.robots_denied += 1
                _log.info("robots.denied", url=url, reason=verdict.reason)
                raise RobotsDenied(f"robots.txt disallows {url}")
            if verdict.crawl_delay:
                self.limiter.configure(host, rate=1.0 / max(verdict.crawl_delay, 0.01))

        if not self.breaker.allow(host):
            self.stats.circuit_skips += 1
            raise CircuitOpen(f"circuit open for {host}")

        started = time.perf_counter()
        last_error: str = "unknown"

        for attempt in range(self._policy.max_retries + 1):
            await self.limiter.acquire(url, adapter=label)
            self.stats.requests += 1
            try:
                response = await self._client.get(url, headers=headers)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if (
                    not self._policy.should_retry_exception(exc)
                    or attempt == self._policy.max_retries
                ):
                    REQUESTS.labels(adapter=label, outcome="error").inc()
                    self.stats.failures += 1
                    self.breaker.record_failure(host)
                    raise FetchError(f"GET {url} failed: {last_error}") from exc
                await self._sleep_backoff(attempt, label, reason=type(exc).__name__)
                continue

            status = response.status_code
            self.stats.statuses[str(status)] += 1
            self.stats.bytes_downloaded += len(response.content)

            if status >= 500 or self._policy.should_retry_status(status):
                last_error = f"HTTP {status}"
                if attempt == self._policy.max_retries:
                    REQUESTS.labels(adapter=label, outcome="failed").inc()
                    self.stats.failures += 1
                    self.breaker.record_failure(host)
                    raise FetchError(f"GET {url} failed after retries: {last_error}")
                delay = retry_after_seconds(dict(response.headers))
                await self._sleep_backoff(
                    attempt,
                    label,
                    reason=str(status),
                    server_delay=delay,
                )
                continue

            if status >= 400:
                # 4xx is our mistake, not the host's: do not trip the breaker.
                REQUESTS.labels(adapter=label, outcome="client_error").inc()
                self.stats.failures += 1
                raise FetchError(f"GET {url} returned HTTP {status}")

            elapsed = time.perf_counter() - started
            self.breaker.record_success(host)
            REQUESTS.labels(adapter=label, outcome="ok").inc()
            PAGES_FETCHED.labels(adapter=label).inc()
            REQUEST_LATENCY.labels(adapter=label).observe(elapsed)

            page = FetchedPage.build(
                url=str(response.url),
                status_code=status,
                text=response.text,
                headers=dict(response.headers),
                elapsed=elapsed,
                kind=kind,
            )
            await self._archive(page, label=label)
            return page

        # Unreachable: the loop either returns or raises.
        raise FetchError(f"GET {url} exhausted retries: {last_error}")

    async def _sleep_backoff(
        self,
        attempt: int,
        label: str,
        *,
        reason: str,
        server_delay: float | None = None,
    ) -> None:
        """Wait before the next attempt: server instruction wins over our own."""
        self.stats.retries += 1
        RETRIES.labels(adapter=label, reason=reason).inc()
        delay = (
            server_delay
            if server_delay is not None
            else self._policy.backoff(attempt, rng=self._rng)
        )
        if delay > 0:
            await asyncio.sleep(delay)

    async def _archive(self, page: FetchedPage, *, label: str) -> None:
        if not self.settings.store_snapshots:
            return
        if len(page.text.encode("utf-8")) > self.settings.snapshot_max_bytes:
            _log.debug("snapshot.skipped_too_large", url=page.url)
            return
        try:
            await self.snapshot_store.save(page, adapter=label, status_code=page.status_code)
        except Exception as exc:
            _log.warning("snapshot.save_failed", url=page.url, error=str(exc))

    # -- introspection ------------------------------------------------------

    @property
    def breaker_states(self) -> dict[str, str]:
        return self.breaker.snapshot()

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            **self.stats.as_dict(),
            "hosts": self.limiter.hosts,
            "breakers": self.breaker.snapshot(),
        }
