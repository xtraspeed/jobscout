"""robots.txt compliance, including the fail-closed policy on server errors."""

from __future__ import annotations

import pytest

from jobscout.fetch.robots import RobotsTxt

AGENT = "JobScoutBot/0.1"

ALLOW_ALL = "User-agent: *\nAllow: /\n"
DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private\nDisallow: /admin\n"
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
NAMED_AGENT = "User-agent: JobScoutBot\nDisallow: /nope\n\nUser-agent: *\nAllow: /\n"
WITH_DELAY = "User-agent: *\nCrawl-delay: 4\nAllow: /\n"


def build(body: str | None, *, enabled: bool = True) -> RobotsTxt:
    async def fetch(_url: str) -> str | None:
        return body

    return RobotsTxt(AGENT, enabled=enabled, fetch=fetch)


@pytest.mark.asyncio
async def test_missing_robots_allows_everything() -> None:
    robots = build(None)  # 404 -> no robots.txt published

    assert await robots.allowed("https://example.com/jobs")


@pytest.mark.asyncio
async def test_allow_all_robots() -> None:
    robots = build(ALLOW_ALL)
    assert await robots.allowed("https://example.com/jobs/1")


@pytest.mark.asyncio
async def test_disallowed_paths() -> None:
    robots = build(DISALLOW_PRIVATE)

    assert not await robots.allowed("https://example.com/private/secret")
    assert not await robots.allowed("https://example.com/admin")
    assert await robots.allowed("https://example.com/jobs")


@pytest.mark.asyncio
async def test_disallow_all() -> None:
    robots = build(DISALLOW_ALL)
    assert not await robots.allowed("https://example.com/anything")


@pytest.mark.asyncio
async def test_agent_specific_rule_wins_over_wildcard() -> None:
    robots = build(NAMED_AGENT)

    assert not await robots.allowed("https://example.com/nope")
    assert await robots.allowed("https://example.com/anything-else")


@pytest.mark.asyncio
async def test_crawl_delay_is_surfaced() -> None:
    robots = build(WITH_DELAY)
    verdict = await robots.check("https://example.com/jobs")

    assert verdict.allowed
    assert verdict.crawl_delay == 4.0


@pytest.mark.asyncio
async def test_default_delay_applies_when_unspecified() -> None:
    async def fetch(_url: str) -> str | None:
        return ALLOW_ALL

    robots = RobotsTxt(AGENT, enabled=True, default_delay=1.5, fetch=fetch)
    verdict = await robots.check("https://example.com/jobs")

    assert verdict.crawl_delay == 1.5


@pytest.mark.asyncio
async def test_disabled_robots_short_circuits() -> None:
    robots = build(DISALLOW_ALL, enabled=False)
    verdict = await robots.check("https://example.com/private")

    assert verdict.allowed
    assert verdict.reason == "robots_disabled"


@pytest.mark.asyncio
async def test_transport_failure_fails_closed() -> None:
    async def fetch(_url: str) -> str | None:
        raise OSError("connection reset")

    robots = RobotsTxt(AGENT, enabled=True, fetch=fetch)

    # Better to skip a host we cannot reach than to hammer it.
    assert not await robots.allowed("https://example.com/jobs")


@pytest.mark.asyncio
async def test_robots_fetched_once_per_host() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> str | None:
        calls.append(url)
        return ALLOW_ALL

    robots = RobotsTxt(AGENT, enabled=True, fetch=fetch)
    await robots.check("https://example.com/a")
    await robots.check("https://example.com/b")

    assert calls == ["https://example.com/robots.txt"]


@pytest.mark.asyncio
async def test_prime_injects_robots_without_fetching() -> None:
    async def fetch(_url: str) -> str | None:
        raise AssertionError("should not fetch when primed")

    robots = RobotsTxt(AGENT, enabled=True, fetch=fetch)
    robots.prime("example.com", DISALLOW_PRIVATE)

    assert not await robots.allowed("https://example.com/private/x")


def test_robots_url_construction() -> None:
    assert RobotsTxt.robots_url("https://example.com/jobs/1") == "https://example.com/robots.txt"
    assert RobotsTxt.robots_url("http://example.com:8080/a") == "http://example.com:8080/robots.txt"
