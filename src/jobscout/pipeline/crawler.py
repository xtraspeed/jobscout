"""Crawl orchestration.

The loop is deliberately boring and observable:

    frontier -> bounded-concurrency fan-out -> fetch -> parse -> upsert

Two different limits are at work, and the distinction matters:

* **global concurrency** bounds how many pages are in flight, which protects
  *our* memory and connection pool;
* **the per-domain limiter** bounds the request rate, which protects the *site*.

A single broken page never stops a run: fetch errors are recorded, counted and
skipped, and the loop keeps draining the frontier until it is empty or the page
budget is spent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jobscout.adapters.base import Adapter
from jobscout.config import Settings
from jobscout.errors import JobScoutError
from jobscout.fetch.client import HttpFetcher
from jobscout.models import Emitted, Follow, JobItem
from jobscout.observability import get_logger
from jobscout.observability.metrics import (
    CRAWL_DEPTH,
    CRAWL_DURATION,
    ITEMS_SEEN,
    ITEMS_WRITTEN,
)
from jobscout.pipeline.frontier import Frontier, same_site
from jobscout.store.repositories import ErrorRepository, JobRepository, WriteStats

_log = get_logger(__name__)

#: Items are written in batches so a 50k-item run does not build one huge
#: transaction, while a small run still pays only one round trip.
UPSERT_BATCH = 50


@dataclass(slots=True)
class CrawlResult:
    """Everything worth knowing about a finished run."""

    adapter: str
    status: str = "running"
    run_id: int | None = None
    pages_fetched: int = 0
    items_seen: int = 0
    writes: WriteStats = field(default_factory=WriteStats)
    failures: int = 0
    requests: int = 0
    retries: int = 0
    robots_denied: int = 0
    circuit_skips: int = 0
    bytes_downloaded: int = 0
    max_depth_reached: int = 0
    duplicates_skipped: int = 0
    too_deep_skipped: int = 0
    marked_inactive: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    breakers: dict[str, str] = field(default_factory=dict)

    def counters(self) -> dict[str, Any]:
        """Flat mapping written onto the ``crawl_runs`` row."""
        return {
            "pages_fetched": self.pages_fetched,
            "requests_made": self.requests,
            "retries": self.retries,
            "failures": self.failures,
            "items_seen": self.items_seen,
            "items_new": self.writes.new,
            "items_changed": self.writes.changed,
            "items_unchanged": self.writes.unchanged,
            "max_depth_reached": self.max_depth_reached,
        }

    def detail(self) -> dict[str, Any]:
        return {
            "robots_denied": self.robots_denied,
            "circuit_skips": self.circuit_skips,
            "bytes_downloaded": self.bytes_downloaded,
            "duplicates_skipped": self.duplicates_skipped,
            "too_deep_skipped": self.too_deep_skipped,
            "marked_inactive": self.marked_inactive,
            "breakers": self.breakers,
        }

    def summary(self) -> str:
        return (
            f"{self.adapter}: {self.status} in {self.duration_seconds:.1f}s | "
            f"pages={self.pages_fetched} seen={self.items_seen} "
            f"new={self.writes.new} changed={self.writes.changed} "
            f"unchanged={self.writes.unchanged} failures={self.failures}"
        )


@dataclass(slots=True)
class _Outcome:
    """Result of processing one page."""

    fetched: bool = False
    items: list[JobItem] = field(default_factory=list)
    follows: list[Follow] = field(default_factory=list)
    error: str | None = None


class Crawler:
    """Drives one adapter to completion against one fetcher and one repository."""

    def __init__(
        self,
        settings: Settings,
        adapter: Adapter,
        fetcher: HttpFetcher,
        *,
        jobs: JobRepository,
        errors: ErrorRepository | None = None,
        run_id: int | None = None,
        max_pages: int | None = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.fetcher = fetcher
        self.jobs = jobs
        self.errors = errors
        self.run_id = run_id
        self.max_pages = max_pages if max_pages is not None else settings.max_pages
        self.result = CrawlResult(adapter=adapter.name)

    async def run(self) -> CrawlResult:
        """Execute the crawl and return its :class:`CrawlResult`."""
        started = time.perf_counter()
        self.result.status = "running"
        frontier = Frontier(max_depth=self.settings.max_depth, max_items=self.max_pages * 10)

        try:
            seeds = await self.adapter.start()
            frontier.push_all(seeds, depth=0)
            _log.info(
                "crawl.started",
                adapter=self.adapter.name,
                seeds=len(seeds),
                max_pages=self.max_pages,
                max_depth=self.settings.max_depth,
            )
            await self._drain(frontier)
            self.result.status = "succeeded"
        except JobScoutError as exc:
            self.result.status = "failed"
            self.result.errors.append(str(exc))
            _log.error("crawl.failed", adapter=self.adapter.name, error=str(exc))
        except Exception as exc:
            self.result.status = "failed"
            self.result.errors.append(f"{type(exc).__name__}: {exc}")
            _log.exception("crawl.crashed", adapter=self.adapter.name)
        finally:
            self.result.duration_seconds = round(time.perf_counter() - started, 3)
            self._absorb_fetcher_stats()
            self.result.duplicates_skipped = frontier.stats.duplicates
            self.result.too_deep_skipped = frontier.stats.too_deep
            self.result.max_depth_reached = frontier.stats.max_depth_reached
            self.result.breakers = self.fetcher.breaker_states
            CRAWL_DURATION.labels(adapter=self.adapter.name).observe(self.result.duration_seconds)
            _log.info("crawl.finished", summary=self.result.summary())

        return self.result

    # -- internals ----------------------------------------------------------

    async def _drain(self, frontier: Frontier) -> None:
        """Pop, fan out with bounded concurrency, and write results in batches."""
        seen_ids: set[str] = set()
        buffer: list[JobItem] = []

        while len(frontier) and self.result.pages_fetched < self.max_pages:
            batch = self._take_batch(frontier)
            if not batch:
                break

            CRAWL_DEPTH.labels(adapter=self.adapter.name).set(len(frontier))
            outcomes = await asyncio.gather(
                *(self._process(follow, depth) for follow, depth in batch)
            )

            for (_, depth), outcome in zip(batch, outcomes, strict=True):
                frontier.push_all(outcome.follows, depth=depth + 1)

            for outcome in outcomes:
                if outcome.error:
                    await self._record_error(outcome.error)
                    continue
                if not outcome.fetched:
                    continue
                self.result.pages_fetched += 1
                for item in outcome.items:
                    seen_ids.add(item.external_id)
                buffer.extend(outcome.items)
                if len(buffer) >= UPSERT_BATCH:
                    await self._flush(buffer, seen_ids)
                    buffer = []

        if buffer:
            await self._flush(buffer, seen_ids)

        if seen_ids:
            self.result.marked_inactive = await self.jobs.mark_stale(
                self.adapter.name, self.run_id or 0, keep_ids=seen_ids
            )
            if self.result.marked_inactive:
                _log.info(
                    "crawl.marked_inactive",
                    adapter=self.adapter.name,
                    count=self.result.marked_inactive,
                )

    def _take_batch(self, frontier: Frontier) -> list[tuple[Follow, int]]:
        """Up to ``global_concurrency`` items, honouring the page budget."""
        batch: list[tuple[Follow, int]] = []
        while len(batch) < self.settings.global_concurrency:
            if self.result.pages_fetched + len(batch) >= self.max_pages:
                break
            popped = frontier.pop()
            if popped is None:
                break
            batch.append(popped)
        return batch

    async def _process(self, follow: Follow, depth: int) -> _Outcome:
        outcome = _Outcome()
        if depth > 0 and not same_site(follow.url, self._anchor(follow)):
            _log.debug("crawl.offsite_skipped", url=follow.url)
            return outcome
        try:
            page = await self.fetcher.fetch(follow.url, adapter=self.adapter.name, kind=follow.kind)
        except JobScoutError as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            return outcome
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            return outcome

        outcome.fetched = True
        try:
            outputs: Sequence[Follow | Emitted] = self.adapter.parse(page, follow)
        except Exception as exc:
            _log.warning("crawl.parse_failed", url=page.url, error=str(exc))
            outcome.error = f"parse: {type(exc).__name__}: {exc}"
            return outcome

        for output in outputs:
            if isinstance(output, Emitted):
                outcome.items.append(output.item)
            elif isinstance(output, Follow):
                outcome.follows.append(output)
        return outcome

    def _anchor(self, follow: Follow) -> str:
        """The site this follow is allowed to belong to."""
        anchor = follow.meta.get("base_url")
        if isinstance(anchor, str) and anchor:
            return anchor
        return follow.url

    async def _flush(self, buffer: list[JobItem], seen_ids: set[str]) -> None:
        stats = await self.jobs.upsert_many(buffer, run_id=self.run_id)
        self.result.writes.merge(stats)
        self.result.items_seen += len(buffer)
        ITEMS_SEEN.labels(adapter=self.adapter.name).inc(len(buffer))
        for outcome, count in (
            ("new", stats.new),
            ("changed", stats.changed),
            ("unchanged", stats.unchanged),
        ):
            if count:
                ITEMS_WRITTEN.labels(adapter=self.adapter.name, outcome=outcome).inc(count)
        buffer.clear()

    async def _record_error(self, message: str) -> None:
        self.result.failures += 1
        self.result.errors.append(message)
        if len(self.result.errors) > 200:
            return  # keep the in-memory list bounded; DB keeps everything
        if self.errors is not None:
            try:
                await self.errors.record(self.run_id, message.split(" ", 1)[-1], message)
            except Exception as exc:
                _log.debug("crawl.error_record_failed", error=str(exc))

    def _absorb_fetcher_stats(self) -> None:
        stats = self.fetcher.stats
        self.result.requests = stats.requests
        self.result.retries = stats.retries
        self.result.robots_denied = stats.robots_denied
        self.result.circuit_skips = stats.circuit_skips
        self.result.bytes_downloaded = stats.bytes_downloaded
        self.result.failures = max(self.result.failures, stats.failures)
