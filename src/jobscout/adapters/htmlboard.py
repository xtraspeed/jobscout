"""Generic, config-driven HTML job board adapter.

All site knowledge lives in a YAML file passed to this class, so supporting a
new board means writing selectors, not code. Two request kinds are produced:

``list``    -> index pages, paginated by next-link or a page template
``detail``  -> one page per listing, merged with the list-level seed data

When no ``detail`` section is configured, index pages emit items directly, which
covers boards whose index rows contain everything worth storing.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from jobscout.adapters.base import BaseAdapter
from jobscout.models import Emitted, FetchedPage, Follow
from jobscout.observability import get_logger
from jobscout.parse.board import BoardConfig, JobBoardParser

_log = get_logger(__name__)

KIND_LIST = "list"
KIND_DETAIL = "detail"


class HtmlBoardAdapter(BaseAdapter):
    """Crawls any board described by a :class:`BoardConfig`."""

    def __init__(self, config: BoardConfig, *, parser: JobBoardParser | None = None) -> None:
        self.config = config
        self.name = config.name
        self.parser = parser or JobBoardParser(config)

    @classmethod
    def from_yaml(cls, path: str | Path, **overrides: object) -> HtmlBoardAdapter:
        return cls(BoardConfig.from_yaml(path), **overrides)  # type: ignore[arg-type]

    async def start(self) -> Sequence[Follow]:
        first = self.config.start_urls[0]
        _log.info(
            "htmlboard.start",
            board=self.name,
            start=first,
            max_pages=self.config.pagination.max_pages,
        )
        return [
            Follow(
                url=first,
                kind=KIND_LIST,
                meta={"page": 1, "seen": 0, "base_url": self.config.resolved_base_url},
            )
        ]

    def parse(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]:
        if follow.kind == KIND_DETAIL:
            return self._parse_detail(page, follow)
        if follow.kind == KIND_LIST:
            return self._parse_list(page, follow)
        return []

    # -- index pages --------------------------------------------------------

    def _parse_list(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]:
        base_url = str(follow.meta.get("base_url") or self.config.resolved_base_url)
        parsed = self.parser.parse_list(page.text, base_url)
        seen = int(follow.meta.get("seen", 0))
        page_number = int(follow.meta.get("page", 1))

        results: list[Follow | Emitted] = []
        for detail_url in parsed.detail_urls:
            seen += 1
            if self.config.detail:
                results.append(
                    Follow(
                        url=detail_url,
                        kind=KIND_DETAIL,
                        meta={"seed": parsed.seeds.get(detail_url, {}), "base_url": base_url},
                    )
                )
            else:
                item = self.parser.build_item(detail_url, parsed.seeds.get(detail_url, {}))
                if item is not None:
                    results.append(Emitted(item=item, source_url=page.url))

        next_page = page_number + 1
        if next_page <= self.config.pagination.max_pages:
            for next_url in self._next_urls(parsed.next_urls, next_page):
                results.append(
                    Follow(
                        url=next_url,
                        kind=KIND_LIST,
                        meta={"page": next_page, "seen": seen, "base_url": base_url},
                    )
                )

        _log.info(
            "htmlboard.list_parsed",
            board=self.name,
            page=page_number,
            details=len(parsed.detail_urls),
            next_pages=len(parsed.next_urls),
        )
        return results

    def _next_urls(self, next_from_markup: Sequence[str], next_page: int) -> list[str]:
        """Prefer a real "next" link; otherwise synthesise one from config.

        Only one next URL is followed at a time so the crawl stays a linear
        pagination walk rather than fanning out across a site.
        """
        if next_from_markup:
            return [next_from_markup[0]]
        try:
            return [self.config.page_url(next_page)]
        except ValueError:
            return []

    # -- detail pages -------------------------------------------------------

    def _parse_detail(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]:
        seed = follow.meta.get("seed") or {}
        if not isinstance(seed, dict):
            seed = {}
        item = self.parser.parse_detail(page.text, page.url, seed=seed)
        if item is None:
            _log.debug("htmlboard.detail_incomplete", board=self.name, url=page.url)
            return []
        return [Emitted(item=item, source_url=page.url)]
