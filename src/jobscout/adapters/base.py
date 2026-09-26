"""The adapter contract.

An adapter is the only site-specific code in the project. It knows how to seed a
crawl and how to turn a fetched page into either more URLs to visit
(:class:`~jobscout.models.Follow`) or finished listings
(:class:`~jobscout.models.Emitted`). It never performs I/O itself: the pipeline
owns fetching, politeness, retries and persistence.

That split is what makes the same parser usable over live HTTP (htmlboard,
hnhiring) and over recorded fixtures (fixture), and what makes the whole crawl
loop testable without a network.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from jobscout.models import Emitted, FetchedPage, Follow


@runtime_checkable
class Adapter(Protocol):
    """Site-specific discovery and parsing."""

    name: str

    async def start(self) -> Sequence[Follow]:
        """Seed URLs for a new run."""
        ...

    def parse(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]:
        """Turn a fetched page into follow-up URLs and/or finished items.

        ``follow`` is the request that produced ``page``; its ``kind`` and
        ``meta`` tell the adapter what it is looking at (index page vs detail
        page, thread vs comment) without having to sniff the URL.

        Must be total: return an empty sequence for unknown pages instead of
        raising, so one unexpected page cannot abort a run.
        """
        ...


class BaseAdapter:
    """Convenience base with a name and a default no-op ``parse``."""

    name: str = "base"

    async def start(self) -> Sequence[Follow]:
        raise NotImplementedError

    def parse(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"
