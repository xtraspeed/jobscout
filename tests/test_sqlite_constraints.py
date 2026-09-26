"""Regression test for SQLite foreign-key enforcement.

A production bug shipped because the local test database silently ignored
foreign keys: the crawler wrote a placeholder ``run_id`` of 0, which PostgreSQL
rejected and SQLite happily accepted. The whole suite was green locally.

These tests assert the pragma is actually on, so the local database keeps
behaving like the production one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.store.models import JobItemRow


@pytest.mark.asyncio
async def test_sqlite_enforces_foreign_keys(session: AsyncSession) -> None:
    """A run_id that does not exist must be rejected, not silently stored."""
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO job_items (run_id, source, external_id, url, title, company, "
                "description, tags, raw, search_text, content_hash, remote, is_active, "
                "first_seen_at, last_seen_at) "
                "VALUES (999, 's', 'fk', 'u', 'T', 'C', '', '[]', '{}', 'T', 'h', 0, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_sqlite_allows_a_null_run_id(session: AsyncSession) -> None:
    """run_id is nullable, so writing items outside a run must still work."""
    await session.execute(
        text(
            "INSERT INTO job_items (run_id, source, external_id, url, title, company, "
            "description, tags, raw, search_text, content_hash, remote, is_active, "
            "first_seen_at, last_seen_at) "
            "VALUES (NULL, 's', '1', 'u', 'T', 'C', '', '[]', '{}', 'T', 'h', 0, 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    await session.commit()

    row = await session.get(JobItemRow, 1)
    assert row is not None
    assert row.run_id is None


@pytest.mark.asyncio
async def test_foreign_keys_pragma_is_on(session: AsyncSession) -> None:
    """Assert the pragma directly, so the behavioural test above has a cause."""
    result = await session.execute(text("PRAGMA foreign_keys"))
    assert result.scalar_one() == 1
