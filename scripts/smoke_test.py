"""End-to-end smoke test.

Verifies the whole product from an empty database: crawl, store, re-parse
idempotency, and (if the API is running) the query endpoints.

    python scripts/smoke_test.py                     # offline, scratch database
    python scripts/smoke_test.py --with-api          # also probe a running API
    python scripts/smoke_test.py --api-url http://localhost:8000

The script refuses to run against anything other than a scratch SQLite database
whose filename contains "smoke", so it can never truncate real data.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobscout.config import Settings
from jobscout.fetch.transport import fixture_transport
from jobscout.observability import configure_logging
from jobscout.pipeline import reparse_snapshots, run_crawl

FIXTURE_BUNDLE = ROOT / "tests" / "fixtures" / "demo_board"
SCRATCH_DB = ROOT / "var" / "smoke.db"

_passed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}", file=sys.stderr)
        raise SystemExit(1)


def step(title: str) -> None:
    print(f"\n{title}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument(
        "--with-api",
        action="store_true",
        help="fail if the API is not reachable (default: skip when it is down)",
    )
    return parser


def prepare_scratch_database() -> str:
    """Delete and re-migrate the scratch database, refusing anything else."""
    url = f"sqlite+aiosqlite:///{SCRATCH_DB.as_posix()}"
    if "smoke" not in SCRATCH_DB.name:  # pragma: no cover - constant guard
        raise SystemExit("refusing to touch a database that is not clearly a scratch file")
    if "sqlite" not in url:
        raise SystemExit("refusing to run the smoke test against a non-sqlite database")

    SCRATCH_DB.parent.mkdir(parents=True, exist_ok=True)
    if SCRATCH_DB.exists():
        print(f"  resetting scratch database {SCRATCH_DB.name}")
        SCRATCH_DB.unlink()

    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{SCRATCH_DB.as_posix()}")
    command.upgrade(config, "head")
    return url


def main() -> int:
    args = build_parser().parse_args()
    configure_logging("WARNING", "console")
    started = time.perf_counter()

    step("0/5 prepare scratch database")
    url = prepare_scratch_database()
    settings = Settings(
        database_url=url,
        per_domain_rate=1000.0,
        per_domain_concurrency=8,
        max_retries=0,
        log_level="WARNING",
        log_format="console",
    )
    check("schema migrated", SCRATCH_DB.exists())

    from jobscout.adapters.fixture import FixtureAdapter, FixtureBundle

    adapter = FixtureAdapter(FixtureBundle(FIXTURE_BUNDLE))

    def resolve(url: str) -> tuple[int, str, str] | None:
        page = adapter.page_for(url)
        if page.status_code == 404:
            return None
        return page.status_code, page.text, "text/html"

    step("1/5 crawl (offline, recorded fixtures)")
    result = asyncio.run(run_crawl(settings, adapter, transport=fixture_transport(resolve)))
    check("crawl succeeded", result.status == "succeeded", result.status)
    check("pages fetched", result.pages_fetched == 6, str(result.pages_fetched))
    check("items collected", result.writes.new == 4, str(result.writes.new))
    print(f"        {result.summary()}")

    step("2/5 re-crawl is idempotent")
    second = asyncio.run(run_crawl(settings, adapter, transport=fixture_transport(resolve)))
    check("no new rows", second.writes.new == 0, str(second.writes.new))
    check("no changed rows", second.writes.changed == 0, str(second.writes.changed))
    check("all unchanged", second.writes.unchanged == 4, str(second.writes.unchanged))

    step("3/5 offline re-parse from the archive")
    report = asyncio.run(reparse_snapshots(settings, adapter))
    check("archive replayed", report.parsed == 6, str(report.parsed))
    check("re-parse changed nothing", report.writes.changed == 0, str(report.writes.changed))
    check("re-parse added nothing", report.writes.new == 0, str(report.writes.new))

    step("4/5 query API")
    if not api_is_up(args.api_url):
        if args.with_api:
            check("API reachable", False, args.api_url)
        else:
            print(f"  SKIP  no API at {args.api_url} (start it with: make api)")
    else:
        check_api(args.api_url)

    step("5/5 summary")
    print(f"  {_passed} checks passed in {time.perf_counter() - started:.1f}s")
    return 0


def api_is_up(api_url: str) -> bool:
    try:
        return httpx.get(f"{api_url}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def check_api(api_url: str) -> None:
    items = httpx.get(f"{api_url}/items", params={"limit": 5}, timeout=5.0).json()
    check("API returns listings", items["count"] == 4, str(items["count"]))

    remote = httpx.get(f"{api_url}/items", params={"remote": True}, timeout=5.0).json()
    check("remote filter works", all(row["remote"] for row in remote["items"]))
    check("remote matched some", remote["count"] == 2, str(remote["count"]))

    search = httpx.get(f"{api_url}/items", params={"q": "ingestion"}, timeout=5.0).json()
    check("full-text search narrows results", search["count"] == 1, str(search["count"]))
    check(
        "search hit the right listing",
        search["items"][0]["company"] == "Northwind Analytics",
        search["items"][0]["company"],
    )

    broad = httpx.get(f"{api_url}/items", params={"q": "engineer"}, timeout=5.0).json()
    check("broad search still matches", broad["count"] >= 1, str(broad["count"]))

    stats = httpx.get(f"{api_url}/stats", timeout=5.0).json()
    check("stats totals", stats["total"] == 4, str(stats["total"]))
    check("daily series is dense", len(stats["daily"]) == 30, str(len(stats["daily"])))

    companies = httpx.get(f"{api_url}/companies", timeout=5.0).json()
    check("companies rollup", len(companies) == 3, str(len(companies)))

    runs = httpx.get(f"{api_url}/runs", timeout=5.0).json()
    check("runs recorded", len(runs) >= 3, str(len(runs)))

    ready = httpx.get(f"{api_url}/ready", timeout=5.0).json()
    check("readiness reports the database", ready["database"] == "ok", str(ready))

    metrics = httpx.get(f"{api_url}/metrics", timeout=5.0).text
    check("metrics exposed", "jobscout_api_requests_total" in metrics)

    detail_id = items["items"][0]["id"]
    detail = httpx.get(f"{api_url}/items/{detail_id}", timeout=5.0).json()
    check("detail includes a description", bool(detail.get("description")))
    check("detail includes the source url", detail["url"].startswith("https://demo-board.example"))

    missing = httpx.get(f"{api_url}/items/999999", timeout=5.0)
    check("missing listing is 404", missing.status_code == 404, str(missing.status_code))


if __name__ == "__main__":
    raise SystemExit(main())
