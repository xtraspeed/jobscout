"""Engine and session management.

Capacity note: total connections are ``replicas x processes x
(pool_size + max_overflow)``, so those numbers must be sized against the
PostgreSQL ``max_connections`` budget rather than picked generously.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jobscout.config import Settings
from jobscout.observability import get_logger

_log = get_logger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]


def create_engine(settings: Settings, *, url: str | None = None) -> AsyncEngine:
    """Build an :class:`AsyncEngine` for the configured database.

    SQLite uses ``NullPool``: a pooled aiosqlite connection is bound to the event
    loop that opened it, which breaks when the test client opens a new loop per
    request. NullPool keeps SQLite safe everywhere and is irrelevant to Postgres.
    """
    target = url or settings.database_url
    if target.startswith("sqlite"):
        engine = create_async_engine(
            target,
            echo=settings.database_echo,
            poolclass=NullPool,
            future=True,
        )
        _enable_sqlite_foreign_keys(engine)
        return engine
    return create_async_engine(
        target,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        future=True,
    )


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Turn on SQLite foreign-key enforcement.

    SQLite ignores foreign keys unless asked, which lets a schema mistake --
    writing a ``run_id`` that does not exist, say -- pass the entire local test
    suite and then fail on the first real crawl against PostgreSQL. Enabling the
    pragma makes the local database behave like the production one.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def create_sessionmaker(engine: AsyncEngine) -> SessionMaker:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(sessionmaker: SessionMaker) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on error."""
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()
    _log.debug("db.engine_disposed")


def session_dialect(session: AsyncSession) -> str:
    """Dialect name a session is bound to.

    Search and upsert SQL differ between PostgreSQL and SQLite, so the
    repositories need this. Read from the underlying sync session because
    ``AsyncSession.get_bind`` is not a coroutine in SQLAlchemy 2.1.
    """
    bind = session.sync_session.get_bind()
    return bind.dialect.name if bind is not None else "postgresql"
