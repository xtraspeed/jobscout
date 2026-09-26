"""``robots.txt`` compliance.

The parser is injected, so this module is testable without a network. Policy:

* ``404``/``410`` -> everything allowed (no robots.txt means no restrictions).
* ``2xx``        -> honour the rules for our user agent.
* ``5xx``/network error -> **fail closed**: skip the host and log a warning,
  because hammering a host that is already in trouble is exactly what we
  promised not to do.
* ``crawl_delay`` -> fed to the rate limiter so politeness is enforced, not just
  parsed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from jobscout.fetch.ratelimit import host_of
from jobscout.observability import get_logger

_log = get_logger(__name__)

RobotsFetcher = Callable[[str], Awaitable[str | None]]


@dataclass(slots=True)
class RobotsVerdict:
    """Outcome of a robots check for one URL."""

    allowed: bool
    reason: str
    crawl_delay: float | None = None


class RobotsTxt:
    """Per-host robots.txt cache."""

    def __init__(
        self,
        user_agent: str,
        *,
        enabled: bool = True,
        default_delay: float | None = None,
        fetch: RobotsFetcher | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.enabled = enabled
        self.default_delay = default_delay
        self._fetch = fetch
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def robots_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    async def _load(self, host: str, robots_url: str) -> RobotFileParser | None:
        if host in self._cache:
            return self._cache[host]
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host in self._cache:  # another task won the race
                return self._cache[host]
            parser: RobotFileParser | None
            if self._fetch is None:
                parser = None
            else:
                try:
                    body = await self._fetch(robots_url)
                except Exception as exc:
                    _log.warning("robots.fetch_failed", host=host, error=str(exc))
                    body = None
                    parser = _all_disallowed()
                else:
                    parser = _parse(body)
            self._cache[host] = parser
            return parser

    async def check(self, url: str) -> RobotsVerdict:
        """Decide whether ``url`` may be fetched by our user agent."""
        if not self.enabled:
            return RobotsVerdict(allowed=True, reason="robots_disabled")

        host = host_of(url)
        parser = await self._load(host, self.robots_url(url))
        if parser is None:  # no fetcher wired (tests) -> treat as unrestricted
            return RobotsVerdict(allowed=True, reason="no_parser")

        if not parser.can_fetch(self.user_agent, url):
            return RobotsVerdict(allowed=False, reason="disallowed_by_robots")

        delay = _as_float(parser.crawl_delay(self.user_agent)) or _as_float(parser.crawl_delay("*"))
        if delay is None:
            delay = self.default_delay
        return RobotsVerdict(allowed=True, reason="allowed", crawl_delay=delay)

    async def allowed(self, url: str) -> bool:
        return (await self.check(url)).allowed

    def prime(self, host: str, body: str) -> None:
        """Inject a robots.txt body for a host (used by tests and fixtures)."""
        self._cache[host] = _parse(body)


def _as_float(value: object) -> float | None:
    """Coerce a ``Crawl-delay`` directive, which may be typed as a string."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse(body: str | None) -> RobotFileParser | None:
    """``None`` means "no restrictions"; otherwise a populated parser."""
    if body is None:
        return None
    parser = RobotFileParser()
    parser.parse(body.splitlines())
    return parser


def _all_disallowed() -> RobotFileParser:
    parser = RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /"])
    return parser
