"""Offline re-parsing: the payoff for archiving raw responses.

A site redesign should cost a selector fix and a re-parse, not a fresh crawl.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.adapters.fixture import FixtureAdapter, FixtureBundle
from jobscout.config import Settings
from jobscout.parse.board import JobBoardParser
from jobscout.pipeline import reparse_snapshots, run_crawl
from jobscout.store.models import CrawlRun, JobItemRow, RawPage
from jobscout.store.repositories import JobRepository


@pytest.fixture
async def crawled(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
) -> dict[str, Any]:
    """A first crawl, so there is an archive to re-parse."""
    result = await run_crawl(
        settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker
    )
    assert result.status == "succeeded"
    return {"adapter": board_adapter, "bundle": board_adapter.bundle}


@pytest.mark.asyncio
async def test_reparse_without_an_archive_is_a_noop(
    settings: Settings,
    board_adapter: FixtureAdapter,
    sessionmaker: Any,
) -> None:
    report = await reparse_snapshots(settings, board_adapter, sessionmaker=sessionmaker)

    assert report.total_urls == 0
    assert report.parsed == 0
    assert report.writes.total == 0


@pytest.mark.asyncio
async def test_reparse_reproduces_the_same_rows(
    settings: Settings,
    crawled: dict[str, Any],
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    adapter = FixtureAdapter(crawled["bundle"])
    before = len((await session.execute(select(JobItemRow))).scalars().all())

    report = await reparse_snapshots(settings, adapter, sessionmaker=sessionmaker)

    after = len((await session.execute(select(JobItemRow))).scalars().all())
    assert report.parsed == 6
    assert report.writes.new == 0, "nothing new: these listings are already stored"
    assert report.writes.changed == 0, "and nothing changed: parsing is deterministic"
    assert report.writes.unchanged == before
    assert after == before, "re-parsing must not duplicate rows"


@pytest.mark.asyncio
async def test_reparse_picks_up_a_selector_fix(
    settings: Settings,
    crawled: dict[str, Any],
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    """The headline use case: fix a selector, re-parse the archive, ship the fix.

    The archive still holds the original bytes; only the parser changes. That is
    what turns a site redesign into a configuration change rather than a fresh
    crawl.
    """
    bundle: FixtureBundle = crawled["bundle"]
    adapter = FixtureAdapter(bundle)

    # "Fix" the title selector to read the document <title> instead of the <h1>.
    config = bundle.config.model_copy(
        update={
            "detail": bundle.config.detail.model_copy(
                update={"fields": {**bundle.config.detail.fields, "title": "title::text"}}
            )
        }
    )
    adapter.parser = JobBoardParser(config)

    report = await reparse_snapshots(settings, adapter, sessionmaker=sessionmaker)

    assert report.parsed == 6
    assert report.writes.changed == 4, "all four detail pages have a <title> element"
    assert report.writes.new == 0, "and no duplicates were created"
    rows = (await session.execute(select(JobItemRow))).scalars().all()
    assert len(rows) == 4, "in place, not re-inserted"

    retitled = next(row for row in rows if "Northwind Analytics" in row.title)
    assert retitled.title == "Senior Backend Engineer — Northwind Analytics", (
        "the new selector read the document <title>, entity already decoded"
    )


@pytest.mark.asyncio
async def test_reparse_records_a_run(
    settings: Settings,
    crawled: dict[str, Any],
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    adapter = FixtureAdapter(crawled["bundle"])
    await reparse_snapshots(settings, adapter, sessionmaker=sessionmaker)

    stmt = select(CrawlRun).order_by(CrawlRun.id.desc()).limit(1)
    run = (await session.execute(stmt)).scalars().first()

    assert run is not None
    assert run.status == "succeeded"
    assert run.detail["mode"] == "reparse"


@pytest.mark.asyncio
async def test_reparse_respects_the_url_limit(
    settings: Settings,
    crawled: dict[str, Any],
    sessionmaker: Any,
) -> None:
    adapter = FixtureAdapter(crawled["bundle"])
    report = await reparse_snapshots(settings, adapter, limit=2, sessionmaker=sessionmaker)

    assert report.total_urls == 2
    assert report.parsed == 2


@pytest.mark.asyncio
async def test_reparse_never_touches_the_network(
    settings: Settings,
    crawled: dict[str, Any],
    sessionmaker: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: re-parsing must not contact the target site."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("reparse attempted to construct an HTTP client")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", explode)

    adapter = FixtureAdapter(crawled["bundle"])
    report = await reparse_snapshots(settings, adapter, sessionmaker=sessionmaker)

    assert report.parsed == 6


@pytest.mark.asyncio
async def test_reparse_tolerates_garbage_bodies(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
) -> None:
    """A truncated or restructured page must yield nothing, not an exception."""
    await run_crawl(settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker)
    for page in board_adapter.bundle.pages.values():
        board_adapter.bundle.override(page.url, "<html><body><p>redesigned")

    report = await reparse_snapshots(settings, board_adapter, sessionmaker=sessionmaker)

    assert report.parsed == 6
    assert report.writes.new == 0


async def test_reparse_marks_absent_listings_inactive(
    settings: Settings,
    board_adapter: FixtureAdapter,
    board_transport: Any,
    sessionmaker: Any,
    session: AsyncSession,
) -> None:
    """A listing whose page left the archive stops being advertised."""
    await run_crawl(settings, board_adapter, transport=board_transport, sessionmaker=sessionmaker)
    assert await JobRepository(session, dialect="sqlite").count() == 4

    # Purge one archived page, as archive pruning would.
    await session.execute(
        delete(RawPage).where(RawPage.url == "https://demo-board.example/jobs/1004")
    )
    await session.commit()

    report = await reparse_snapshots(settings, board_adapter, sessionmaker=sessionmaker)

    assert report.parsed == 5
    assert report.marked_inactive == 1

    rows = (await session.execute(select(JobItemRow))).scalars().all()
    inactive = [row for row in rows if not row.is_active]
    assert [row.title for row in inactive] == ["Platform Engineer"]
    assert len(rows) == 4, "rows are flagged, never deleted"


def test_fixture_bundle_validates_its_inputs(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FixtureBundle(tmp_path)


def test_fixture_bundle_reports_missing_page(board_adapter: FixtureAdapter) -> None:
    page = board_adapter.page_for("https://demo-board.example/jobs/does-not-exist")

    assert page.status_code == 404
    assert "404" in page.text


def test_fixture_bundle_length(board_adapter: FixtureAdapter) -> None:
    assert len(board_adapter.bundle) == 6
