"""initial schema

Creates the four tables the crawler and query API depend on:

* ``crawl_runs``    - one row per adapter execution, with counters
* ``job_items``     - normalised listings, unique on (source, external_id)
* ``raw_pages``     - gzipped response archive for offline re-parsing
* ``fetch_errors``  - failed requests kept for debugging

The full-text search index is an *expression* GIN index over ``search_text``,
which keeps the table definition identical on PostgreSQL and SQLite while giving
PostgreSQL proper ``tsvector`` ranking. The SQLite path uses ``LIKE`` instead.

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crawl_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pages_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requests_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_depth_reached", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_crawl_runs_adapter", "crawl_runs", ["adapter"])
    op.create_index("ix_crawl_runs_started_status", "crawl_runs", ["started_at", "status"])

    op.create_table(
        "job_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("company", sa.String(length=512), nullable=False),
        sa.Column("location", sa.String(length=512), nullable=True),
        sa.Column("remote", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("employment_type", sa.String(length=64), nullable=True),
        sa.Column("salary_min", sa.Float(), nullable=True),
        sa.Column("salary_max", sa.Float(), nullable=True),
        sa.Column("salary_currency", sa.String(length=8), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["run_id"], ["crawl_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", name="uq_job_items_source_external"),
    )
    op.create_index("ix_job_items_run_id", "job_items", ["run_id"])
    op.create_index("ix_job_items_company", "job_items", ["company"])
    op.create_index("ix_job_items_location", "job_items", ["location"])
    op.create_index("ix_job_items_remote", "job_items", ["remote"])
    op.create_index("ix_job_items_employment_type", "job_items", ["employment_type"])
    op.create_index("ix_job_items_posted_at", "job_items", ["posted_at"])
    op.create_index("ix_job_items_content_hash", "job_items", ["content_hash"])
    op.create_index("ix_job_items_is_active", "job_items", ["is_active"])
    op.create_index("ix_job_items_company_remote", "job_items", ["company", "remote"])
    op.create_index("ix_job_items_posted_active", "job_items", ["posted_at", "is_active"])

    # Full-text search: an expression index keeps the schema portable while
    # giving PostgreSQL a real tsvector index.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_job_items_search_fts ON job_items "
            "USING gin (to_tsvector('simple', coalesce(search_text, '')))"
        )

    op.create_table(
        "raw_pages",
        # BIGINT on PostgreSQL, INTEGER on SQLite: only an INTEGER PRIMARY KEY is
        # a rowid alias, so a plain BIGINT would never auto-assign an id there.
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False, server_default="unknown"),
        # What the adapter asked for, so an offline re-parse can replay the page
        # with the same context (list vs detail vs json).
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="html"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("body_gz", sa.LargeBinary(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_raw_pages_adapter", "raw_pages", ["adapter"])
    op.create_index("ix_raw_pages_content_hash", "raw_pages", ["content_hash"])
    op.create_index("ix_raw_pages_fetched_at", "raw_pages", ["fetched_at"])
    op.create_index("ix_raw_pages_url_fetched", "raw_pages", ["url", "fetched_at"])

    op.create_table(
        "fetch_errors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["crawl_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fetch_errors_run_id", "fetch_errors", ["run_id"])


def downgrade() -> None:
    op.drop_table("fetch_errors")
    op.drop_table("raw_pages")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_job_items_search_fts")
    op.drop_table("job_items")
    op.drop_table("crawl_runs")
