"""Command-line interface.

::

    jobscout crawl    --target hnhiring --max-pages 200
    jobscout crawl    --target demo_board --config src/jobscout/selectors/demo_board.yml
    jobscout crawl    --target fixture --path tests/fixtures/board
    jobscout reparse  --target demo_board
    jobscout stats
    jobscout runs
    jobscout serve
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jobscout.adapters import available_adapters, board_config_path, get_adapter
from jobscout.config import Settings
from jobscout.errors import JobScoutError
from jobscout.fetch.transport import fixture_transport
from jobscout.observability import configure_logging, get_logger
from jobscout.pipeline import reparse_snapshots, run_crawl
from jobscout.store.db import (
    create_engine,
    create_sessionmaker,
    session_dialect,
    session_scope,
)
from jobscout.store.repositories import JobRepository, RunRepository

_log = get_logger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobscout",
        description="Async public job-listing scraper.",
    )
    parser.add_argument("--database-url", help="override JOBSCOUT_DATABASE_URL")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--log-format", choices=["json", "console"], default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="run a crawl")
    crawl.add_argument("--target", required=True, help=f"one of: {', '.join(available_adapters())}")
    crawl.add_argument("--config", type=Path, help="selector config for htmlboard targets")
    crawl.add_argument("--path", type=Path, help="fixture bundle directory")
    crawl.add_argument("--max-pages", type=int, default=None)
    crawl.add_argument("--threads", type=int, default=None, help="HN hiring threads to crawl")
    crawl.add_argument(
        "--allow-network",
        action="store_true",
        help="required for live targets; omitted means the run is refused",
    )
    crawl.add_argument("--json", action="store_true", help="print the result as JSON")

    reparse = sub.add_parser("reparse", help="re-parse archived pages without crawling")
    reparse.add_argument("--target", required=True)
    reparse.add_argument("--config", type=Path)
    reparse.add_argument("--path", type=Path, help="fixture bundle directory")
    reparse.add_argument("--limit", type=int, default=1000)

    sub.add_parser("stats", help="print dataset statistics")
    sub.add_parser("runs", help="print recent crawl runs")
    sub.add_parser("adapters", help="list available adapters")

    serve = sub.add_parser("serve", help="run the query API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if args.database_url:
        overrides["database_url"] = args.database_url
    if args.log_level:
        overrides["log_level"] = args.log_level
    if args.log_format:
        overrides["log_format"] = args.log_format
    return Settings(**overrides)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)
    configure_logging(settings.log_level, settings.log_format)

    try:
        return _dispatch(args, settings)
    except JobScoutError as exc:
        _log.error("cli.failed", command=args.command, error=str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    match args.command:
        case "crawl":
            return _cmd_crawl(args, settings)
        case "reparse":
            return _cmd_reparse(args, settings)
        case "stats":
            return _cmd_stats(settings)
        case "runs":
            return _cmd_runs(settings)
        case "adapters":
            print("\n".join(available_adapters()))
            return EXIT_OK
        case "serve":
            return _cmd_serve(args, settings)
        case _:  # pragma: no cover - argparse enforces the choices
            return EXIT_USAGE


# ------------------------------------------------------------------ commands --


def _cmd_crawl(args: argparse.Namespace, settings: Settings) -> int:
    adapter_kwargs: dict[str, Any] = {}
    if args.config is not None:
        adapter_kwargs["config_path"] = args.config
    elif args.target == "htmlboard":
        # Default to the bundled demo board so the command works with no args.
        adapter_kwargs["config_path"] = board_config_path("demo_board")
    if args.path is not None:
        adapter_kwargs["path"] = args.path
    if args.threads and args.target == "hnhiring":
        adapter_kwargs["threads"] = args.threads

    adapter = get_adapter(args.target, **adapter_kwargs)

    transport = None
    if args.target == "fixture":
        from jobscout.adapters.fixture import FixtureAdapter

        if not isinstance(adapter, FixtureAdapter):  # pragma: no cover - defensive
            raise JobScoutError("fixture target did not produce a fixture adapter")
        transport = fixture_transport(lambda url: _fixture_response(adapter, url))
    elif not args.allow_network:
        print(
            "refusing to hit the live internet without --allow-network.\n"
            "Live crawling is opt-in so a mistyped command cannot hammer a site.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    result = asyncio.run(
        run_crawl(settings, adapter, max_pages=args.max_pages, transport=transport)
    )

    if args.json:
        print(json.dumps(_result_payload(result), indent=2, default=str))
    else:
        print(result.summary())
        for error in result.errors[:5]:
            print(f"  - {error}")
    return EXIT_OK if result.status == "succeeded" else EXIT_ERROR


def _fixture_response(adapter: Any, url: str) -> tuple[int, str, str]:
    page = adapter.page_for(url)
    return page.status_code, page.text, page.headers.get("content-type", "text/html")


def _cmd_reparse(args: argparse.Namespace, settings: Settings) -> int:
    adapter_kwargs: dict[str, Any] = {}
    if args.config is not None:
        adapter_kwargs["config_path"] = args.config
    elif args.target == "htmlboard":
        adapter_kwargs["config_path"] = board_config_path("demo_board")
    if args.path is not None:
        adapter_kwargs["path"] = args.path
    adapter = get_adapter(args.target, **adapter_kwargs)

    report = asyncio.run(reparse_snapshots(settings, adapter, limit=args.limit))
    print(
        f"reparsed {report.parsed} archived page(s) in {report.duration_seconds:.2f}s | "
        f"new={report.writes.new} changed={report.writes.changed} "
        f"unchanged={report.writes.unchanged} inactive={report.marked_inactive}"
    )
    return EXIT_OK


def _cmd_stats(settings: Settings) -> int:
    async def run() -> dict[str, Any]:
        engine = create_engine(settings)
        try:
            async with session_scope(create_sessionmaker(engine)) as session:
                return await JobRepository(session, dialect=session_dialect(session)).overview()
        finally:
            await engine.dispose()

    print(json.dumps(asyncio.run(run()), indent=2, default=str))
    return EXIT_OK


def _cmd_runs(settings: Settings) -> int:
    async def run() -> list[dict[str, Any]]:
        engine = create_engine(settings)
        try:
            async with session_scope(create_sessionmaker(engine)) as session:
                runs = await RunRepository(session).list_recent(limit=20)
                return [
                    {
                        "id": item.id,
                        "adapter": item.adapter,
                        "status": item.status,
                        "started_at": item.started_at,
                        "new": item.items_new,
                        "changed": item.items_changed,
                        "failures": item.failures,
                    }
                    for item in runs
                ]
        finally:
            await engine.dispose()

    print(json.dumps(asyncio.run(run()), indent=2, default=str))
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    uvicorn.run(
        "jobscout.api.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return EXIT_OK


def _result_payload(result: Any) -> dict[str, Any]:
    return {
        "adapter": result.adapter,
        "run_id": result.run_id,
        "status": result.status,
        "duration_seconds": result.duration_seconds,
        **result.counters(),
        "errors": result.errors[:20],
    }


__all__ = ["build_parser", "main", "settings_from_args"]


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
