"""Persistence: SQLAlchemy models, engine/session helpers and repositories."""

from __future__ import annotations

from jobscout.store.db import (
    SessionMaker,
    create_engine,
    create_sessionmaker,
    dispose_engine,
    session_dialect,
    session_scope,
)
from jobscout.store.models import Base, CrawlRun, FetchError, JobItemRow, RawPage
from jobscout.store.repositories import (
    ErrorRepository,
    ItemFilters,
    ItemPage,
    JobRepository,
    RunRepository,
    WriteStats,
    decode_cursor,
    encode_cursor,
)
from jobscout.store.snapshots import DbSnapshotStore, prune_snapshots

__all__ = [
    "Base",
    "CrawlRun",
    "DbSnapshotStore",
    "ErrorRepository",
    "FetchError",
    "ItemFilters",
    "ItemPage",
    "JobItemRow",
    "JobRepository",
    "RawPage",
    "RunRepository",
    "SessionMaker",
    "WriteStats",
    "create_engine",
    "create_sessionmaker",
    "decode_cursor",
    "dispose_engine",
    "encode_cursor",
    "prune_snapshots",
    "session_dialect",
    "session_scope",
]
