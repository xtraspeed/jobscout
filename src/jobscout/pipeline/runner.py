"""Run orchestration: database lifecycle around a crawl, plus offline re-parsing.

``run_crawl`` is the one place that knows how a crawl and the database fit
together. Everything below it (fetcher, adapters, repositories) is usable on its
own, which is what makes the pieces independently testable.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from jobscout.adapters.base import Adapter
from jobscout.config import Settings
from jobscout.fetch.client import HttpFetcher
from jobscout.models import Emitted, FetchedPage, Follow, JobItem
from jobscout.observability import get_logger
from jobscout.pipeline.crawler import Crawler, CrawlResult
from jobscout.store.db import (
    SessionMaker,
    create_engine,
    create_sessionmaker,
    session_dialect,
    session_scope,
)
from jobscout.store.repositories import (
    ErrorRepository,
    JobRepository,
    RunRepository,
    WriteStats,
)
from jobscout.store.snapshots import DbSnapshotStore

_log = get_logger(__name__)

#: Rows per write transaction. Large enough to amortise round trips, small
#: enough that a failure never loses a whole run's work.
WRITE_BATCH = 50


@dataclass(slots=True)
class ReParseReport:
    """Counters for an offline re-parse."""

    total_urls: int = 0
    parsed: int = 0
    skipped: int = 0
    marked_inactive: int = 0
    duration_seconds: float = 0.0
    writes: WriteStats = field(default_factory=WriteStats)
    buffer: list[JobItem] = field(default_factory=list)

    def counters(self) -> dict[str, Any]:
        return {
            "pages_fetched": self.parsed,
            "items_seen": self.parsed,
            "items_new": self.writes.new,
            "items_changed": self.writes.changed,
            "items_unchanged": self.writes.unchanged,
        }


async def _dialect(session: AsyncSession) -> str:
    """Dialect name of whatever the session is bound to."""
    return session_dialect(session)


async def run_crawl(
    settings: Settings,
    adapter: Adapter,
    *,
    max_pages: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    engine: AsyncEngine | None = None,
    sessionmaker: SessionMaker | None = None,
) -> CrawlResult:
    """Execute one crawl run end to end, recording it in ``crawl_runs``."""
    owns_engine = False
    if sessionmaker is None:
        engine = engine or create_engine(settings)
        sessionmaker = create_sessionmaker(engine)
        owns_engine = True

    try:
        async with session_scope(sessionmaker) as session:
            dialect = await _dialect(session)
            runs = RunRepository(session)
            run = await runs.start(
                adapter.name, detail={"max_pages": max_pages or settings.max_pages}
            )
            _log.info("crawl.run_started", run_id=run.id, adapter=adapter.name)

            jobs = JobRepository(session, dialect=dialect)
            errors = ErrorRepository(session)
            snapshots = DbSnapshotStore(session, dialect=dialect)

            async with HttpFetcher(
                settings,
                adapter=adapter.name,
                snapshot_store=snapshots,
                transport=transport,
            ) as fetcher:
                crawler = Crawler(
                    settings,
                    adapter,
                    fetcher,
                    jobs=jobs,
                    errors=errors,
                    run_id=run.id,
                    max_pages=max_pages,
                )
                result = await crawler.run()

            await runs.finish(
                run,
                status=result.status,
                counters=result.counters(),
                duration=result.duration_seconds,
            )
            run.detail = result.detail()
            result.run_id = run.id
            return result
    finally:
        if owns_engine and engine is not None:
            await engine.dispose()


async def reparse_snapshots(
    settings: Settings,
    adapter: Adapter,
    *,
    limit: int = 1000,
    engine: AsyncEngine | None = None,
    sessionmaker: SessionMaker | None = None,
) -> ReParseReport:
    """Re-run an adapter's parser over archived bodies, with no network access.

    This is the payoff for storing raw responses: when a board changes its
    markup, fixing the selectors and re-parsing the archive costs seconds and
    zero requests to the target site.
    """
    owns_engine = False
    if sessionmaker is None:
        engine = engine or create_engine(settings)
        sessionmaker = create_sessionmaker(engine)
        owns_engine = True

    report = ReParseReport()
    try:
        async with session_scope(sessionmaker) as session:
            dialect = await _dialect(session)
            snapshots = DbSnapshotStore(session, dialect=dialect)
            jobs = JobRepository(session, dialect=dialect)
            runs = RunRepository(session)
            run = await runs.start(adapter.name, detail={"mode": "reparse"})

            urls = await snapshots.urls(adapter=adapter.name, limit=limit)
            report.total_urls = len(urls)
            if not urls:
                _log.warning("reparse.no_snapshots", adapter=adapter.name)
                await runs.finish(run, status="succeeded", counters=report.counters(), duration=0.0)
                return report

            started = time.perf_counter()
            seen: set[str] = set()
            for url in urls:
                page = await snapshots.load(url)
                if page is None:
                    report.skipped += 1
                    continue
                report.parsed += 1
                item = _reparse_page(adapter, page)
                if item is not None:
                    report.buffer.append(item)
                    seen.add(item.external_id)
                if len(report.buffer) >= WRITE_BATCH:
                    await _write(jobs, report, run.id)
            await _write(jobs, report, run.id)

            report.marked_inactive = await jobs.mark_stale(
                adapter.name, keep_ids=seen, run_id=run.id
            )
            report.duration_seconds = round(time.perf_counter() - started, 3)

            await runs.finish(
                run,
                status="succeeded",
                counters=report.counters(),
                duration=report.duration_seconds,
            )
            run.detail = {"mode": "reparse", "skipped": report.skipped, "limit": limit}
            _log.info("reparse.finished", **{"adapter": adapter.name, **report.counters()})
            return report
    finally:
        if owns_engine and engine is not None:
            await engine.dispose()


def _reparse_page(adapter: Adapter, page: FetchedPage) -> JobItem | None:
    """Ask an adapter to parse one archived page.

    The archived ``kind`` is replayed as the request kind, so a detail page is
    parsed as a detail page rather than being guessed at. Anything the adapter
    cannot interpret simply yields no item.
    """
    follow = Follow(url=page.url, kind=page.kind, meta={"stage": "reparse", "reparse": True})
    try:
        outputs: Sequence[Follow | Emitted] = adapter.parse(page, follow)
    except Exception as exc:
        _log.warning("reparse.page_failed", url=page.url, error=str(exc))
        return None
    for output in outputs:
        if isinstance(output, Emitted):
            return output.item
    return None


async def _write(jobs: JobRepository, report: ReParseReport, run_id: int) -> None:
    if not report.buffer:
        return
    stats = await jobs.upsert_many(report.buffer, run_id=run_id)
    report.writes.merge(stats)
    report.buffer.clear()
