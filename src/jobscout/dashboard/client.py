"""Typed HTTP client for the JobScout query API.

The dashboard never touches PostgreSQL; it goes through the API. That keeps one
owner for filtering, search and pagination, and it means the dashboard works
against a remote deployment unchanged.

Caching is deliberate: Streamlit reruns the whole script on every widget change,
so an uncached call here would hit the API on each keystroke.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx


class ApiUnavailable(RuntimeError):
    """The API could not be reached."""


@dataclass(slots=True)
class Listing:
    """A row in the listings table."""

    id: int
    title: str
    company: str
    location: str | None
    remote: bool
    employment_type: str | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    posted_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    tags: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def salary_display(self) -> str:
        if self.salary_min is None and self.salary_max is None:
            return "—"
        currency = self.salary_currency or ""
        low = _money(self.salary_min)
        high = _money(self.salary_max)
        if low and high and low != high:
            return f"{currency} {low} – {high}".strip()
        return f"{currency} {low or high}".strip()

    @property
    def posted_display(self) -> str:
        return self.posted_at.date().isoformat() if self.posted_at else "—"

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Listing:
        return cls(
            id=data["id"],
            title=data["title"],
            company=data["company"],
            location=data.get("location"),
            remote=bool(data.get("remote")),
            employment_type=data.get("employment_type"),
            salary_min=data.get("salary_min"),
            salary_max=data.get("salary_max"),
            salary_currency=data.get("salary_currency"),
            posted_at=_parse_dt(data.get("posted_at")),
            first_seen_at=_parse_dt(data.get("first_seen_at")) or datetime.min,
            last_seen_at=_parse_dt(data.get("last_seen_at")) or datetime.min,
            tags=list(data.get("tags") or []),
            source=data.get("source", ""),
        )


@dataclass(slots=True)
class ListingPage:
    listings: list[Listing]
    next_cursor: str | None
    count: int


@dataclass(slots=True)
class ApiStatus:
    """What the sidebar health widget shows."""

    reachable: bool
    detail: str = ""
    latest_run: datetime | None = None


class JobScoutClient:
    """Small, synchronous client for the query API."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 10.0) -> None:
        configured = base_url or os.getenv("JOBSCOUT_DASHBOARD_API_URL") or "http://localhost:8000"
        self.base_url = configured.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JobScoutClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v not in (None, [], "")}
        try:
            response = self._client.get(path, params=clean)
        except httpx.HTTPError as exc:
            raise ApiUnavailable(f"cannot reach {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise ApiUnavailable(f"{path} -> HTTP {response.status_code}")
        return response.json()

    # -- endpoints ----------------------------------------------------------

    def status(self) -> ApiStatus:
        try:
            payload = self._get("/ready")
        except ApiUnavailable as exc:
            return ApiStatus(reachable=False, detail=str(exc))
        return ApiStatus(
            reachable=True,
            detail="ok",
            latest_run=_parse_dt(payload.get("latest_run")),
        )

    def listings(
        self,
        *,
        q: str | None = None,
        companies: list[str] | None = None,
        location: str | None = None,
        remote: bool | None = None,
        employment_type: str | None = None,
        source: str | None = None,
        min_salary: float | None = None,
        has_salary: bool | None = None,
        include_inactive: bool = False,
        sort: str = "posted_at",
        desc: bool = True,
        limit: int = 25,
        cursor: str | None = None,
    ) -> ListingPage:
        payload = self._get(
            "/items",
            {
                "q": q,
                "company": companies,
                "location": location,
                "remote": remote,
                "employment_type": employment_type,
                "source": source,
                "min_salary": min_salary,
                "has_salary": has_salary,
                "include_inactive": include_inactive,
                "sort": sort,
                "desc": desc,
                "limit": limit,
                "cursor": cursor,
            },
        )
        return ListingPage(
            listings=[Listing.from_payload(row) for row in payload.get("items", [])],
            next_cursor=payload.get("next_cursor"),
            count=int(payload.get("count", 0)),
        )

    def listing(self, item_id: int) -> dict[str, Any]:
        return dict(self._get(f"/items/{item_id}"))

    def companies(
        self, *, limit: int = 100, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        return list(self._get("/companies", {"limit": limit, "include_inactive": include_inactive}))

    def facets(self, field_name: str, *, limit: int = 50) -> list[str]:
        return list(self._get(f"/facets/{field_name}", {"limit": limit}))

    def stats(self, *, days: int = 30) -> dict[str, Any]:
        return dict(self._get("/stats", {"days": days}))

    def runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._get("/runs", {"limit": limit}))


def _money(value: float | None) -> str:
    if value is None:
        return ""
    if value >= 1000 and value % 1000 == 0:
        return f"{int(value / 1000)}k"
    return f"{value:,.0f}"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
