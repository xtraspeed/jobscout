"""Shared fixtures.

Two rules the whole suite follows:

* every database is a throwaway SQLite file, never a shared or persistent one;
* no test touches the network unless it is explicitly marked ``network``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.adapters.fixture import FixtureAdapter, FixtureBundle
from jobscout.api.main import create_app
from jobscout.config import Settings
from jobscout.fetch.transport import fixture_transport, json_transport
from jobscout.observability import configure_logging
from jobscout.parse.hn import HN_ITEM_BASE, HN_SEARCH_API
from jobscout.store.db import create_engine, create_sessionmaker
from jobscout.store.models import Base
from jobscout.store.repositories import JobRepository

FIXTURES = Path(__file__).parent / "fixtures"
DEMO_BOARD = FIXTURES / "demo_board"

configure_logging("WARNING", "console")


@pytest.fixture
def fixture_dir() -> Path:
    return DEMO_BOARD


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Fast, offline settings: high rate, no snapshots, temp SQLite."""
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'jobscout.db').as_posix()}",
        respect_robots=True,
        per_domain_rate=1000.0,
        per_domain_concurrency=8,
        global_concurrency=8,
        request_timeout=5.0,
        connect_timeout=5.0,
        max_retries=0,
        backoff_base=0.001,
        backoff_cap=0.01,
        store_snapshots=True,
        max_depth=3,
        max_pages=50,
        log_level="WARNING",
        log_format="console",
        metrics_enabled=True,
    )


@pytest.fixture
def board_bundle() -> FixtureBundle:
    return FixtureBundle(DEMO_BOARD)


@pytest.fixture
def board_adapter(board_bundle: FixtureBundle) -> FixtureAdapter:
    return FixtureAdapter(board_bundle)


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[Any]:
    """A migrated-from-scratch in-memory schema on a temp file."""
    eng = create_engine(settings)
    async with eng.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine: Any) -> Any:
    return create_sessionmaker(engine)


@pytest_asyncio.fixture
async def session(sessionmaker: Any) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as open_session:
        yield open_session
        await open_session.rollback()


@pytest_asyncio.fixture
async def repo(session: AsyncSession) -> JobRepository:
    return JobRepository(session, dialect="sqlite")


@pytest_asyncio.fixture
async def api_client(settings: Settings, sessionmaker: Any) -> AsyncIterator[AsyncClient]:
    """In-process API client with the engine injected (no lifespan surprises)."""
    app = create_app(settings)
    app.state.sessionmaker = sessionmaker
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://api.test") as client:
        yield client


@pytest.fixture
def board_transport(board_adapter: FixtureAdapter) -> Any:
    """httpx transport that answers from the recorded fixture bundle."""

    def resolve(url: str) -> tuple[int, str, str] | None:
        page = board_adapter.page_for(url)
        if page.status_code == 404:
            return None
        return page.status_code, page.text, "text/html; charset=utf-8"

    return fixture_transport(resolve)


@pytest.fixture
def hn_payloads() -> dict[str, Any]:
    """Recorded Hacker News API responses for the hnhiring adapter."""
    search = {
        "hits": [
            {
                "objectID": "40000001",
                "title": "Ask HN: Who is hiring? (March 2026)",
                "created_at_i": 1772323200,
            },
            {
                "objectID": "40000002",
                "title": "Ask HN: Who wants to be hired? (March 2026)",
                "created_at_i": 1772323200,
            },
            {"objectID": "40000003", "title": "Some unrelated story", "created_at_i": 1772323200},
        ]
    }
    thread = {"id": 40000001, "time": 1772323200, "kids": [41000001, 41000002, 41000003]}
    return {
        "search": search,
        "thread": thread,
        "comments": {
            "41000001": {
                "id": 41000001,
                "parent_id": 40000001,
                "text": (FIXTURES / "hn_comment_1.html").read_text(encoding="utf-8"),
                "kids": [41000101],
            },
            "41000002": {
                "id": 41000002,
                "parent_id": 40000001,
                "text": (FIXTURES / "hn_comment_2.html").read_text(encoding="utf-8"),
            },
            "41000003": {
                "id": 41000003,
                "parent_id": 40000001,
                "text": (FIXTURES / "hn_comment_not_a_posting.html").read_text(encoding="utf-8"),
            },
        },
    }


@pytest.fixture
def hn_json_transport(hn_payloads: dict[str, Any]) -> Any:
    """Transport serving the recorded HN API payloads.

    Keyed by URL path, so the adapter's query parameters do not have to be
    restated in the fixture.
    """
    thread_id = hn_payloads["thread"]["id"]
    documents: dict[str, Any] = {
        HN_SEARCH_API: hn_payloads["search"],
        f"{HN_ITEM_BASE}/{thread_id}.json": hn_payloads["thread"],
    }
    for comment_id, payload in hn_payloads["comments"].items():
        documents[f"{HN_ITEM_BASE}/{comment_id}.json"] = payload

    return json_transport(documents)
