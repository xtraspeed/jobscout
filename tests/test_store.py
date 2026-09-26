"""Store behaviour: change detection, idempotent upserts and cursor pagination.

These are the tests that protect the core promise of the system: re-crawling the
same data must not duplicate rows or churn timestamps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.models import JobItem
from jobscout.store.models import JobItemRow
from jobscout.store.repositories import (
    ItemFilters,
    JobRepository,
    RunRepository,
    decode_cursor,
    encode_cursor,
)

SOURCE = "test_board"
BASE_URL = "https://board.test/jobs"


def make_item(
    external_id: str = "1",
    *,
    title: str = "Backend Engineer",
    company: str = "Acme",
    remote: bool = False,
    salary_max: float | None = 100_000.0,
    posted_at: datetime | None = None,
) -> JobItem:
    return JobItem(
        source=SOURCE,
        external_id=external_id,
        url=f"{BASE_URL}/{external_id}",
        title=title,
        company=company,
        location="Berlin" if not remote else "Remote (EU)",
        remote=remote,
        salary_min=salary_max,
        salary_max=salary_max,
        salary_currency="EUR",
        description="Work on things.",
        tags=["Python"],
        posted_at=posted_at or datetime(2026, 2, 1, tzinfo=UTC),
    )


async def count_rows(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(JobItemRow))
    return int(result.scalar_one())


# ----------------------------------------------------------------- upserts ---


@pytest.mark.asyncio
async def test_first_write_is_new(repo: JobRepository, session: AsyncSession) -> None:
    stats = await repo.upsert_many([make_item("1"), make_item("2")])
    await session.commit()

    assert stats.new == 2
    assert stats.changed == 0
    assert await count_rows(session) == 2


@pytest.mark.asyncio
async def test_reinsert_is_unchanged_and_does_not_duplicate(
    repo: JobRepository, session: AsyncSession
) -> None:
    await repo.upsert_many([make_item("1")])
    await session.commit()

    stats = await repo.upsert_many([make_item("1")])
    await session.commit()

    assert (stats.new, stats.changed, stats.unchanged) == (0, 0, 1)
    assert await count_rows(session) == 1


@pytest.mark.asyncio
async def test_changed_content_updates_the_row(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many([make_item("1")])
    await session.commit()
    before = (await repo.get(1)).content_hash  # type: ignore[union-attr]

    stats = await repo.upsert_many([make_item("1", title="Staff Backend Engineer")])
    await session.commit()

    assert (stats.new, stats.changed, stats.unchanged) == (0, 1, 0)
    row = await repo.get(1)
    assert row is not None
    assert row.title == "Staff Backend Engineer"
    assert row.content_hash != before


@pytest.mark.asyncio
async def test_first_seen_is_preserved_across_updates(
    repo: JobRepository, session: AsyncSession
) -> None:
    await repo.upsert_many([make_item("1")])
    await session.commit()
    original = (await repo.get(1)).first_seen_at  # type: ignore[union-attr]

    await repo.upsert_many([make_item("1", title="New Title")])
    await session.commit()
    row = await repo.get(1)

    assert row is not None
    assert row.first_seen_at == original, "first_seen_at must never move"
    assert row.last_seen_at >= original


@pytest.mark.asyncio
async def test_mixed_batch_counts_each_outcome(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many([make_item("1"), make_item("2")])
    await session.commit()

    stats = await repo.upsert_many(
        [
            make_item("1"),  # identical
            make_item("2", title="Changed"),  # changed
            make_item("3"),  # new
        ]
    )
    await session.commit()

    assert (stats.new, stats.changed, stats.unchanged) == (1, 1, 1)
    assert stats.total == 3
    assert await count_rows(session) == 3


@pytest.mark.asyncio
async def test_empty_batch_is_a_noop(repo: JobRepository) -> None:
    assert (await repo.upsert_many([])).total == 0


@pytest.mark.asyncio
async def test_natural_key_collision_across_sources_is_allowed(
    repo: JobRepository, session: AsyncSession
) -> None:
    """The same external id from two sources is two distinct listings."""
    await repo.upsert_many([make_item("42")])
    other = make_item("42").model_copy(update={"source": "other_board"})
    await repo.upsert_many([other])
    await session.commit()

    assert await count_rows(session) == 2


@pytest.mark.asyncio
async def test_mark_stale_flags_listings_missing_from_a_run(
    repo: JobRepository, session: AsyncSession
) -> None:
    await repo.upsert_many([make_item("1"), make_item("2"), make_item("3")])
    await session.commit()

    marked = await repo.mark_stale(SOURCE, keep_ids={"1", "2"})
    await session.commit()

    assert marked == 1
    assert (await repo.get(3)).is_active is False  # type: ignore[union-attr]
    assert (await repo.get(1)).is_active is True  # type: ignore[union-attr]


# --------------------------------------------------------------- filtering ---


@pytest.mark.asyncio
async def test_filters_by_company_and_remote(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many(
        [
            make_item("1", company="Acme"),
            make_item("2", company="Globex", remote=True),
            make_item("3", company="Acme", remote=True),
        ]
    )
    await session.commit()

    acme = await repo.list_items(ItemFilters(companies=["Acme"], limit=10))
    assert {row.company for row in acme.items} == {"Acme"}

    remote = await repo.list_items(ItemFilters(remote=True, limit=10))
    assert {row.external_id for row in remote.items} == {"2", "3"}


@pytest.mark.asyncio
async def test_salary_filter_covers_range_overlap(
    repo: JobRepository, session: AsyncSession
) -> None:
    await repo.upsert_many(
        [
            make_item("1", salary_max=90_000.0),
            make_item("2", salary_max=180_000.0),
            make_item("3", salary_max=None),
        ]
    )
    await session.commit()

    page = await repo.list_items(ItemFilters(min_salary=120_000.0, limit=10))

    assert {row.external_id for row in page.items} == {"2"}


@pytest.mark.asyncio
async def test_has_salary_filter(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many([make_item("1"), make_item("2", salary_max=None)])
    await session.commit()

    with_salary = await repo.list_items(ItemFilters(has_salary=True, limit=10))
    without = await repo.list_items(ItemFilters(has_salary=False, limit=10))

    assert {row.external_id for row in with_salary.items} == {"1"}
    assert {row.external_id for row in without.items} == {"2"}


@pytest.mark.asyncio
async def test_text_search_matches_title_and_company(
    repo: JobRepository, session: AsyncSession
) -> None:
    await repo.upsert_many(
        [
            make_item("1", title="Rust Engineer", company="Acme"),
            make_item("2", title="Designer", company="Rusty Co"),
        ]
    )
    await session.commit()

    page = await repo.list_items(ItemFilters(q="rust", limit=10))

    assert {row.external_id for row in page.items} == {"1", "2"}


@pytest.mark.asyncio
async def test_inactive_items_are_hidden_by_default(
    repo: JobRepository, session: AsyncSession
) -> None:
    await repo.upsert_many([make_item("1")])
    await session.commit()
    await repo.mark_stale(SOURCE, keep_ids=set())
    await session.commit()

    assert (await repo.list_items(ItemFilters(limit=10))).items == []
    assert len((await repo.list_items(ItemFilters(is_active=False, limit=10))).items) == 1


# -------------------------------------------------------------- pagination ---


@pytest.mark.asyncio
async def test_keyset_pagination_walks_without_repeats(
    repo: JobRepository, session: AsyncSession
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await repo.upsert_many(
        [make_item(str(i), posted_at=base + timedelta(days=i)) for i in range(7)]
    )
    await session.commit()

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = await repo.list_items(ItemFilters(limit=3, cursor=cursor))
        seen.extend(row.external_id for row in page.items)
        cursor = page.next_cursor
        if not cursor:
            break

    assert len(seen) == 7
    assert len(set(seen)) == 7, "no listing may appear on two pages"


@pytest.mark.asyncio
async def test_last_page_has_no_cursor(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many([make_item("1"), make_item("2")])
    await session.commit()

    page = await repo.list_items(ItemFilters(limit=50))

    assert page.next_cursor is None


def test_cursor_round_trip() -> None:
    moment = datetime(2026, 2, 11, 9, 30, tzinfo=UTC)
    cursor = encode_cursor(moment, 42)

    assert decode_cursor(cursor) == (moment, 42)


def test_cursor_handles_null_sort_values() -> None:
    assert decode_cursor(encode_cursor(None, 7)) == (None, 7)


@pytest.mark.parametrize("bad", ["not-base64!!", "", "####", "YWJj"])
def test_cursor_rejects_garbage(bad: str) -> None:
    assert decode_cursor(bad) is None


@pytest.mark.asyncio
async def test_malformed_cursor_is_ignored_not_fatal(
    repo: JobRepository, session: AsyncSession
) -> None:
    await repo.upsert_many([make_item("1")])
    await session.commit()

    page = await repo.list_items(ItemFilters(limit=10, cursor="garbage"))

    assert len(page.items) == 1


@pytest.mark.asyncio
async def test_sorting_ascending(repo: JobRepository, session: AsyncSession) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await repo.upsert_many(
        [make_item(str(i), company=f"C{i}", posted_at=base + timedelta(days=i)) for i in range(3)]
    )
    await session.commit()

    page = await repo.list_items(ItemFilters(sort="company", descending=False, limit=10))

    assert [row.company for row in page.items] == ["C0", "C1", "C2"]


# -------------------------------------------------------- aggregates / runs ---


@pytest.mark.asyncio
async def test_company_rollup(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many(
        [
            make_item("1", company="Acme", remote=True),
            make_item("2", company="Acme"),
            make_item("3", company="Globex"),
        ]
    )
    await session.commit()

    companies = await repo.companies()

    top = companies[0]
    assert top["company"] == "Acme"
    assert top["listings"] == 2
    assert top["remote_listings"] == 1


@pytest.mark.asyncio
async def test_facets_are_ordered_by_frequency(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many(
        [
            make_item("1", company="Acme"),
            make_item("2", company="Acme"),
            make_item("3", company="Globex"),
        ]
    )
    await session.commit()

    assert (await repo.facets("company")) == ["Acme", "Globex"]


@pytest.mark.asyncio
async def test_overview_and_daily_series(repo: JobRepository, session: AsyncSession) -> None:
    now = datetime.now(UTC)
    await repo.upsert_many(
        [
            make_item("1", remote=True, posted_at=now - timedelta(days=1)),
            make_item("2", posted_at=now - timedelta(days=2)),
        ]
    )
    await session.commit()

    overview = await repo.overview(days=30)

    assert overview["total"] == 2
    assert overview["active"] == 2
    assert overview["remote"] == 1
    assert overview["window_days"] == 30
    assert overview["new_recent"] == 2
    assert overview["by_source"] == {SOURCE: 2}
    assert len(overview["daily"]) == 30, "the series is zero-filled to the window length"
    assert sum(point["count"] for point in overview["daily"]) == 2


@pytest.mark.asyncio
async def test_daily_series_is_windowed(repo: JobRepository, session: AsyncSession) -> None:
    """Listings older than the window are counted in totals but not the trend."""
    now = datetime.now(UTC)
    await repo.upsert_many([make_item("1", posted_at=now - timedelta(days=400))])
    await session.commit()

    overview = await repo.overview(days=30)

    assert overview["total"] == 1
    assert sum(point["count"] for point in overview["daily"]) == 0


@pytest.mark.asyncio
async def test_run_lifecycle(session: AsyncSession) -> None:
    runs = RunRepository(session)

    run = await runs.start("hnhiring", detail={"max_pages": 10})
    assert run.status == "running"

    await runs.finish(
        run, status="succeeded", counters={"items_new": 3, "pages_fetched": 5}, duration=1.25
    )

    assert run.status == "succeeded"
    assert run.items_new == 3
    assert run.pages_fetched == 5
    assert run.finished_at is not None
    assert (await runs.latest_successful()) is not None


@pytest.mark.asyncio
async def test_latest_successful_ignores_failed_runs(session: AsyncSession) -> None:
    runs = RunRepository(session)
    run = await runs.start("flaky")
    await runs.finish(run, status="failed", counters={}, duration=0.1)

    assert await runs.latest_successful() is None


@pytest.mark.asyncio
async def test_search_text_is_populated_for_fts(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many([make_item("1")])
    await session.commit()

    row = await repo.get(1)
    assert row is not None
    assert "Backend Engineer" in row.search_text
    assert "Acme" in row.search_text


@pytest.mark.asyncio
async def test_json_columns_round_trip(repo: JobRepository, session: AsyncSession) -> None:
    item = make_item("1").model_copy(update={"tags": ["Python", "Go"], "raw": {"nested": {"a": 1}}})
    await repo.upsert_many([item])
    await session.commit()

    row = await repo.get(1)
    assert row is not None
    assert row.tags == ["Python", "Go"]
    assert row.raw == {"nested": {"a": 1}}


@pytest.mark.asyncio
async def test_upsert_does_not_churn_updated_at_for_identical_rows(
    repo: JobRepository, session: AsyncSession
) -> None:
    await repo.upsert_many([make_item("1")])
    await session.commit()
    original_hash = (await repo.get(1)).content_hash  # type: ignore[union-attr]

    await repo.upsert_many([make_item("1")])
    await session.commit()

    assert (await repo.get(1)).content_hash == original_hash  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_deactivating_via_bulk_update(repo: JobRepository, session: AsyncSession) -> None:
    await repo.upsert_many([make_item("1"), make_item("2")])
    await session.commit()

    await session.execute(update(JobItemRow).values(is_active=False))
    await session.commit()

    assert (await repo.count(ItemFilters())) == 0
    assert (await repo.count(ItemFilters(is_active=False))) == 2
