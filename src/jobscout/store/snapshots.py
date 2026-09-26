"""Database implementation of the raw-response archive.

Keeps the latest body per URL. Older bodies are pruned on demand by
:func:`prune_snapshots`, because the archive is a debugging and re-parsing tool,
not an audit log: one copy per URL is what ``jobscout reparse`` needs.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.fetch.snapshot import compress, decompress
from jobscout.models import FetchedPage
from jobscout.observability import get_logger
from jobscout.store.models import RawPage

_log = get_logger(__name__)


class DbSnapshotStore:
    """Archives response bodies in the ``raw_pages`` table."""

    def __init__(self, session: AsyncSession, dialect: str = "postgresql") -> None:
        self.session = session
        self.dialect = dialect

    async def save(
        self, page: FetchedPage, *, adapter: str, status_code: int | None = None
    ) -> None:
        payload = compress(page.text)
        values = {
            "url": page.url,
            "status_code": status_code if status_code is not None else page.status_code,
            "adapter": adapter,
            "kind": page.kind,
            "content_hash": page.content_hash,
            "size_bytes": len(payload),
            # Cookies and auth-ish headers are deliberately not archived.
            "headers": {k: v for k, v in page.headers.items() if k != "set-cookie"},
            "body_gz": payload,
            "fetched_at": page.fetched_at,
        }
        # Pass the mapped class, not `__table__`: the dialect insert helpers
        # accept either, but only the former is typed as an insertable target.
        builder = pg_insert if self.dialect == "postgresql" else sqlite_insert
        await self.session.execute(builder(RawPage).values(**values))
        await self.session.flush()

    async def load(self, url: str) -> FetchedPage | None:
        stmt = (
            select(RawPage)
            .where(RawPage.url == url)
            .order_by(RawPage.fetched_at.desc(), RawPage.id.desc())
            .limit(1)
        )
        row = (await self.session.execute(stmt)).scalars().first()
        if row is None:
            return None
        return FetchedPage(
            url=row.url,
            status_code=row.status_code,
            text=decompress(row.body_gz),
            headers=dict(row.headers or {}),
            content_hash=row.content_hash,
            fetched_at=row.fetched_at,
            from_snapshot=True,
            kind=row.kind,
        )

    async def urls(self, *, adapter: str | None = None, limit: int = 5000) -> list[str]:
        """Distinct archived URLs, most recently fetched first."""
        stmt = select(RawPage.url).order_by(RawPage.fetched_at.desc())
        if adapter is not None:
            stmt = stmt.where(RawPage.adapter == adapter)
        rows = (await self.session.execute(stmt.limit(limit * 4))).scalars().all()
        seen: dict[str, None] = {}
        for url in rows:
            seen.setdefault(url, None)
            if len(seen) >= limit:
                break
        return list(seen)

    async def count(self) -> int:
        stmt = select(func.count()).select_from(RawPage)
        return int((await self.session.execute(stmt)).scalar_one())


async def prune_snapshots(session: AsyncSession, *, keep_per_url: int = 1) -> int:
    """Delete all but the ``keep_per_url`` newest archives for each URL.

    Uses a window function so it behaves identically on PostgreSQL and SQLite.
    """
    if keep_per_url < 1:
        raise ValueError("keep_per_url must be >= 1")

    ranked = select(
        RawPage.id.label("id"),
        func.row_number()
        .over(partition_by=RawPage.url, order_by=RawPage.fetched_at.desc())
        .label("rn"),
    ).subquery()
    doomed = select(ranked.c.id).where(ranked.c.rn > keep_per_url)
    result = await session.execute(delete(RawPage).where(RawPage.id.in_(doomed)))
    removed = int(getattr(result, "rowcount", 0) or 0)
    if removed:
        _log.info("snapshot.pruned", rows_removed=removed, keep_per_url=keep_per_url)
    return removed
