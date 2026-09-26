"""Query API contract tests, served in-process against a temp SQLite database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from jobscout.models import JobItem
from jobscout.store.repositories import ItemFilters, JobRepository, RunRepository

SOURCE = "test_board"
NOW = datetime.now(UTC)


def make_item(
    external_id: str,
    *,
    title: str = "Backend Engineer",
    company: str = "Acme",
    location: str = "Berlin, Germany",
    remote: bool = False,
    employment_type: str = "full-time",
    salary_max: float | None = 120_000.0,
    description: str = "Build ingestion pipelines with Python and Postgres.",
    days_ago: int = 0,
) -> JobItem:
    return JobItem(
        source=SOURCE,
        external_id=external_id,
        url=f"https://board.test/jobs/{external_id}",
        title=title,
        company=company,
        location=location,
        remote=remote,
        employment_type=employment_type,
        salary_min=salary_max,
        salary_max=salary_max,
        salary_currency="EUR",
        description=description,
        tags=["Python"],
        posted_at=NOW - timedelta(days=days_ago),
    )


async def seed(session: AsyncSession) -> None:
    repo = JobRepository(session, dialect="sqlite")
    await repo.upsert_many(
        [
            make_item("1", title="Rust Engineer", company="Acme", days_ago=0),
            make_item(
                "2",
                title="Frontend Engineer",
                company="Globex",
                location="Remote (EU)",
                remote=True,
                employment_type="contract",
                salary_max=90_000.0,
                days_ago=1,
            ),
            make_item(
                "3",
                title="Data Engineer",
                company="Acme",
                salary_max=None,
                days_ago=5,
            ),
        ]
    )
    await session.commit()


@pytest.fixture
async def seeded(api_client: AsyncClient, session: AsyncSession, sessionmaker: Any) -> AsyncClient:
    await seed(session)
    runs = RunRepository(session)
    run = await runs.start(SOURCE)
    await runs.finish(
        run,
        status="succeeded",
        counters={"items_new": 3, "pages_fetched": 4, "failures": 1},
        duration=1.5,
    )
    await session.commit()
    return api_client


# ----------------------------------------------------------------- liveness --


@pytest.mark.asyncio
async def test_health_is_dependency_free(api_client: AsyncClient) -> None:
    response = await api_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_livez_alias(api_client: AsyncClient) -> None:
    assert (await api_client.get("/livez")).status_code == 200


@pytest.mark.asyncio
async def test_ready_reports_database_and_last_run(seeded: AsyncClient) -> None:
    response = await seeded.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["database"] == "ok"
    assert payload["latest_run"] is not None


@pytest.mark.asyncio
async def test_root_advertises_endpoints(api_client: AsyncClient) -> None:
    payload = (await api_client.get("/")).json()
    assert payload["health"] == "/health"
    assert payload["docs"] == "/docs"


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_jobscout_series(seeded: AsyncClient) -> None:
    await seeded.get("/items")
    response = await seeded.get("/metrics")

    assert response.status_code == 200
    assert "jobscout_api_requests_total" in response.text


@pytest.mark.asyncio
async def test_metrics_can_be_disabled(settings: Any, sessionmaker: Any) -> None:
    from httpx import ASGITransport

    from jobscout.api.main import create_app

    app = create_app(settings.model_copy(update={"metrics_enabled": False}))
    app.state.sessionmaker = sessionmaker
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        assert (await client.get("/metrics")).status_code == 404


# ----------------------------------------------------------------- listings --


@pytest.mark.asyncio
async def test_list_items_returns_seeded_rows(seeded: AsyncClient) -> None:
    response = await seeded.get("/items")
    payload = response.json()

    assert response.status_code == 200
    assert payload["count"] == 3
    assert {row["company"] for row in payload["items"]} == {"Acme", "Globex"}


@pytest.mark.asyncio
async def test_item_summary_hides_internal_columns(seeded: AsyncClient) -> None:
    row = (await seeded.get("/items")).json()["items"][0]

    assert set(row) == {
        "id",
        "source",
        "title",
        "company",
        "location",
        "remote",
        "employment_type",
        "salary_min",
        "salary_max",
        "salary_currency",
        "posted_at",
        "first_seen_at",
        "last_seen_at",
        "is_active",
        "tags",
    }
    assert "content_hash" not in row
    assert "search_text" not in row
    assert "raw" not in row


@pytest.mark.asyncio
async def test_get_item_detail_includes_description_and_provenance(seeded: AsyncClient) -> None:
    item_id = (await seeded.get("/items")).json()["items"][0]["id"]

    payload = (await seeded.get(f"/items/{item_id}")).json()

    assert payload["description"]
    assert payload["url"].startswith("https://board.test/jobs/")
    assert len(payload["content_hash"]) == 64


@pytest.mark.asyncio
async def test_get_missing_item_is_404(seeded: AsyncClient) -> None:
    assert (await seeded.get("/items/999999")).status_code == 404


@pytest.mark.asyncio
async def test_filter_by_remote(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/items", params={"remote": True})).json()

    assert [row["company"] for row in payload["items"]] == ["Globex"]


@pytest.mark.asyncio
async def test_filter_by_company_repeats_as_or(seeded: AsyncClient) -> None:
    payload = (
        await seeded.get("/items", params=[("company", "Acme"), ("company", "Globex")])
    ).json()

    assert payload["count"] == 3


@pytest.mark.asyncio
async def test_filter_by_min_salary(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/items", params={"min_salary": 100_000})).json()

    assert [row["title"] for row in payload["items"]] == ["Rust Engineer"]


@pytest.mark.asyncio
async def test_filter_by_has_salary(seeded: AsyncClient) -> None:
    without = (await seeded.get("/items", params={"has_salary": False})).json()

    assert [row["title"] for row in without["items"]] == ["Data Engineer"]


@pytest.mark.asyncio
async def test_full_text_search(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/items", params={"q": "ingestion"})).json()

    assert payload["count"] == 3, "the description is part of the search text"

    narrow = (await seeded.get("/items", params={"q": "Globex"})).json()
    assert [row["title"] for row in narrow["items"]] == ["Frontend Engineer"]


@pytest.mark.asyncio
async def test_search_with_no_match(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/items", params={"q": "nonexistentrole"})).json()

    assert payload["items"] == []
    assert payload["next_cursor"] is None


@pytest.mark.asyncio
async def test_pagination_walks_the_whole_set(seeded: AsyncClient) -> None:
    seen: list[str] = []
    cursor: str | None = None

    for _ in range(10):
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        payload = (await seeded.get("/items", params=params)).json()
        seen.extend(row["id"] for row in payload["items"])
        cursor = payload["next_cursor"]
        if not cursor:
            break

    assert len(seen) == 3
    assert len(set(seen)) == 3


@pytest.mark.asyncio
async def test_limit_is_capped(seeded: AsyncClient) -> None:
    assert (await seeded.get("/items", params={"limit": 5000})).status_code == 422


@pytest.mark.asyncio
async def test_invalid_sort_is_rejected(seeded: AsyncClient) -> None:
    assert (await seeded.get("/items", params={"sort": "salary_min"})).status_code == 422


@pytest.mark.asyncio
async def test_posted_after_filter(seeded: AsyncClient) -> None:
    cutoff = (NOW - timedelta(days=2)).isoformat()
    payload = (await seeded.get("/items", params={"posted_after": cutoff})).json()

    assert payload["count"] == 2


# ---------------------------------------------------------------- aggregates --


@pytest.mark.asyncio
async def test_companies_rollup(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/companies")).json()

    acme = next(row for row in payload if row["company"] == "Acme")
    assert acme["listings"] == 2


@pytest.mark.asyncio
async def test_facets_endpoint(seeded: AsyncClient) -> None:
    assert (await seeded.get("/facets/employment_type")).json() == ["full-time", "contract"]


@pytest.mark.asyncio
async def test_facets_rejects_unknown_columns(seeded: AsyncClient) -> None:
    assert (await seeded.get("/facets/salary_min")).status_code == 422


@pytest.mark.asyncio
async def test_stats_overview(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/stats", params={"days": 30})).json()

    assert payload["total"] == 3
    assert payload["active"] == 3
    assert payload["remote"] == 1
    assert payload["with_salary"] == 2
    assert payload["window_days"] == 30
    assert len(payload["daily"]) == 30
    assert sum(point["count"] for point in payload["daily"]) == 3


# --------------------------------------------------------------------- runs --


@pytest.mark.asyncio
async def test_runs_listing(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/runs")).json()

    assert len(payload) == 1
    assert payload[0]["status"] == "succeeded"
    assert payload[0]["items_new"] == 3


@pytest.mark.asyncio
async def test_run_detail_includes_failure_breakdown(seeded: AsyncClient) -> None:
    run_id = (await seeded.get("/runs")).json()[0]["id"]

    payload = (await seeded.get(f"/runs/{run_id}")).json()

    assert payload["id"] == run_id
    assert isinstance(payload["errors"], list)


@pytest.mark.asyncio
async def test_missing_run_is_404(seeded: AsyncClient) -> None:
    assert (await seeded.get("/runs/4242")).status_code == 404


# ----------------------------------------------------------------- inactive --


@pytest.mark.asyncio
async def test_inactive_items_hidden_by_default(seeded: AsyncClient, session: AsyncSession) -> None:
    await JobRepository(session, dialect="sqlite").mark_stale(SOURCE, keep_ids=set())
    await session.commit()

    assert (await seeded.get("/items")).json()["count"] == 0
    assert (await seeded.get("/items", params={"include_inactive": True})).json()["count"] == 3


@pytest.mark.asyncio
async def test_count_matches_repository(
    seeded: AsyncClient, session: AsyncSession, sessionmaker: Any
) -> None:
    repo = JobRepository(session, dialect="sqlite")
    assert (await seeded.get("/items")).json()["count"] == await repo.count(ItemFilters())


@pytest.mark.asyncio
async def test_openapi_schema_is_served(seeded: AsyncClient) -> None:
    schema = (await seeded.get("/openapi.json")).json()

    assert "/items" in schema["paths"]
    assert "/stats" in schema["paths"]


@pytest.mark.asyncio
async def test_response_time_header_is_set(seeded: AsyncClient) -> None:
    response = await seeded.get("/items")
    assert "X-Response-Time-Ms" in response.headers


@pytest.mark.asyncio
async def test_internal_columns_never_leak_in_detail(
    seeded: AsyncClient, session: AsyncSession
) -> None:
    item_id = (await seeded.get("/items")).json()["items"][0]["id"]
    payload = (await seeded.get(f"/items/{item_id}")).json()

    assert "search_text" not in payload
    assert isinstance(payload["raw"], dict)
    assert isinstance(payload["tags"], list)


@pytest.mark.asyncio
async def test_listings_are_ordered_newest_first(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/items")).json()

    posted = [row["posted_at"] for row in payload["items"]]
    assert posted == sorted(posted, reverse=True)


@pytest.mark.asyncio
async def test_sort_by_company_ascending(seeded: AsyncClient) -> None:
    payload = (await seeded.get("/items", params={"sort": "company", "desc": False})).json()

    assert [row["company"] for row in payload["items"]] == ["Acme", "Acme", "Globex"]
