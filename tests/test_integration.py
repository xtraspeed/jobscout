"""Integration tests against a real PostgreSQL.

Everything else in the suite runs on SQLite, which is what keeps it fast and
hermetic -- but it also means the PostgreSQL-only behaviour is never executed:
``ON CONFLICT DO UPDATE``, the GIN ``tsvector`` index, ``date_trunc``, ``JSONB``
round-trips, and concurrent upserts on the same natural key.

These tests cover exactly that, and nothing else. They need a dedicated
throwaway database and are skipped when it is not configured::

    export JOBSCOUT_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/jobscout_test
    pytest -m integration

**Point this at a disposable database only.** The fixture drops and recreates the
public schema.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.adapters.fixture import FixtureAdapter, FixtureBundle
from jobscout.config import Settings
from jobscout.models import JobItem
from jobscout.pipeline import run_crawl
from jobscout.store.db import create_engine, create_sessionmaker, session_scope
from jobscout.store.models import JobItemRow
from jobscout.store.repositories import ItemFilters, JobRepository, RunRepository

pytestmark = pytest.mark.integration

TESTS_DIR = Path(__file__).parent
REPO_ROOT = TESTS_DIR.parent

TEST_DATABASE_URL = os.getenv("JOBSCOUT_TEST_DATABASE_URL", "")
FIXTURE_BUNDLE = os.getenv("JOBSCOUT_TEST_FIXTURES", str(TESTS_DIR / "fixtures" / "demo_board"))

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL.startswith("postgresql"),
    reason="set JOBSCOUT_TEST_DATABASE_URL to a postgresql+asyncpg:// URL to run these",
)


def make_item(
    external_id: str = "1",
    *,
    title: str = "Senior Backend Engineer",
    company: str = "Acme",
    remote: bool = False,
    salary_max: float | None = 105_000.0,
    description: str = "Own the ingestion pipeline. Python and Postgres.",
    days_ago: int = 0,
    tags: list[str] | None = None,
) -> JobItem:
    return JobItem(
        source="board",
        external_id=external_id,
        url=f"https://board.test/jobs/{external_id}",
        title=title,
        company=company,
        location="Remote (EU)" if remote else "Berlin, Germany",
        remote=remote,
        employment_type="full-time",
        salary_min=salary_max,
        salary_max=salary_max,
        salary_currency="EUR",
        description=description,
        tags=tags if tags is not None else ["Python", "PostgreSQL"],
        raw={"nested": {"a": 1, "b": [2, 3]}},
        posted_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


@pytest.fixture(scope="module")
def pg_settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        database_pool_size=10,
        database_max_overflow=5,
        per_domain_rate=1000.0,
        per_domain_concurrency=8,
        max_retries=0,
        respect_robots=True,
        store_snapshots=True,
        log_level="WARNING",
        log_format="console",
    )


@pytest_asyncio.fixture
async def pg_engine(pg_settings: Settings) -> AsyncIterator[Any]:
    """A migrated, empty schema. The public schema is dropped first."""
    from alembic import command
    from alembic.config import Config

    if not TEST_DATABASE_URL.startswith("postgresql"):
        pytest.skip("no PostgreSQL configured")

    sync_url = pg_settings.sync_database_url
    engine = create_engine(pg_settings)

    # Recreate from scratch so every test starts from a known schema.
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(config, "head")

    engine = create_engine(pg_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def pg_sessionmaker(pg_engine: Any) -> Any:
    return create_sessionmaker(pg_engine)


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(pg_sessionmaker: Any) -> AsyncIterator[None]:
    """Empty every table before each test.

    The engine (and therefore the schema) is module-scoped because running the
    migrations per test is slow; truncating with ``RESTART IDENTITY`` gives each
    test the same clean slate a fresh database would, including predictable ids.
    """
    async with pg_sessionmaker() as session:
        await session.execute(
            text("TRUNCATE job_items, raw_pages, crawl_runs, fetch_errors RESTART IDENTITY CASCADE")
        )
        await session.commit()
    yield


@pytest_asyncio.fixture
async def pg_session(pg_sessionmaker: Any) -> AsyncIterator[AsyncSession]:
    async with pg_sessionmaker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def pg_repo(pg_session: AsyncSession) -> JobRepository:
    return JobRepository(pg_session, dialect="postgresql")


# ------------------------------------------------------------------ dialect ---


@requires_postgres
@pytest.mark.asyncio
async def test_migrations_apply_to_postgres(pg_engine: Any) -> None:
    async with pg_engine.connect() as connection:
        version = await connection.execute(text("SELECT version_num FROM alembic_version"))
        assert version.scalar_one() == "0001_initial"


@requires_postgres
@pytest.mark.asyncio
async def test_full_text_index_exists(pg_engine: Any) -> None:
    async with pg_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_job_items_search_fts'")
        )
        definition = result.scalar_one()
    assert "USING gin" in definition
    assert "to_tsvector" in definition


@requires_postgres
@pytest.mark.asyncio
async def test_natural_key_constraint_is_enforced(pg_session: AsyncSession) -> None:
    from sqlalchemy.exc import IntegrityError

    await pg_session.execute(
        text(
            "INSERT INTO job_items (source, external_id, url, title, company, description, "
            "tags, raw, search_text, content_hash, first_seen_at, last_seen_at) "
            "VALUES ('s', '1', 'u', 'T', 'C', '', '[]', '{}', 'T', 'h', now(), now())"
        )
    )
    with pytest.raises(IntegrityError):
        await pg_session.execute(
            text(
                "INSERT INTO job_items (source, external_id, url, title, company, description, "
                "tags, raw, search_text, content_hash, first_seen_at, last_seen_at) "
                "VALUES ('s', '1', 'u2', 'T', 'C', '', '[]', '{}', 'T', 'h2', now(), now())"
            )
        )
    await pg_session.rollback()


# ------------------------------------------------------------------- upsert ---


@requires_postgres
@pytest.mark.asyncio
async def test_upsert_round_trips_jsonb(pg_repo: JobRepository, pg_session: AsyncSession) -> None:
    await pg_repo.upsert_many([make_item("1", tags=["Python", "Kafka"])])
    await pg_session.commit()

    row = await pg_repo.get(1)
    assert row is not None
    assert row.tags == ["Python", "Kafka"]
    assert row.raw == {"nested": {"a": 1, "b": [2, 3]}}


@requires_postgres
@pytest.mark.asyncio
async def test_upsert_is_idempotent(pg_repo: JobRepository, pg_session: AsyncSession) -> None:
    await pg_repo.upsert_many([make_item("1"), make_item("2")])
    await pg_session.commit()

    stats = await pg_repo.upsert_many([make_item("1"), make_item("2")])
    await pg_session.commit()

    assert (stats.new, stats.changed, stats.unchanged) == (0, 0, 2)
    count = await pg_session.execute(select(func.count()).select_from(JobItemRow))
    assert count.scalar_one() == 2


@requires_postgres
@pytest.mark.asyncio
async def test_changed_content_updates_in_place(
    pg_repo: JobRepository, pg_session: AsyncSession
) -> None:
    await pg_repo.upsert_many([make_item("1")])
    await pg_session.commit()
    original_first_seen = (await pg_repo.get(1)).first_seen_at  # type: ignore[union-attr]

    stats = await pg_repo.upsert_many([make_item("1", title="Staff Backend Engineer")])
    await pg_session.commit()

    assert stats.changed == 1
    row = await pg_repo.get(1)
    assert row is not None
    assert row.title == "Staff Backend Engineer"
    assert row.first_seen_at == original_first_seen


@requires_postgres
@pytest.mark.asyncio
async def test_concurrent_upserts_have_exactly_one_winner(
    pg_sessionmaker: Any, pg_session: AsyncSession
) -> None:
    """The natural key must resolve a race, not raise or duplicate.

    Eight connections write the same ``(source, external_id)`` with *different*
    content at the same time. Exactly one row must survive, holding one of the
    submitted values, and no transaction may fail.
    """
    contenders = 8

    async def write(index: int) -> None:
        async with pg_sessionmaker() as session:
            repo = JobRepository(session, dialect="postgresql")
            await repo.upsert_many([make_item("race", title=f"Contender {index}")])
            await session.commit()

    await asyncio.gather(*(write(index) for index in range(contenders)))

    rows = (
        (await pg_session.execute(select(JobItemRow).where(JobItemRow.external_id == "race")))
        .scalars()
        .all()
    )

    assert len(rows) == 1, "the unique key must collapse the race"
    assert rows[0].title in {f"Contender {index}" for index in range(contenders)}


# ------------------------------------------------------------------- search ---


@requires_postgres
@pytest.mark.asyncio
async def test_tsvector_search_matches_content(
    pg_repo: JobRepository, pg_session: AsyncSession
) -> None:
    await pg_repo.upsert_many(
        [
            make_item("1", description="Work on Kafka streaming pipelines."),
            make_item("2", description="Design a design system.", company="Contoso"),
        ]
    )
    await pg_session.commit()

    hit = await pg_repo.list_items(ItemFilters(q="kafka", limit=10))
    assert [row.external_id for row in hit.items] == ["1"]

    miss = await pg_repo.list_items(ItemFilters(q="kubernetes", limit=10))
    assert miss.items == []


@requires_postgres
@pytest.mark.asyncio
async def test_tsvector_search_does_not_stem_job_titles(
    pg_repo: JobRepository, pg_session: AsyncSession
) -> None:
    """'simple' keeps identifiers intact; an English stemmer would not."""
    await pg_repo.upsert_many([make_item("1", title="Node.js Engineer")])
    await pg_session.commit()

    page = await pg_repo.list_items(ItemFilters(q="Node.js", limit=10))
    assert [row.external_id for row in page.items] == ["1"]


@requires_postgres
@pytest.mark.asyncio
async def test_search_text_is_never_null(pg_repo: JobRepository, pg_session: AsyncSession) -> None:
    """`search_text` is NOT NULL, so the tsvector expression never sees NULL.

    The repository always derives it from the item, and the column's NOT NULL
    constraint is what guarantees that -- an assertion worth pinning down,
    because the expression index is built on this column.
    """
    await pg_repo.upsert_many([make_item("1")])
    await pg_session.commit()

    row = await pg_repo.get(1)
    assert row is not None
    assert row.search_text

    nulls = await pg_session.execute(
        text("SELECT count(*) FROM job_items WHERE search_text IS NULL")
    )
    assert nulls.scalar_one() == 0

    # Search therefore cannot be defeated by a missing value.
    page = await pg_repo.list_items(ItemFilters(q="python", limit=10))
    assert [row.external_id for row in page.items] == ["1"]


# --------------------------------------------------------- series & paging ---


@requires_postgres
@pytest.mark.asyncio
async def test_daily_series_uses_date_trunc(
    pg_repo: JobRepository, pg_session: AsyncSession
) -> None:
    await pg_repo.upsert_many(
        [make_item("1", days_ago=0), make_item("2", days_ago=1), make_item("3", days_ago=3)]
    )
    await pg_session.commit()

    series = await pg_repo.daily_series(days=7)

    assert len(series) == 7
    assert sum(point["count"] for point in series) == 3
    assert series[-1]["count"] == 1, "today's bucket"
    assert series[-2]["count"] == 1, "yesterday's bucket"
    assert series[-4]["count"] == 1


@requires_postgres
@pytest.mark.asyncio
async def test_keyset_pagination_over_postgres(
    pg_repo: JobRepository, pg_session: AsyncSession
) -> None:
    await pg_repo.upsert_many([make_item(str(index), days_ago=index) for index in range(9)])
    await pg_session.commit()

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = await pg_repo.list_items(ItemFilters(limit=4, cursor=cursor))
        seen.extend(row.external_id for row in page.items)
        cursor = page.next_cursor
        if not cursor:
            break

    assert len(seen) == 9
    assert len(set(seen)) == 9


@requires_postgres
@pytest.mark.asyncio
async def test_mark_stale_on_postgres(pg_repo: JobRepository, pg_session: AsyncSession) -> None:
    await pg_repo.upsert_many([make_item("1"), make_item("2"), make_item("3")])
    await pg_session.commit()

    marked = await pg_repo.mark_stale("board", run_id=0, keep_ids={"1", "2"})
    await pg_session.commit()

    assert marked == 1
    assert (await pg_repo.count(ItemFilters())) == 2


@requires_postgres
@pytest.mark.asyncio
async def test_timestamptz_is_timezone_aware(
    pg_repo: JobRepository, pg_session: AsyncSession
) -> None:
    """Timestamps must come back aware, or date maths silently breaks."""
    await pg_repo.upsert_many([make_item("1")])
    await pg_session.commit()

    row = await pg_repo.get(1)
    assert row is not None
    assert row.posted_at is not None
    assert row.posted_at.tzinfo is not None


# ------------------------------------------------------------------ crawler ---


@requires_postgres
@pytest.mark.asyncio
async def test_full_crawl_against_postgres(
    pg_settings: Settings, pg_sessionmaker: Any, pg_session: AsyncSession
) -> None:
    """The whole pipeline on the real target database."""
    from jobscout.fetch.transport import fixture_transport

    adapter = FixtureAdapter(FixtureBundle(FIXTURE_BUNDLE))

    def resolve(url: str) -> tuple[int, str, str] | None:
        page = adapter.page_for(url)
        if page.status_code == 404:
            return None
        return page.status_code, page.text, "text/html"

    result = await run_crawl(
        pg_settings, adapter, transport=fixture_transport(resolve), sessionmaker=pg_sessionmaker
    )

    assert result.status == "succeeded"
    assert result.writes.new == 4

    repo = JobRepository(pg_session, dialect="postgresql")
    assert await repo.count(ItemFilters()) == 4

    # Idempotency on the real engine.
    second = await run_crawl(
        pg_settings, adapter, transport=fixture_transport(resolve), sessionmaker=pg_sessionmaker
    )
    assert (second.writes.new, second.writes.changed) == (0, 0)
    assert second.writes.unchanged == 4

    runs = await RunRepository(pg_session).list_recent()
    assert len(runs) == 2
    assert all(run.status == "succeeded" for run in runs)
    assert all(run.items_new is not None for run in runs)


@requires_postgres
@pytest.mark.asyncio
async def test_raw_pages_archive_round_trips(
    pg_settings: Settings, pg_sessionmaker: Any, pg_session: AsyncSession
) -> None:
    """The gzipped archive must survive a real PostgreSQL round trip."""
    from jobscout.fetch.transport import fixture_transport
    from jobscout.store.snapshots import DbSnapshotStore

    adapter = FixtureAdapter(FixtureBundle(FIXTURE_BUNDLE))

    def resolve(url: str) -> tuple[int, str, str] | None:
        page = adapter.page_for(url)
        return None if page.status_code == 404 else (page.status_code, page.text, "text/html")

    await run_crawl(
        pg_settings, adapter, transport=fixture_transport(resolve), sessionmaker=pg_sessionmaker
    )

    store = DbSnapshotStore(pg_session, dialect="postgresql")
    urls = await store.urls(adapter=adapter.name)
    assert len(urls) == 6

    page = await store.load("https://demo-board.example/jobs/1001")
    assert page is not None
    assert page.from_snapshot is True
    assert page.kind == "detail", "the archive must remember the request kind"
    assert "Senior Backend Engineer" in page.text


@requires_postgres
@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_error(pg_settings: Settings) -> None:
    """A failing transaction must not leave partial rows behind."""
    engine = create_engine(pg_settings)
    sessionmaker = create_sessionmaker(engine)
    try:
        with pytest.raises(RuntimeError):
            async with session_scope(sessionmaker) as session:
                repo = JobRepository(session, dialect="postgresql")
                await repo.upsert_many([make_item("rollback-me")])
                raise RuntimeError("boom")

        async with session_scope(sessionmaker) as session:
            repo = JobRepository(session, dialect="postgresql")
            assert await repo.count(ItemFilters()) == 0
    finally:
        await engine.dispose()
