"""Schema drift detection.

The ORM models and the Alembic migrations are two declarations of the same
schema, and nothing forces them to agree. A migration that creates a slightly
different table produces errors far away from the cause -- a NOT NULL id column
that never auto-assigns, a missing column only one query reads -- so the two are
compared directly here.

This test runs the real migrations against a temporary SQLite database, so it
needs no PostgreSQL and no fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from jobscout.store.models import Base

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A SQLite database created purely by running the migrations."""
    db_path = tmp_path_factory.mktemp("migrations") / "migrated.db"
    url = f"sqlite:///{db_path.as_posix()}"

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    # The sync URL is derived from JOBSCOUT_DATABASE_URL inside env.py.
    command.upgrade(config, "head")
    return url


def test_migrations_create_every_table(migrated_db: str) -> None:
    from sqlalchemy import create_engine

    tables = set(inspect(create_engine(migrated_db)).get_table_names())

    assert set(Base.metadata.tables) <= tables


def test_migrated_schema_matches_orm_metadata(migrated_db: str) -> None:
    from sqlalchemy import create_engine

    inspector = inspect(create_engine(migrated_db))

    for name, table in Base.metadata.tables.items():
        migrated = {col["name"] for col in inspector.get_columns(name)}
        declared = {col.name for col in table.columns}
        assert migrated == declared, f"column drift in {name}: {declared ^ migrated}"


def test_primary_keys_match(migrated_db: str) -> None:
    from sqlalchemy import create_engine

    inspector = inspect(create_engine(migrated_db))

    for name, table in Base.metadata.tables.items():
        constraint = inspector.get_pk_constraint(name)
        migrated = set(constraint["constrained_columns"])
        declared = {col.name for col in table.primary_key.columns}
        assert migrated == declared, f"primary key drift in {name}"


def test_unique_constraints_match(migrated_db: str) -> None:
    from sqlalchemy import create_engine

    inspector = inspect(create_engine(migrated_db))
    migrated = {
        tuple(sorted(constraint["column_names"]))
        for constraint in inspector.get_unique_constraints("job_items")
    }
    assert ("external_id", "source") in migrated, "the natural key must stay unique"


def test_indexes_match(migrated_db: str) -> None:
    from sqlalchemy import create_engine

    inspector = inspect(create_engine(migrated_db))

    for name, table in Base.metadata.tables.items():
        declared = {index.name for index in table.indexes}
        created = {index["name"] for index in inspector.get_indexes(name)}
        # SQLite may omit an index that backs a UNIQUE constraint.
        assert declared <= created, f"missing indexes on {name}: {declared - created}"


def test_sqlite_ids_auto_increment(migrated_db: str) -> None:
    """Only an INTEGER PRIMARY KEY is a rowid alias on SQLite.

    A migration that declared a plain BIGINT id would pass every schema-shape
    assertion and then fail on the first insert, which is exactly the kind of
    drift this test exists to catch.
    """
    from sqlalchemy import create_engine, text

    insert = text(
        "INSERT INTO raw_pages (url, status_code, adapter, kind, content_hash, "
        "size_bytes, headers, body_gz, fetched_at) "
        "VALUES (:url, 200, 'test', 'html', 'hash', 0, '{}', X'00', '2026-01-01')"
    )

    engine = create_engine(migrated_db)
    with engine.begin() as connection:
        connection.execute(insert, {"url": "https://x.test"})
        connection.execute(insert, {"url": "https://y.test"})
        ids = [row[0] for row in connection.execute(text("SELECT id FROM raw_pages ORDER BY url"))]

    assert ids == [1, 2], "SQLite must assign rowids automatically"


def test_alembic_version_is_stamped(migrated_db: str) -> None:
    from sqlalchemy import create_engine, text

    with create_engine(migrated_db).connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0001_initial"


def test_downgrade_then_upgrade_round_trips(migrated_db: str, tmp_path: Path) -> None:
    """A migration that cannot be reversed is a migration you cannot deploy."""
    db_path = tmp_path / "roundtrip.db"
    url = f"sqlite:///{db_path.as_posix()}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    from sqlalchemy import create_engine

    tables = set(inspect(create_engine(url)).get_table_names())
    assert "job_items" in tables
