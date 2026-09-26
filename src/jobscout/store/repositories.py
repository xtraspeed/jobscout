"""Repositories: all SQL lives here, nothing else in the codebase writes queries.

The interesting part is :meth:`JobRepository.upsert_many`, which implements
change detection: it compares incoming ``content_hash`` values against what is
already stored and only writes rows that are new or genuinely different. That
keeps re-crawls cheap and makes ``last_seen_at`` a reliable "still listed"
signal without touching unchanged rows.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Select, and_, case, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from jobscout.models import JobItem, utcnow
from jobscout.observability import get_logger
from jobscout.observability.metrics import DB_WRITE_SECONDS
from jobscout.store.models import CrawlRun, FetchError, JobItemRow

_log = get_logger(__name__)

SortField = Literal["posted_at", "first_seen_at", "last_seen_at", "company"]

_SORT_COLUMNS: dict[SortField, Any] = {
    "posted_at": JobItemRow.posted_at,
    "first_seen_at": JobItemRow.first_seen_at,
    "last_seen_at": JobItemRow.last_seen_at,
    "company": JobItemRow.company,
}

#: Columns an upsert may overwrite. Deliberately excludes ``first_seen_at``:
#: it records when a listing was first observed and must never move.
UPDATABLE_COLUMNS: tuple[str, ...] = (
    "run_id",
    "url",
    "title",
    "company",
    "location",
    "remote",
    "employment_type",
    "salary_min",
    "salary_max",
    "salary_currency",
    "description",
    "tags",
    "raw",
    "search_text",
    "posted_at",
    "content_hash",
    "last_seen_at",
    "is_active",
)


# ------------------------------------------------------- pure SQL builders ---
#
# Session-free and module-level so the dialect-specific statements can be
# compiled and asserted without a live database. Without this, the PostgreSQL
# branch of each statement is only ever exercised in production.


def build_upsert_statement(dialect: str, rows: Sequence[dict[str, Any]]) -> Any:
    """Dialect-appropriate ``INSERT ... ON CONFLICT DO UPDATE`` for listings.

    The ``WHERE`` clause on the conflict action is what makes a re-crawl cheap: a
    row whose content hash is unchanged becomes a no-op instead of a write.
    """
    if not rows:
        raise ValueError("build_upsert_statement requires at least one row")
    builder = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = builder(JobItemRow).values(list(rows))
    return stmt.on_conflict_do_update(
        index_elements=["source", "external_id"],
        set_={column: getattr(stmt.excluded, column) for column in UPDATABLE_COLUMNS},
        where=(JobItemRow.content_hash != stmt.excluded.content_hash),
    )


def build_search_condition(dialect: str, column: Any, query: str) -> Any:
    """Full-text predicate over ``column``.

    PostgreSQL uses a ``tsvector`` with the ``simple`` configuration --
    deliberately not English-stemmed, because job titles are full of proper nouns
    and identifiers (``C++``, ``Node.js``, ``M4``) that stemming mangles. SQLite
    falls back to a substring match so the test suite needs no server.
    """
    if dialect == "postgresql":
        vector = func.to_tsvector("simple", func.coalesce(column, ""))
        return vector.op("@@")(func.plainto_tsquery("simple", query))
    pattern = f"%{query.strip()}%"
    return or_(column.ilike(pattern), JobItemRow.title.ilike(pattern))


def build_day_bucket(dialect: str, column: Any) -> Any:
    """Truncate a timestamp column to a day, for the trend series."""
    if dialect == "postgresql":
        return func.date_trunc("day", column)
    return func.strftime("%Y-%m-%d", column)


@dataclass(slots=True)
class WriteStats:
    """Outcome of a batch write, used for run counters and metrics."""

    new: int = 0
    changed: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.new + self.changed + self.unchanged

    def merge(self, other: WriteStats) -> None:
        self.new += other.new
        self.changed += other.changed
        self.unchanged += other.unchanged

    def as_dict(self) -> dict[str, int]:
        return {"new": self.new, "changed": self.changed, "unchanged": self.unchanged}


@dataclass(slots=True)
class ItemFilters:
    """Everything the query API can filter on."""

    q: str | None = None
    companies: list[str] = field(default_factory=list)
    location: str | None = None
    remote: bool | None = None
    employment_type: str | None = None
    source: str | None = None
    posted_after: datetime | None = None
    min_salary: float | None = None
    has_salary: bool | None = None
    is_active: bool = True
    sort: SortField = "posted_at"
    descending: bool = True
    limit: int = 25
    cursor: str | None = None


@dataclass(slots=True)
class ItemPage:
    items: list[JobItemRow]
    next_cursor: str | None
    total_estimate: int | None = None


# --------------------------------------------------------------------- jobs --


class JobRepository:
    """Reads and writes :class:`JobItemRow`."""

    def __init__(self, session: AsyncSession, dialect: str = "postgresql") -> None:
        self.session = session
        self.dialect = dialect

    # -- writes -------------------------------------------------------------

    async def upsert_many(self, items: Sequence[JobItem], run_id: int | None = None) -> WriteStats:
        """Insert new listings, update changed ones, skip identical ones.

        Implemented as a single ``INSERT ... ON CONFLICT DO UPDATE ... WHERE
        content_hash IS DISTINCT`` so a re-crawl of unchanged data performs one
        cheap no-op write per row instead of an update.
        """
        if not items:
            return WriteStats()

        with DB_WRITE_SECONDS.labels(operation="upsert_many").time():
            keys = [(item.source, item.external_id) for item in items]
            existing = await self._existing_hashes(keys)
            stats = WriteStats()
            for item in items:
                known = existing.get((item.source, item.external_id))
                if known is None:
                    stats.new += 1
                elif known == item.content_hash:
                    stats.unchanged += 1
                else:
                    stats.changed += 1

            now = utcnow()
            rows = [self._to_row(item, run_id=run_id, now=now) for item in items]
            await self._bulk_upsert(rows)
            return stats

    async def _existing_hashes(self, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Fetch stored hashes for the natural keys in this batch."""
        found: dict[tuple[str, str], str] = {}
        # IN with a tuple of columns needs chunking on some backends.
        chunk_size = 500
        for start in range(0, len(keys), chunk_size):
            chunk = keys[start : start + chunk_size]
            stmt = select(JobItemRow.source, JobItemRow.external_id, JobItemRow.content_hash).where(
                tuple_(JobItemRow.source, JobItemRow.external_id).in_(chunk)
            )
            for source, external_id, content_hash in (await self.session.execute(stmt)).all():
                found[(source, external_id)] = content_hash
        return found

    async def _bulk_upsert(self, rows: Sequence[dict[str, Any]]) -> None:
        await self.session.execute(build_upsert_statement(self.dialect, rows))

    @staticmethod
    def _to_row(item: JobItem, *, run_id: int | None, now: datetime) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "source": item.source,
            "external_id": item.external_id,
            "url": item.url,
            "title": item.title,
            "company": item.company,
            "location": item.location,
            "remote": item.remote,
            "employment_type": item.employment_type,
            "salary_min": item.salary_min,
            "salary_max": item.salary_max,
            "salary_currency": item.salary_currency,
            "description": item.description,
            "tags": list(item.tags),
            "raw": item.raw,
            "search_text": item.search_text,
            "posted_at": item.posted_at,
            "content_hash": item.content_hash,
            "first_seen_at": now,
            "last_seen_at": now,
            "is_active": True,
        }

    async def mark_stale(self, source: str, run_id: int, *, keep_ids: set[str]) -> int:
        """Flag listings from a previous run of ``source`` that this run missed.

        Gives the dataset a real "no longer advertised" signal instead of
        assuming every previously-seen listing is still live.
        """
        stmt = update(JobItemRow).where(JobItemRow.source == source, JobItemRow.is_active.is_(True))
        if keep_ids:
            stmt = stmt.where(~JobItemRow.external_id.in_(keep_ids))
        result = await self.session.execute(stmt.values(is_active=False, run_id=run_id))
        return int(getattr(result, "rowcount", 0) or 0)

    # -- reads --------------------------------------------------------------

    async def get(self, item_id: int) -> JobItemRow | None:
        return await self.session.get(JobItemRow, item_id)

    async def count(self, filters: ItemFilters | None = None) -> int:
        stmt = select(func.count()).select_from(JobItemRow)
        stmt = self._apply_filters(stmt, filters or ItemFilters())
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_items(self, filters: ItemFilters) -> ItemPage:
        """Keyset-paginated listing query (``limit + 1`` to detect a next page)."""
        stmt = select(JobItemRow)
        stmt = self._apply_filters(stmt, filters)

        sort_column = _SORT_COLUMNS[filters.sort]
        direction = sort_column.desc() if filters.descending else sort_column.asc()
        stmt = stmt.order_by(direction, JobItemRow.id.desc())
        stmt = stmt.limit(filters.limit + 1)

        rows = list((await self.session.execute(stmt)).scalars().all())
        has_more = len(rows) > filters.limit
        rows = rows[: filters.limit]

        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = encode_cursor(getattr(last, filters.sort), last.id)

        return ItemPage(items=rows, next_cursor=next_cursor)

    async def companies(self, *, limit: int = 100, is_active: bool = True) -> list[dict[str, Any]]:
        stmt = (
            select(
                JobItemRow.company,
                func.count(JobItemRow.id).label("listings"),
                func.sum(case((JobItemRow.remote.is_(True), 1), else_=0)).label("remote_listings"),
                func.max(JobItemRow.posted_at).label("latest_posted"),
                func.max(JobItemRow.salary_max).label("max_salary"),
            )
            .where(JobItemRow.is_active.is_(is_active))
            .group_by(JobItemRow.company)
            .order_by(func.count(JobItemRow.id).desc())
            .limit(limit)
        )
        return [
            {
                "company": row.company,
                "listings": int(row.listings or 0),
                "remote_listings": int(row.remote_listings or 0),
                "latest_posted": row.latest_posted,
                "max_salary": row.max_salary,
            }
            for row in (await self.session.execute(stmt)).all()
        ]

    async def facets(self, field_name: str, *, limit: int = 50) -> list[str]:
        """Distinct values for a column, most frequent first (for filter UIs)."""
        column = getattr(JobItemRow, field_name)
        stmt = (
            select(column)
            .where(column.is_not(None), JobItemRow.is_active.is_(True))
            .group_by(column)
            .order_by(func.count(JobItemRow.id).desc())
            .limit(limit)
        )
        return [row[0] for row in (await self.session.execute(stmt)).all()]

    async def overview(self, *, days: int = 30) -> dict[str, Any]:
        """Headline numbers plus a daily series, for the dashboard."""
        total, active = (
            await self.session.execute(
                select(
                    func.count(JobItemRow.id),
                    func.sum(case((JobItemRow.is_active.is_(True), 1), else_=0)),
                )
            )
        ).one()
        remote_total, with_salary = (
            await self.session.execute(
                select(
                    func.sum(case((JobItemRow.remote.is_(True), 1), else_=0)),
                    func.sum(case((JobItemRow.salary_max.is_not(None), 1), else_=0)),
                )
            )
        ).one()
        since = utcnow() - timedelta(days=days)
        recent = int(
            (
                await self.session.execute(
                    select(func.count(JobItemRow.id)).where(JobItemRow.first_seen_at >= since)
                )
            ).scalar_one()
        )
        by_source = {
            row.source: int(row.n)
            for row in (
                await self.session.execute(
                    select(JobItemRow.source, func.count(JobItemRow.id).label("n"))
                    .group_by(JobItemRow.source)
                    .order_by(func.count(JobItemRow.id).desc())
                )
            ).all()
        }
        return {
            "total": int(total or 0),
            "active": int(active or 0),
            "remote": int(remote_total or 0),
            "with_salary": int(with_salary or 0),
            "new_recent": recent,
            "window_days": days,
            "by_source": by_source,
            "daily": await self.daily_series(days=days),
        }

    async def daily_series(
        self, *, days: int = 30, column: str = "posted_at"
    ) -> list[dict[str, Any]]:
        """Listings per day, zero-filled so charts have no gaps."""
        target = getattr(JobItemRow, column)
        bucket = build_day_bucket(self.dialect, target)
        since = utcnow() - timedelta(days=days - 1)
        stmt = (
            select(bucket.label("day"), func.count(JobItemRow.id).label("n"))
            .where(target.is_not(None), target >= since)
            .group_by(bucket)
            .order_by(bucket)
        )
        counts = {_as_day(row.day): int(row.n) for row in (await self.session.execute(stmt)).all()}
        return _fill_days(counts, days=days)

    # -- filtering ----------------------------------------------------------

    def _apply_filters(self, stmt: Select[Any], filters: ItemFilters) -> Select[Any]:
        conditions: list[ColumnElement[bool]] = []
        if filters.is_active:
            conditions.append(JobItemRow.is_active.is_(True))
        if filters.companies:
            conditions.append(JobItemRow.company.in_(filters.companies))
        if filters.location:
            conditions.append(JobItemRow.location.ilike(f"%{filters.location}%"))
        if filters.remote is not None:
            conditions.append(JobItemRow.remote.is_(filters.remote))
        if filters.employment_type:
            conditions.append(JobItemRow.employment_type == filters.employment_type)
        if filters.source:
            conditions.append(JobItemRow.source == filters.source)
        if filters.posted_after:
            conditions.append(JobItemRow.posted_at >= filters.posted_after)
        if filters.has_salary is True:
            conditions.append(JobItemRow.salary_max.is_not(None))
        elif filters.has_salary is False:
            conditions.append(JobItemRow.salary_max.is_(None))
        if filters.min_salary is not None:
            # A listing qualifies if any part of its range clears the bar.
            conditions.append(
                or_(
                    JobItemRow.salary_max >= filters.min_salary,
                    JobItemRow.salary_min >= filters.min_salary,
                )
            )
        if filters.q:
            conditions.append(self._search_condition(filters.q))
        if filters.cursor:
            decoded = decode_cursor(filters.cursor)
            if decoded is not None:
                conditions.append(self._keyset_condition(filters, decoded))

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    def _search_condition(self, query: str) -> Any:
        """Full-text search over the denormalised ``search_text`` column."""
        return build_search_condition(self.dialect, JobItemRow.search_text, query)

    @staticmethod
    def _keyset_condition(filters: ItemFilters, decoded: tuple[str | None, int]) -> Any:
        sort_value, last_id = decoded
        column = _SORT_COLUMNS[filters.sort]
        if sort_value is None:
            return JobItemRow.id < last_id
        comparison = column < sort_value if filters.descending else column > sort_value
        return or_(comparison, and_(column == sort_value, JobItemRow.id < last_id))


