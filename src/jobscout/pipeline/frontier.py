"""The crawl frontier.

A bounded, deduplicated work queue with depth accounting. It is intentionally
in-memory: a single run is the unit of work, and the ``(source, external_id)``
unique key plus content hashing already give cross-run idempotency, so
persisting every visited URL would add write amplification for no correctness
gain.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from jobscout.models import Follow


def canonical_url(url: str) -> str:
    """Normalise a URL for deduplication.

    Drops the fragment (never sent to servers) and sorts query parameters, so
    ``/jobs?a=1&b=2`` and ``/jobs?b=2&a=1#top`` are one unit of work.
    """
    parts = urlsplit(url.strip())
    query = "&".join(sorted(p for p in parts.query.split("&") if p))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def same_site(a: str, b: str) -> bool:
    """Loose same-site test used to keep crawls from wandering off-domain."""
    host_a = (urlsplit(a).hostname or "").lower().removeprefix("www.")
    host_b = (urlsplit(b).hostname or "").lower().removeprefix("www.")
    if not host_a or not host_b:
        return False
    return host_a == host_b or host_a.endswith(f".{host_b}") or host_b.endswith(f".{host_a}")


@dataclass(order=True, slots=True)
class _Entry:
    depth: int
    order: int
    follow: Follow = field(compare=False)


@dataclass(slots=True)
class FrontierStats:
    """What the frontier did, for the run summary."""

    queued: int = 0
    popped: int = 0
    duplicates: int = 0
    too_deep: int = 0
    max_depth_reached: int = 0
    over_budget: int = 0


class Frontier:
    """Depth-first, deduplicating queue of :class:`Follow` objects."""

    def __init__(
        self, *, max_depth: int = 2, max_items: int = 1000, seeds: Iterable[str] = ()
    ) -> None:
        self.max_depth = max_depth
        self.max_items = max_items
        self.stats = FrontierStats()
        self._heap: list[_Entry] = []
        self._counter = itertools.count()
        self._seen: set[str] = {canonical_url(url) for url in seeds}

    def push(self, follow: Follow, *, depth: int = 0) -> bool:
        """Enqueue a follow-up. Returns ``False`` if it was dropped."""
        if depth > self.max_depth:
            self.stats.too_deep += 1
            return False
        if self.stats.queued >= self.max_items:
            self.stats.over_budget += 1
            return False
        key = canonical_url(follow.url)
        if key in self._seen:
            self.stats.duplicates += 1
            return False
        self._seen.add(key)
        heapq.heappush(self._heap, _Entry(depth, next(self._counter), follow))
        self.stats.queued += 1
        self.stats.max_depth_reached = max(self.stats.max_depth_reached, depth)
        return True

    def push_all(self, follows: Iterable[Follow], *, depth: int = 0) -> int:
        return sum(1 for follow in follows if self.push(follow, depth=depth))

    def pop(self) -> tuple[Follow, int] | None:
        """Next follow and its depth, or ``None`` when the queue is empty."""
        if not self._heap:
            return None
        entry = heapq.heappop(self._heap)
        self.stats.popped += 1
        return entry.follow, entry.depth

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def __iter__(self) -> Iterator[tuple[Follow, int]]:
        while self._heap:
            popped = self.pop()
            if popped is None:
                return
            yield popped
