"""Domain models shared by adapters, the pipeline, the store and the API."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    """Timezone-aware current time (kept in one place for test patching)."""
    return datetime.now(UTC)


def stable_hash(payload: Any) -> str:
    """Deterministic hash of a JSON-serialisable payload.

    Used for change detection: two scrapes of the same listing produce the same
    hash, so re-crawling only writes rows whose content actually moved.
    """
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class JobItem(BaseModel):
    """A normalised public job listing."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source: str = Field(description="adapter name that produced the item")
    external_id: str = Field(description="stable id within the source")
    url: str
    title: str
    company: str
    location: str | None = None
    remote: bool = False
    employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    posted_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("posted_at")
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_validator("tags")
    @classmethod
    def _dedupe_tags(cls, value: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for tag in value:
            cleaned = tag.strip()
            if cleaned:
                seen.setdefault(cleaned, None)
        return list(seen)

    @property
    def search_text(self) -> str:
        """Denormalised text used for full-text search and content hashing."""
        parts = [
            self.title,
            self.company,
            self.location or "",
            self.employment_type or "",
            " ".join(self.tags),
            self.description,
        ]
        return " ".join(part for part in parts if part).strip()

    @property
    def content_hash(self) -> str:
        """Hash over user-visible fields only; ``raw``/timestamps excluded."""
        return stable_hash(
            {
                "title": self.title,
                "company": self.company,
                "location": self.location,
                "remote": self.remote,
                "employment_type": self.employment_type,
                "salary_min": self.salary_min,
                "salary_max": self.salary_max,
                "salary_currency": self.salary_currency,
                "description": self.description,
                "tags": sorted(self.tags),
            }
        )

    @property
    def key(self) -> str:
        """Natural key for upserts: ``(source, external_id)``."""
        return f"{self.source}:{self.external_id}"


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """An HTTP response as the crawler sees it."""

    url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    content_hash: str = ""
    fetched_at: datetime = field(default_factory=utcnow)
    elapsed: float = 0.0
    from_snapshot: bool = False
    kind: str = "html"
    """What the adapter asked for (``list``, ``detail``, ``json``, ...).

    Archived alongside the body so an offline re-parse can hand the page back to
    the adapter with the same context it had during the crawl.
    """

    @classmethod
    def build(
        cls,
        *,
        url: str,
        status_code: int,
        text: str,
        headers: dict[str, str] | None = None,
        elapsed: float = 0.0,
        kind: str = "html",
    ) -> FetchedPage:
        return cls(
            url=url,
            status_code=status_code,
            text=text,
            headers={k.lower(): v for k, v in (headers or {}).items()},
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            elapsed=elapsed,
            kind=kind,
        )

    def json(self) -> Any:
        """Parse the body as JSON (used by API-backed adapters)."""
        return json.loads(self.text)

    @property
    def is_html(self) -> bool:
        content_type = self.headers.get("content-type", "")
        return "html" in content_type or content_type == ""


@dataclass(frozen=True, slots=True)
class Follow:
    """A URL the crawl should visit next, discovered by an adapter."""

    url: str
    kind: str = "html"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Emitted:
    """A normalised listing produced by an adapter from a fetched page."""

    item: JobItem
    source_url: str