# --------------------------------------------------------------------- runs --


class RunRepository:
    """Crawl run bookkeeping."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start(self, adapter: str, detail: dict[str, Any] | None = None) -> CrawlRun:
        run = CrawlRun(
            adapter=adapter,
            status="running",
            started_at=utcnow(),
            detail=detail or {},
        )
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def finish(
        self,
        run: CrawlRun,
        *,
        status: str,
        counters: dict[str, Any],
        duration: float,
    ) -> CrawlRun:
        for key, value in counters.items():
            if hasattr(run, key):
                setattr(run, key, value)
        run.status = status
        run.finished_at = utcnow()
        run.duration_seconds = round(duration, 3)
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def get(self, run_id: int) -> CrawlRun | None:
        return await self.session.get(CrawlRun, run_id)

    async def list_recent(self, *, limit: int = 20) -> list[CrawlRun]:
        stmt = select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_successful(self) -> CrawlRun | None:
        stmt = (
            select(CrawlRun)
            .where(CrawlRun.status == "succeeded")
            .order_by(CrawlRun.started_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def failure_breakdown(self, run_id: int) -> list[dict[str, Any]]:
        stmt = (
            select(FetchError.error, func.count(FetchError.id).label("n"))
            .where(FetchError.run_id == run_id)
            .group_by(FetchError.error)
            .order_by(func.count(FetchError.id).desc())
            .limit(20)
        )
        return [
            {"error": row.error, "count": int(row.n)}
            for row in (await self.session.execute(stmt)).all()
        ]


class ErrorRepository:
    """Persists fetch failures for post-mortem debugging."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(self, run_id: int | None, url: str, error: str) -> None:
        self.session.add(FetchError(run_id=run_id, url=url, error=error[:2000]))
        await self.session.flush()

    async def clear_for_run(self, run_id: int) -> None:
        await self.session.execute(delete(FetchError).where(FetchError.run_id == run_id))


# ----------------------------------------------------------------- helpers ---


def encode_cursor(sort_value: Any, row_id: int) -> str:
    """Opaque keyset cursor: ``<sort value>|<id>``."""
    raw = f"{_iso(sort_value)}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str | None, int] | None:
    """Inverse of :func:`encode_cursor`; returns ``None`` for a malformed cursor."""
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        sort_value, _, row_id = raw.rpartition("|")
        return (_parse_iso(sort_value) if sort_value else None, int(row_id))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()
    return "" if value is None else str(value)


def _parse_iso(value: str) -> Any:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


def _as_day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str):
        return value[:10]
    return str(value)


def _fill_days(counts: dict[str, int], *, days: int) -> list[dict[str, Any]]:
    """Return a dense ``[{day, count}]`` series ending today."""
    today = utcnow().date()
    series: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        series.append({"day": day, "count": counts.get(day, 0)})
    return series
