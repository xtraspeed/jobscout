"""Response models for the query API.

Kept separate from the SQLAlchemy rows so the wire format is an explicit
contract: internal columns (``search_text``, ``content_hash``, ``raw``) are
never exposed by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ItemSummary(BaseModel):
    """A listing as it appears in list views."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    title: str
    company: str
    location: str | None = None
    remote: bool = False
    employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    posted_at: datetime | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool = True
    tags: list[str] = Field(default_factory=list)


class ItemDetail(ItemSummary):
    """A listing with its full description and provenance."""

    description: str = ""
    url: str
    external_id: str
    content_hash: str
    run_id: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ItemPage(BaseModel):
    """A keyset-paginated slice of listings."""

    items: list[ItemSummary]
    next_cursor: str | None = None
    count: int


class CompanySummary(BaseModel):
    company: str
    listings: int
    remote_listings: int
    latest_posted: datetime | None = None
    max_salary: float | None = None


class DailyPoint(BaseModel):
    day: str
    count: int


class Overview(BaseModel):
    """Headline numbers for the dashboard."""

    total: int
    active: int
    remote: int
    with_salary: int
    new_recent: int = Field(description="listings first seen inside the window")
    window_days: int = 30
    by_source: dict[str, int] = Field(default_factory=dict)
    daily: list[DailyPoint] = Field(default_factory=list)


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    adapter: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    pages_fetched: int = 0
    items_seen: int = 0
    items_new: int = 0
    items_changed: int = 0
    items_unchanged: int = 0
    failures: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


class RunDetail(RunSummary):
    errors: list[dict[str, Any]] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    database: str | None = None
    latest_run: datetime | None = None
