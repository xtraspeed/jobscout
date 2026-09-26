"""Offline replay adapter.

Serves recorded pages from disk instead of the network and runs them through
the *same* :class:`~jobscout.parse.board.JobBoardParser` the live adapter uses.
That means the whole crawl loop — frontier, politeness, dedupe, upsert, metrics —
is exercised in CI with zero external dependencies, and a recorded fixture
regression is a genuine parser regression.

A fixture bundle is a directory containing ``manifest.json``::

    {
      "config": "board.yml",
      "pages": [
        {"url": "https://board.test/jobs", "file": "list-1.html", "kind": "list"},
        {"url": "https://board.test/jobs/1", "file": "detail-1.html", "kind": "detail"}
      ]
    }

Unknown URLs return a 404 page, which keeps the crawler honest about missing
fixtures instead of silently succeeding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jobscout.adapters.htmlboard import KIND_LIST, HtmlBoardAdapter
from jobscout.models import FetchedPage
from jobscout.observability import get_logger
from jobscout.parse.board import BoardConfig

_log = get_logger(__name__)

MANIFEST_NAME: Final = "manifest.json"
NOT_FOUND_BODY: Final = "<html><body><h1>404 Not Found (fixture)</h1></body></html>"


@dataclass(frozen=True, slots=True)
class FixturePage:
    """One recorded page."""

    url: str
    file: str
    kind: str
    base_url: str | None = None
    seed: dict[str, Any] | None = None


class FixtureBundle:
    """A directory of recorded pages plus the board config they were parsed with."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        manifest_path = self.root / MANIFEST_NAME
        if not manifest_path.is_file():
            raise FileNotFoundError(f"no {MANIFEST_NAME} in {self.root}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.pages: dict[str, FixturePage] = {}
        for entry in manifest.get("pages", []):
            page = FixturePage(
                url=entry["url"],
                file=entry["file"],
                kind=entry.get("kind", KIND_LIST),
                base_url=entry.get("base_url"),
                seed=entry.get("seed"),
            )
            self.pages[page.url] = page
        self.config_path = self.root / manifest.get("config", "board.yml")
        if not self.config_path.is_file():
            raise FileNotFoundError(f"no board config at {self.config_path}")
        self.config = BoardConfig.from_yaml(self.config_path)
        #: In-memory body replacements, keyed by URL. Lets a test simulate a site
        #: redesign without mutating the recorded files on disk.
        self.overrides: dict[str, str] = {}

    def read(self, page: FixturePage) -> str:
        if page.url in self.overrides:
            return self.overrides[page.url]
        path = self.root / page.file
        if not path.is_file():
            raise FileNotFoundError(f"fixture file missing: {path}")
        return path.read_text(encoding="utf-8")

    def override(self, url: str, markup: str) -> None:
        """Replace one page's body for the lifetime of this bundle."""
        self.overrides[url] = markup

    def __len__(self) -> int:
        return len(self.pages)


class FixtureAdapter(HtmlBoardAdapter):
    """A :class:`HtmlBoardAdapter` that answers from disk.

    Used as the default target for tests, CI and the offline demo. It is driven
    through the real :class:`~jobscout.fetch.client.HttpFetcher` with a fixture
    transport (:mod:`jobscout.fetch.transport`), so politeness, robots handling,
    retries, archiving and metrics all execute exactly as they do in production.
    """

    def __init__(self, bundle: FixtureBundle) -> None:
        super().__init__(bundle.config)
        self.bundle = bundle
        # The adapter name must equal the `source` the parser stamps on items:
        # the pipeline uses it to scope staleness marking and metric labels.
        self.name = bundle.config.name
        self.reads = 0

    def page_for(self, url: str) -> FetchedPage:
        """Return the recorded body for ``url``, or a 404 page if absent.

        The recorded ``kind`` is replayed so a re-parse of the archive sees the
        same request context the live crawl had.
        """
        entry = self.bundle.pages.get(url)
        if entry is None:
            return FetchedPage.build(url=url, status_code=404, text=NOT_FOUND_BODY)
        self.reads += 1
        return FetchedPage.build(
            url=url, status_code=200, text=self.bundle.read(entry), kind=entry.kind
        )


def load_fixture_adapter(path: str | Path) -> FixtureAdapter:
    return FixtureAdapter(FixtureBundle(path))
