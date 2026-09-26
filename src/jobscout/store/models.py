"""SQLAlchemy table definitions.

Deliberately portable: only column types that behave identically on PostgreSQL
and SQLite are used, so the entire test suite runs on a temp SQLite file while
production runs on PostgreSQL. Anything PostgreSQL-specific (the GIN full-text
index) lives in the Alembic migration behind a dialect check.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for every table."""


class TimestampMixin:
    """Convenience accessors for stored timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class CrawlRun(Base, TimestampMixin):
    """One execution of one adapter, with its counters and outcome."""

    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_changed: Mapped[int] = mapped_column(Integer, default=0)
    items_unchanged: Mapped[int] = mapped_column(Integer, default=0)

    max_depth_reached: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    items: Mapped[list[JobItemRow]] = relationship(back_populates="run")

    __table_args__ = (Index("ix_crawl_runs_started_status", "started_at", "status"),)


class JobItemRow(Base):
    """A normalised public job listing. The product of the whole pipeline."""

    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL"), index=True
    )

    # Natural key: (source, external_id) is unique and is what upserts target.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    location: Mapped[str | None] = mapped_column(String(512), index=True)
    remote: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    employment_type: Mapped[str | None] = mapped_column(String(64), index=True)

    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(8))

    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    search_text: Mapped[str] = mapped_column(Text, default="")

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    run: Mapped[CrawlRun | None] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_job_items_source_external"),
        Index("ix_job_items_company_remote", "company", "remote"),
        Index("ix_job_items_posted_active", "posted_at", "is_active"),
    )


class RawPage(Base):
    """Gzipped response archive; the input to offline re-parsing."""

    __tablename__ = "raw_pages"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    kind: Mapped[str] = mapped_column(String(32), default="html")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    body_gz: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True
    )

    __table_args__ = (Index("ix_raw_pages_url_fetched", "url", "fetched_at"),)


class FetchError(Base):
    """Failed requests, kept for debugging rather than only in logs."""

    __tablename__ = "fetch_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
