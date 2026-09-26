"""Record a live job board into a fixture bundle.

Fixture bundles are what let the test suite and CI run the real crawler, the real
parsers and the real pipeline with no network. This script creates and refreshes
one from a live board.

    python scripts/record_fixtures.py --config my_board.yml --out tests/fixtures/my_board

Before pointing this at a real site, read its robots.txt and terms of service,
and pick a ``per_domain_rate`` the site can absorb. Recorded pages stay local and
are never committed by accident (they are, but only from sites you are allowed
to crawl -- ``.gitignore`` this directory if that is a concern for your repo).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobscout.adapters.htmlboard import HtmlBoardAdapter
from jobscout.config import Settings
from jobscout.fetch import HttpFetcher
from jobscout.fetch.snapshot import NullSnapshotStore
from jobscout.models import Emitted, FetchedPage, Follow
from jobscout.observability import configure_logging
from jobscout.parse.board import BoardConfig

USER_AGENT = "JobScoutRecorder/0.1 (+set JOBSCOUT_USER_AGENT with your contact address)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="board selector YAML")
    parser.add_argument("--out", type=Path, required=True, help="fixture bundle directory")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--rate", type=float, default=0.5, help="requests/second (be gentle)")
    return parser


class RecordingAdapter(HtmlBoardAdapter):
    """Crawls normally but remembers every page it saw."""

    def __init__(self, config: BoardConfig) -> None:
        super().__init__(config)
        self.recorded: dict[str, tuple[str, str]] = {}  # url -> (filename, kind)

    async def start(self) -> list[Follow]:
        return list(await super().start())

    def parse(self, page: FetchedPage, follow: Follow) -> list[Follow | Emitted]:
        self.recorded[page.url] = (f"page-{len(self.recorded) + 1:04d}.html", follow.kind)
        return list(super().parse(page, follow))


async def _crawl_and_collect(
    config: BoardConfig, out_dir: Path, max_pages: int, rate: float
) -> dict[str, tuple[str, str]]:
    """Crawl the board, returning ``{url: (filename, kind)}``.

    Bodies are written to disk once the crawl is finished, so the event loop is
    never blocked on file I/O between requests.
    """
    adapter = RecordingAdapter(config)

    settings = Settings(
        user_agent=USER_AGENT,
        per_domain_rate=rate,
        per_domain_concurrency=1,
        max_pages=max_pages,
        store_snapshots=False,  # the bundle itself is the archive here
    )

    frontier: list[tuple[Follow, int]] = [(f, 0) for f in await adapter.start()]
    seen: set[str] = set()
    bodies: dict[str, str] = {}

    async with HttpFetcher(
        settings, adapter=config.name, snapshot_store=NullSnapshotStore()
    ) as fetcher:
        while frontier and len(seen) < max_pages:
            follow, depth = frontier.pop(0)
            if follow.url in seen or depth > 2:
                continue
            seen.add(follow.url)
            try:
                page = await fetcher.fetch(follow.url, adapter=config.name, kind=follow.kind)
            except Exception as exc:
                print(f"  skip  {follow.url}: {exc}", file=sys.stderr)
                continue

            filename = f"page-{len(seen):04d}.html"
            adapter.recorded[page.url] = (filename, follow.kind)
            bodies[page.url] = page.text

            for output in adapter.parse(page, follow):
                if isinstance(output, Follow):
                    frontier.append((output, depth + 1))

    for url, (filename, _kind) in adapter.recorded.items():
        (out_dir / filename).write_text(bodies[url], encoding="utf-8")

    _write_manifest(config, out_dir, adapter)
    return adapter.recorded


def record(config_path: Path, out_dir: Path, max_pages: int, rate: float) -> int:
    """Record a live board into a fixture bundle (blocking entrypoint)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    config = BoardConfig.from_yaml(config_path)
    recorded = asyncio.run(_crawl_and_collect(config, out_dir, max_pages, rate))
    return len(recorded)


def _write_manifest(config: BoardConfig, out_dir: Path, adapter: RecordingAdapter) -> None:
    """Write ``manifest.json`` and copy the selector config into the bundle."""
    pages: list[dict[str, Any]] = []
    for url, (filename, kind) in adapter.recorded.items():
        pages.append(
            {
                "url": url,
                "file": filename,
                "kind": kind,
                "base_url": config.resolved_base_url,
            }
        )

    manifest = {
        "config": "board.yml",
        "pages": pages,
        "notes": f"Recorded from {config.start_urls[0]} on demand. Verify permission to crawl.",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    import yaml

    (out_dir / "board.yml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )


def main() -> int:
    args = build_parser().parse_args()
    configure_logging("INFO", "console")

    count = record(args.config, args.out, args.max_pages, args.rate)
    print(f"recorded {count} page(s) into {args.out}")
    print("review the bundle, then run: pytest tests/ -k fixture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
