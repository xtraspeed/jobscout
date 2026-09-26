"""End-to-end crawl against recorded fixtures.

These run the *real* :class:`HttpFetcher` (rate limiter, robots, retries,
archiving, metrics) with only the socket replaced by a fixture transport, so
the whole pipeline is exercised offline.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.adapters.fixture import FixtureAdapter
from jobscout.config import Settings
from jobscout.pipeline import run_crawl
from jobscout.store.models import CrawlRun, JobItemRow, RawPage
from jobscout.store.repositories import JobRepository, RunRepository


async def count_items(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(JobItemRow))
    return int(result.scalar_one())


async def fetch_all(session: AsyncSession) -> list[JobItemRow]:
    stmt = select(JobItemRow).order_by(JobItemRow.external_id)
    return list((await session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_crawl_collects_every_listing(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    result = await run_crawl(
        settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker
    )

    assert result.status == "succeeded"
    # 2 index pages + 4 recorded detail pages (1003 has no recorded page).
    assert result.pages_fetched == 6
    assert result.items_seen == 4
    assert result.writes.new == 4
    assert await count_items(session) == 4


@pytest.mark.asyncio
async def test_crawled_items_are_fully_normalised(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    await run_crawl(settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker)

    rows = await fetch_all(session)
    assert len(rows) == 4
    # The adapter name must equal the item source: the pipeline scopes staleness
    # marking and metric labels by it.
    assert {row.source for row in rows} == {board_adapter.name}

    backend = next(row for row in rows if row.title == "Senior Backend Engineer")
    assert backend.company == "Northwind Analytics"
    assert backend.location == "Berlin, Germany (Hybrid)"
    assert backend.salary_min == 85_000.0
    assert backend.salary_max == 105_000.0
    assert backend.salary_currency == "EUR"
    assert backend.employment_type == "full-time"
    assert "Kafka" in backend.description
    assert "jobs@northwind.example" not in backend.description, "PII must be scrubbed"
    assert backend.url == "https://demo-board.example/jobs/1001"
    assert backend.posted_at is not None
    assert len(backend.external_id) == 24, "external id is a stable sha1 prefix of the URL"


@pytest.mark.asyncio
async def test_pagination_follows_the_next_link(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    await run_crawl(settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker)

    titles = {row.title for row in await fetch_all(session)}
    assert "Platform Engineer" in titles, "page 2 must be crawled"
    assert "Technical Writer" in titles


@pytest.mark.asyncio
async def test_recrawl_is_idempotent(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    """The second run must report every item as unchanged and add no rows."""
    first = await run_crawl(
        settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker
    )
    assert first.writes.new == 4

    second = await run_crawl(
        settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker
    )

    assert second.writes.new == 0
    assert second.writes.changed == 0
    assert second.writes.unchanged == 4
    assert await count_items(session) == 4


@pytest.mark.asyncio
async def test_changed_page_produces_exactly_one_changed_row(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    await run_crawl(settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker)

    # Simulate the board retitling a role. The override is in-memory, so the
    # recorded files on disk stay untouched for every other test.
    detail_url = "https://demo-board.example/jobs/1001"
    original = board_adapter.bundle.read(board_adapter.bundle.pages[detail_url])
    board_adapter.bundle.override(
        detail_url, original.replace("Senior Backend Engineer", "Backend Engineer (Payments)")
    )

    second = await run_crawl(
        settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker
    )

    assert second.writes.changed == 1
    assert second.writes.new == 0
    rows = await fetch_all(session)
    assert len(rows) == 4
    assert any(row.title == "Backend Engineer (Payments)" for row in rows)


@pytest.mark.asyncio
async def test_missing_recorded_page_is_recorded_as_a_failure(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    """Job 1003 appears on the index but has no detail page: 404, not a crash."""
    result = await run_crawl(
        settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker
    )

    assert result.status == "succeeded", "a single 404 must not fail the run"
    assert result.failures >= 1
    assert await count_items(session) == 4


@pytest.mark.asyncio
async def test_robots_txt_is_fetched_and_honoured(
    settings: Settings,
    board_adapter: FixtureAdapter,
    sessionmaker: Any,
) -> None:
    from jobscout.fetch.transport import RESTRICTIVE_ROBOTS, fixture_transport

    def resolve(url: str) -> tuple[int, str, str] | None:
        page = board_adapter.page_for(url)
        if page.status_code == 404:
            return None
        return page.status_code, page.text, "text/html"

    transport = fixture_transport(resolve, robots=RESTRICTIVE_ROBOTS)
    result = await run_crawl(
        settings, board_adapter, transport=transport, sessionmaker=sessionmaker
    )

    assert result.status == "succeeded"
    assert result.robots_denied > 0, "every URL must be refused by robots"
    assert result.pages_fetched == 0


@pytest.mark.asyncio
async def test_raw_pages_are_archived(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    await run_crawl(settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker)

    archived = (await session.execute(select(func.count()).select_from(RawPage))).scalar_one()
    assert int(archived) == 6, "every fetched page is kept for offline re-parsing"


@pytest.mark.asyncio
async def test_run_row_records_counters(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    result = await run_crawl(
        settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker
    )

    run = await session.get(CrawlRun, result.run_id)
    assert run is not None
    assert run.status == "succeeded"
    assert run.adapter == board_adapter.name
    assert run.items_new == 4
    assert run.pages_fetched == 6
    assert run.finished_at is not None
    assert run.duration_seconds > 0
    assert await RunRepository(session).list_recent()


@pytest.mark.asyncio
async def test_page_budget_is_respected(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
) -> None:
    result = await run_crawl(
        settings, board_adapter, max_pages=2, transport=board_transport, sessionmaker=sessionmaker
    )

    assert result.pages_fetched <= 2
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_missing_listing_is_marked_inactive_on_recrawl(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    await run_crawl(settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker)
    assert await JobRepository(session, dialect="sqlite").count() == 4

    # Drop one detail page from the bundle: the run no longer sees that listing.
    del board_adapter.bundle.pages["https://demo-board.example/jobs/1004"]
    result = await run_crawl(
        settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker
    )

    assert result.marked_inactive == 1
    rows = await fetch_all(session)
    inactive = [row for row in rows if not row.is_active]
    assert len(inactive) == 1
    assert inactive[0].title == "Platform Engineer"
    # Nothing was deleted: the listing is still queryable, just not active.
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_crawl_survives_a_broken_adapter(
    settings: Settings,
    board_transport: Any,
    sessionmaker: Any,
    fixture_dir: Any,
) -> None:
    """An adapter that raises on parse must not take the run down."""
    from jobscout.adapters.fixture import FixtureAdapter as _FixtureAdapter
    from jobscout.adapters.fixture import FixtureBundle

    class ExplodingAdapter(_FixtureAdapter):
        def parse(self, page: Any, follow: Any) -> Any:
            raise RuntimeError("parser exploded")

    adapter = ExplodingAdapter(FixtureBundle(fixture_dir))
    result = await run_crawl(
        settings, adapter, transport=board_transport, sessionmaker=sessionmaker
    )

    assert result.status == "succeeded"
    assert result.items_seen == 0
    assert result.failures > 0


@pytest.mark.asyncio
async def test_item_from_hn_comment_becomes_a_listing(
    settings: Settings,
    hn_json_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    """The HN adapter's three-stage flow over recorded API payloads."""
    from jobscout.adapters.hnhiring import HnHiringAdapter

    adapter = HnHiringAdapter(threads=1, max_comments_per_thread=10)
    result = await run_crawl(
        settings, adapter, transport=hn_json_transport, sessionmaker=sessionmaker
    )

    assert result.status == "succeeded"
    assert result.pages_fetched == 5, "search + thread + 3 comments"

    rows = await fetch_all(session)
    assert {row.company for row in rows} == {"Northwind Analytics", "Contoso Labs"}

    northwind = next(row for row in rows if row.company == "Northwind Analytics")
    assert northwind.title == "Senior Backend Engineer"
    assert northwind.location == "Berlin, Germany"
    assert northwind.salary_min == 85_000.0
    assert northwind.salary_max == 105_000.0
    assert northwind.employment_type == "full-time"
    assert northwind.remote is True
    assert northwind.url == "https://careers.northwind.example/jobs/senior-backend"
    assert northwind.raw["thread_id"] == 40000001


@pytest.mark.asyncio
async def test_hn_non_posting_comment_is_ignored(
    settings: Settings,
    hn_json_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    from jobscout.adapters.hnhiring import HnHiringAdapter

    await run_crawl(
        settings, HnHiringAdapter(threads=1), transport=hn_json_transport, sessionmaker=sessionmaker
    )

    titles = {row.title for row in await fetch_all(session)}
    assert not any("not hiring" in title.lower() for title in titles)
    assert len(titles) == 2


@pytest.mark.asyncio
async def test_hn_search_ignores_want_to_be_hired_threads(
    settings: Settings,
    hn_json_transport: Any,
    sessionmaker: Any,
) -> None:
    """Only "who is hiring" threads yield listings; resumes are not jobs."""
    from jobscout.adapters.hnhiring import HnHiringAdapter

    result = await run_crawl(
        settings, HnHiringAdapter(threads=5), transport=hn_json_transport, sessionmaker=sessionmaker
    )

    assert result.pages_fetched == 5, "one thread, not three stories"
