"""FastAPI query service.

Read-only over the dataset produced by the crawler. The API is the only thing
the dashboard talks to, which keeps a single owner for filtering, pagination and
full-text search.

Route labels used in metrics come from the *template* (``/items/{item_id}``), not
the raw path, so metric cardinality stays bounded.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.api.schemas import (
    CompanySummary,
    HealthResponse,
    ItemDetail,
    ItemPage,
    ItemSummary,
    Overview,
    RunDetail,
    RunSummary,
)
from jobscout.config import Settings, get_settings
from jobscout.observability import get_logger
from jobscout.observability.metrics import API_REQUESTS, CONTENT_TYPE, render_metrics
from jobscout.store.db import (
    SessionMaker,
    create_engine,
    create_sessionmaker,
    dispose_engine,
    session_dialect,
)
from jobscout.store.repositories import ItemFilters, JobRepository, RunRepository, SortField

_log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the engine for the app's lifetime.

    Tests override :func:`get_sessionmaker` and the engine, so nothing here
    reaches for a database unless the app is actually serving.
    """
    settings: Settings = app.state.settings
    engine = create_engine(settings)
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    _log.info("api.started", database_url=_redact(settings.database_url))
    try:
        yield
    finally:
        await dispose_engine(engine)
        _log.info("api.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory (the DI seam tests use)."""
    resolved = settings or get_settings()
    app = FastAPI(
        title=resolved.api_title,
        version="0.1.0",
        description=("Query API over publicly advertised job listings collected by JobScout."),
        lifespan=lifespan,
    )
    app.state.settings = resolved
    _register_routes(app)
    return app


# ------------------------------------------------------------- dependencies --


async def get_sessionmaker(request: Request) -> SessionMaker:
    sessionmaker: SessionMaker | None = getattr(request.app.state, "sessionmaker", None)
    if sessionmaker is None:  # pragma: no cover - only if lifespan did not run
        raise HTTPException(status_code=503, detail="database not initialised")
    return sessionmaker


async def get_session(
    sessionmaker: Annotated[SessionMaker, Depends(get_sessionmaker)],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        yield session


def get_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> JobRepository:
    """Build a repository bound to the session's dialect.

    Dialect is resolved up front because search and upsert SQL differ between
    PostgreSQL and SQLite.
    """
    return JobRepository(session, dialect=session_dialect(session))


# ------------------------------------------------------------------- routes ---


def _register_routes(app: FastAPI) -> None:
    settings: Settings = app.state.settings

    @app.middleware("http")
    async def count_requests(request: Request, call_next: Any) -> Response:
        """Label metrics with the route template to keep cardinality bounded."""
        started = time.perf_counter()
        response: Response = await call_next(request)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        API_REQUESTS.labels(route=route_path, status=str(response.status_code)).inc()
        response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response

    # -- health ------------------------------------------------------------

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    @app.get("/livez", response_model=HealthResponse, tags=["ops"])
    async def health() -> HealthResponse:
        """Liveness: no dependencies touched, so it cannot fail spuriously."""
        return HealthResponse(status="ok")

    @app.get("/ready", response_model=HealthResponse, tags=["ops"])
    @app.get("/readyz", response_model=HealthResponse, tags=["ops"])
    async def ready(
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HealthResponse:
        """Readiness: requires PostgreSQL, and reports crawl freshness."""
        try:
            await session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
        latest = await RunRepository(session).latest_successful()
        return HealthResponse(
            status="ok",
            database="ok",
            latest_run=latest.finished_at if latest else None,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """Prometheus exposition. Gate this at the ingress in production."""
        if not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="metrics disabled")
        return PlainTextResponse(render_metrics(), media_type=CONTENT_TYPE)

    # -- listings ----------------------------------------------------------

    @app.get("/items", response_model=ItemPage, tags=["listings"])
    async def list_items(
        repo: Annotated[JobRepository, Depends(get_repo)],
        q: Annotated[str | None, Query(description="full-text search")] = None,
        company: Annotated[list[str] | None, Query()] = None,
        location: Annotated[str | None, Query()] = None,
        remote: Annotated[bool | None, Query()] = None,
        employment_type: Annotated[str | None, Query()] = None,
        source: Annotated[str | None, Query()] = None,
        posted_after: Annotated[datetime | None, Query()] = None,
        min_salary: Annotated[float | None, Query(ge=0)] = None,
        has_salary: Annotated[bool | None, Query()] = None,
        include_inactive: Annotated[bool, Query()] = False,
        sort: Annotated[SortField, Query(description="sort column")] = "posted_at",
        desc: Annotated[bool, Query()] = True,
        limit: Annotated[int, Query(ge=1, le=200)] = 25,
        cursor: Annotated[str | None, Query(description="opaque keyset cursor")] = None,
    ) -> ItemPage:
        """Filtered, keyset-paginated listings."""
        filters = ItemFilters(
            q=q,
            companies=list(company or []),
            location=location,
            remote=remote,
            employment_type=employment_type,
            source=source,
            posted_after=posted_after,
            min_salary=min_salary,
            has_salary=has_salary,
            is_active=not include_inactive,
            sort=sort,
            descending=desc,
            limit=limit,
            cursor=cursor,
        )
        page = await repo.list_items(filters)
        return ItemPage(
            items=[ItemSummary.model_validate(row) for row in page.items],
            next_cursor=page.next_cursor,
            count=len(page.items),
        )

    @app.get("/items/{item_id}", response_model=ItemDetail, tags=["listings"])
    async def get_item(
        repo: Annotated[JobRepository, Depends(get_repo)],
        item_id: Annotated[int, Path(ge=1)],
    ) -> ItemDetail:
        row = await repo.get(item_id)
        if row is None:
            raise HTTPException(status_code=404, detail="item not found")
        return ItemDetail.model_validate(row)

    @app.get("/companies", response_model=list[CompanySummary], tags=["listings"])
    async def list_companies(
        repo: Annotated[JobRepository, Depends(get_repo)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        include_inactive: Annotated[bool, Query()] = False,
    ) -> list[CompanySummary]:
        rows = await repo.companies(limit=limit, is_active=not include_inactive)
        return [CompanySummary.model_validate(row) for row in rows]

    @app.get("/facets/{field_name}", response_model=list[str], tags=["listings"])
    async def list_facets(
        repo: Annotated[JobRepository, Depends(get_repo)],
        field_name: Annotated[str, Path(pattern="^(location|employment_type|source|company)$")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[str]:
        """Distinct values for a column, for populating filter controls."""
        return await repo.facets(field_name, limit=limit)

    @app.get("/stats", response_model=Overview, tags=["stats"])
    async def stats(
        repo: Annotated[JobRepository, Depends(get_repo)],
        days: Annotated[int, Query(ge=1, le=365)] = 30,
    ) -> Overview:
        return Overview.model_validate(await repo.overview(days=days))

    # -- crawl runs --------------------------------------------------------

    @app.get("/runs", response_model=list[RunSummary], tags=["runs"])
    async def list_runs(
        session: Annotated[AsyncSession, Depends(get_session)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[RunSummary]:
        runs = await RunRepository(session).list_recent(limit=limit)
        return [RunSummary.model_validate(run) for run in runs]

    @app.get("/runs/{run_id}", response_model=RunDetail, tags=["runs"])
    async def get_run(
        session: Annotated[AsyncSession, Depends(get_session)],
        run_id: Annotated[int, Path(ge=1)],
    ) -> RunDetail:
        repo = RunRepository(session)
        run = await repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        detail = RunDetail.model_validate(run)
        detail.errors = await repo.failure_breakdown(run_id)
        return detail

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.api_title,
            "docs": "/docs",
            "health": "/health",
            "metrics": "/metrics",
        }


def _redact(url: str) -> str:
    """Strip credentials from a database URL before logging it."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"
