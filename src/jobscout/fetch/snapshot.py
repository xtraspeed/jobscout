"""Raw response archiving.

Every fetched body can be stored gzipped so parsers can be re-run later without
touching the network again. This is what makes ``jobscout reparse`` possible and
turns a site redesign into a five-minute job instead of a fresh crawl.

The persistence implementation lives in :mod:`jobscout.store.snapshots` to keep
``fetch`` free of database imports.
"""

from __future__ import annotations

import gzip
from typing import Protocol, runtime_checkable

from jobscout.models import FetchedPage


def compress(text: str) -> bytes:
    """Gzip a response body, capped by the caller beforehand."""
    return gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0)


def decompress(payload: bytes) -> str:
    return gzip.decompress(payload).decode("utf-8", errors="replace")


@runtime_checkable
class SnapshotStore(Protocol):
    """Where raw responses go."""

    async def save(
        self,
        page: FetchedPage,
        *,
        adapter: str,
        status_code: int | None = None,
    ) -> None:
        """Persist a response body. Must never raise on the happy path.

        ``page.kind`` records what the adapter asked for, so a later re-parse can
        replay the page with the same context.
        """
        ...

    async def load(self, url: str) -> FetchedPage | None:
        """Return the most recent archived body for ``url``, if any."""
        ...


class NullSnapshotStore:
    """Disables archiving (used when ``store_snapshots`` is false)."""

    async def save(
        self, page: FetchedPage, *, adapter: str, status_code: int | None = None
    ) -> None:
        return None

    async def load(self, url: str) -> FetchedPage | None:
        return None


class MemorySnapshotStore:
    """In-memory archive for tests and dry runs."""

    def __init__(self) -> None:
        self._pages: dict[str, FetchedPage] = {}
        self.saved = 0

    async def save(
        self, page: FetchedPage, *, adapter: str, status_code: int | None = None
    ) -> None:
        self._pages[page.url] = page
        self.saved += 1

    async def load(self, url: str) -> FetchedPage | None:
        page = self._pages.get(url)
        if page is None:
            return None
        return FetchedPage(
            url=page.url,
            status_code=page.status_code,
            text=page.text,
            headers=page.headers,
            content_hash=page.content_hash,
            fetched_at=page.fetched_at,
            elapsed=page.elapsed,
            from_snapshot=True,
            kind=page.kind,
        )
