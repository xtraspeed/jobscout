"""Dialect verification for the SQL that only ever runs on PostgreSQL.

The whole suite runs on SQLite, which means the PostgreSQL branch of the upsert,
the full-text search and the daily bucketing is never executed. Those statements
can still be verified without a server: SQLAlchemy compiles them for a target
dialect, so a syntax error, a misspelled function or a wrong ``excluded``
reference is caught here rather than in production.

These assertions are made against the same pure builders the repository uses, so
they break if the production SQL changes.

The behavioural counterpart -- the same statements against a live server -- lives
in ``tests/test_integration.py``, which needs PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from jobscout.models import JobItem
from jobscout.store.models import Base, JobItemRow
from jobscout.store.repositories import (
    ItemFilters,
    JobRepository,
    build_day_bucket,
    build_search_condition,
    build_upsert_statement,
    decode_cursor,
    encode_cursor,
)

PG = postgresql.dialect()
LITE = sqlite.dialect()
MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0001_initial.py"
).read_text(encoding="utf-8")


def make_item(external_id: str = "1", **overrides: object) -> JobItem:
    values: dict[str, object] = {
        "source": "board",
        "external_id": external_id,
        "url": f"https://board.test/jobs/{external_id}",
        "title": "Senior Backend Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
        "remote": False,
        "employment_type": "full-time",
        "salary_min": 85_000.0,
        "salary_max": 105_000.0,
        "salary_currency": "EUR",
        "description": "Own the ingestion pipeline. Python and Postgres.",
        "tags": ["Python", "PostgreSQL"],
        "raw": {"nested": {"a": 1}},
    }
    values.update(overrides)
    return JobItem(**values)  # type: ignore[arg-type]


def rows(*items: JobItem) -> list[dict[str, object]]:
    return [JobRepository._to_row(item, run_id=None, now=datetime.now(UTC)) for item in items]


def render(dialect: str, sql_dialect: object, *items: JobItem) -> str:
    return str(build_upsert_statement(dialect, rows(*items)).compile(dialect=sql_dialect))


# ------------------------------------------------------------------- upsert ---


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_upsert_targets_the_natural_key(dialect: str) -> None:
    sql = render(dialect, PG if dialect == "postgresql" else LITE, make_item())

    assert "ON CONFLICT (source, external_id) DO UPDATE" in sql


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_upsert_skips_unchanged_rows(dialect: str) -> None:
    """The idempotency guarantee, in SQL form.

    Without the conflict-action ``WHERE`` clause, every re-crawl would rewrite
    every row it sees.
    """
    sql = render(dialect, PG if dialect == "postgresql" else LITE, make_item())

    assert "job_items.content_hash != excluded.content_hash" in sql


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_upsert_never_moves_first_seen_at(dialect: str) -> None:
    sql = render(dialect, PG if dialect == "postgresql" else LITE, make_item())
    set_clause = sql.split("DO UPDATE SET", 1)[1]

    assert "last_seen_at" in set_clause
    assert "first_seen_at" not in set_clause


def test_upsert_requires_rows() -> None:
    with pytest.raises(ValueError):
        build_upsert_statement("postgresql", [])


# ------------------------------------------------------------------- search ---


def test_postgres_search_compiles_to_tsvector() -> None:
    compiled = build_search_condition(
        "postgresql", JobItemRow.search_text, "python engineer"
    ).compile(dialect=PG)
    sql = str(compiled)

    assert "to_tsvector" in sql
    assert "plainto_tsquery" in sql
    assert "@@" in sql
    assert "coalesce(job_items.search_text" in sql, "NULL search text must not break search"


def test_postgres_search_uses_the_simple_dictionary() -> None:
    """English stemming mangles job titles; 'simple' must not be swapped out.

    The configuration is a bind parameter, so it is asserted on the compiled
    parameters rather than the SQL text.
    """
    compiled = build_search_condition("postgresql", JobItemRow.search_text, "engineer").compile(
        dialect=PG
    )

    assert "simple" in compiled.params.values()
    assert "english" not in str(compiled.params).lower()


def test_sqlite_search_falls_back_to_like() -> None:
    compiled = build_search_condition("sqlite", JobItemRow.search_text, "python").compile(
        dialect=PG
    )
    sql = str(compiled)

    assert "to_tsvector" not in sql
    assert "LIKE" in sql.upper()


# ------------------------------------------------------------ daily buckets ---


def test_postgres_day_bucket_uses_date_trunc() -> None:
    assert "date_trunc" in str(
        build_day_bucket("postgresql", JobItemRow.posted_at).compile(dialect=PG)
    )


def test_sqlite_day_bucket_uses_strftime() -> None:
    assert "strftime" in str(build_day_bucket("sqlite", JobItemRow.posted_at).compile(dialect=LITE))


# -------------------------------------------------------------- keyset page ---


def test_keyset_predicate_compiles_on_both_dialects() -> None:
    repo = JobRepository.__new__(JobRepository)
    repo.dialect = "postgresql"
    decoded = decode_cursor(encode_cursor(datetime(2026, 2, 11, tzinfo=UTC), 42))
    assert decoded is not None

    condition = repo._keyset_condition(ItemFilters(sort="posted_at", descending=True), decoded)

    for sql_dialect in (PG, LITE):
        sql = str(condition.compile(dialect=sql_dialect))
        assert "OR" in sql, "keyset pagination needs an id tiebreaker or rows can repeat"


# ---------------------------------------------------------------- migration ---


def test_fts_index_is_created_only_on_postgres() -> None:
    """SQLite has no tsvector, so the GIN index must be dialect-guarded."""
    assert "to_tsvector('simple'" in MIGRATION
    assert "USING gin" in MIGRATION
    assert 'op.get_bind().dialect.name == "postgresql"' in MIGRATION
    assert "DROP INDEX IF EXISTS ix_job_items_search_fts" in MIGRATION


def test_raw_pages_id_is_a_rowid_alias_on_sqlite() -> None:
    """A plain BIGINT id passes every structural check, then fails on insert."""
    assert 'sa.BigInteger().with_variant(sa.Integer(), "sqlite")' in MIGRATION


def test_orm_uses_a_portable_json_type() -> None:
    """JSONB would break the SQLite suite, so the column must stay JSON."""
    json_columns = {
        "job_items": {"tags", "raw"},
        "crawl_runs": {"detail"},
        "raw_pages": {"headers"},
    }
    for table_name, columns in json_columns.items():
        table = Base.metadata.tables[table_name]
        for column in columns:
            assert type(table.columns[column].type).__name__ == "JSON", (
                f"{table_name}.{column} must be JSON, not JSONB"
            )


def test_natural_key_is_unique_in_the_migration() -> None:
    assert 'UniqueConstraint("source", "external_id"' in MIGRATION
