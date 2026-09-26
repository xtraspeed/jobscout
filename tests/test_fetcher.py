"""HTTP fetcher: politeness, retry classification, archiving and circuit breaking.

Everything runs against a fixture transport, so these are real tests of the
client's decision-making without any network access.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jobscout.config import Settings
from jobscout.errors import CircuitOpen, FetchError, RobotsDenied
from jobscout.fetch import HttpFetcher, MemorySnapshotStore
from jobscout.fetch.transport import PERMISSIVE_ROBOTS, RESTRICTIVE_ROBOTS, fixture_transport


def transport(pages: dict[str, tuple[int, str, str]], **kwargs: Any) -> httpx.AsyncBaseTransport:
    return fixture_transport(pages, **kwargs)


PAGE = (200, "<html><body>hello</body></html>", "text/html")
URL = "https://site.test/page"


@pytest.mark.asyncio
async def test_successful_fetch(settings: Settings) -> None:
    async with HttpFetcher(settings, transport=transport({URL: PAGE})) as fetcher:
        page = await fetcher.fetch(URL)

    assert page.status_code == 200
    assert "hello" in page.text
    assert page.content_hash
    assert page.from_snapshot is False
    assert fetcher.stats.requests == 1
    assert fetcher.stats.statuses["200"] == 1


@pytest.mark.asyncio
async def test_user_agent_is_sent(settings: Settings) -> None:
    seen: dict[str, str] = {}

    def resolve(url: str) -> tuple[int, str, str]:
        return PAGE

    class _Recording(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, text="ok", request=request)

    async with HttpFetcher(settings, transport=_Recording()) as fetcher:
        await fetcher.fetch(URL, check_robots=False)

    assert settings.user_agent in seen["ua"]


@pytest.mark.asyncio
async def test_robots_disallow_raises_before_any_request(settings: Settings) -> None:
    calls: list[str] = []

    class _Counting(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=RESTRICTIVE_ROBOTS, request=request)
            return httpx.Response(200, text="secret", request=request)

    async with HttpFetcher(settings, transport=_Counting()) as fetcher:
        with pytest.raises(RobotsDenied):
            await fetcher.fetch(URL)

    assert fetcher.stats.robots_denied == 1
    assert calls == ["https://site.test/robots.txt"], "the body must never be requested"


@pytest.mark.asyncio
async def test_robots_can_be_disabled(settings: Settings) -> None:
    relaxed = settings.model_copy(update={"respect_robots": False})

    async with HttpFetcher(
        relaxed, transport=transport({URL: PAGE}, robots=RESTRICTIVE_ROBOTS)
    ) as fetcher:
        page = await fetcher.fetch(URL)

    assert page.status_code == 200


@pytest.mark.asyncio
async def test_404_is_not_retried(settings: Settings) -> None:
    attempts = {"n": 0}

    def resolve(_url: str) -> tuple[int, str, str] | None:
        if _url.endswith("/robots.txt"):
            return 200, PERMISSIVE_ROBOTS, "text/plain"
        attempts["n"] += 1
        return 404, "nope", "text/html"

    async with HttpFetcher(
        settings.model_copy(update={"max_retries": 3, "respect_robots": False}),
        transport=fixture_transport(resolve),
    ) as fetcher:
        with pytest.raises(FetchError, match="404"):
            await fetcher.fetch(URL)

    assert attempts["n"] == 1, "a 404 is our mistake; retrying wastes the site's bandwidth"
    assert fetcher.stats.retries == 0


@pytest.mark.asyncio
async def test_5xx_is_retried_then_succeeds(settings: Settings) -> None:
    attempts = {"n": 0}

    def resolve(_url: str) -> tuple[int, str, str]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return 503, "unavailable", "text/html"
        return PAGE

    async with HttpFetcher(
        settings.model_copy(update={"max_retries": 4, "backoff_base": 0.001}),
        transport=fixture_transport(resolve),
    ) as fetcher:
        page = await fetcher.fetch(URL)

    assert page.status_code == 200
    assert attempts["n"] == 3
    assert fetcher.stats.retries == 2


@pytest.mark.asyncio
async def test_retries_are_bounded_and_then_fail(settings: Settings) -> None:
    def resolve(_url: str) -> tuple[int, str, str]:
        return 500, "boom", "text/html"

    async with HttpFetcher(
        settings.model_copy(update={"max_retries": 2, "backoff_base": 0.001}),
        transport=fixture_transport(resolve),
    ) as fetcher:
        with pytest.raises(FetchError, match="failed after retries"):
            await fetcher.fetch(URL)

    assert fetcher.stats.requests == 3, "1 initial attempt + 2 retries"
    assert fetcher.stats.failures == 1


@pytest.mark.asyncio
async def test_retry_after_header_is_honoured(settings: Settings) -> None:
    """A server-supplied delay is a politeness instruction and wins over ours."""

    class _RetryAfter(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=PERMISSIVE_ROBOTS, request=request)
            self.calls += 1
            if self.calls == 1:
                return httpx.Response(
                    429, text="slow down", headers={"Retry-After": "0"}, request=request
                )
            return httpx.Response(200, text="ok", request=request)

    fake = _RetryAfter()
    # backoff_base is huge on purpose: if Retry-After were ignored, the retry
    # would sleep for many seconds and the test would time out.
    async with HttpFetcher(
        settings.model_copy(update={"max_retries": 2, "backoff_base": 60.0}),
        transport=fake,
    ) as fetcher:
        page = await fetcher.fetch(URL)

    assert page.status_code == 200
    assert fake.calls == 2
    assert fetcher.stats.retries == 1


@pytest.mark.asyncio
async def test_circuit_opens_after_repeated_failures(settings: Settings) -> None:
    def resolve(url: str) -> tuple[int, str, str] | None:
        if url.endswith("/robots.txt"):
            return 200, PERMISSIVE_ROBOTS, "text/plain"
        return 500, "boom", "text/html"

    async with HttpFetcher(
        settings.model_copy(
            update={
                "max_retries": 0,
                "circuit_failure_threshold": 2,
                "respect_robots": False,
            }
        ),
        transport=fixture_transport(resolve),
    ) as fetcher:
        for index in range(2):
            url = f"https://site.test/fail-{index}"
            with pytest.raises(FetchError):
                await fetcher.fetch(url)

        assert fetcher.breaker.state("site.test").value == "open"
        with pytest.raises(CircuitOpen):
            await fetcher.fetch("https://site.test/another")


@pytest.mark.asyncio
async def test_success_resets_the_breaker(settings: Settings) -> None:
    """A single success clears the failure count, so flapping hosts recover."""
    tuned = settings.model_copy(
        update={"respect_robots": False, "max_retries": 0, "circuit_failure_threshold": 2}
    )

    async with HttpFetcher(tuned, transport=transport({URL: PAGE})) as fetcher:
        await fetcher.fetch(URL)
        fetcher.breaker.record_failure("site.test")
        assert fetcher.breaker.state("site.test").value == "closed", "one failure is not enough"

        await fetcher.fetch(URL)
        fetcher.breaker.record_failure("site.test")
        assert fetcher.breaker.state("site.test").value == "closed", "the counter was reset"

        fetcher.breaker.record_failure("site.test")
        assert fetcher.breaker.state("site.test").value == "open"


@pytest.mark.asyncio
async def test_snapshots_are_archived(settings: Settings) -> None:
    store = MemorySnapshotStore()

    async with HttpFetcher(
        settings, snapshot_store=store, transport=transport({URL: PAGE})
    ) as fetcher:
        await fetcher.fetch(URL)

    assert store.saved == 1
    replayed = await store.load(URL)
    assert replayed is not None
    assert replayed.from_snapshot is True
    assert "hello" in replayed.text


@pytest.mark.asyncio
async def test_snapshot_can_be_disabled(settings: Settings) -> None:
    store = MemorySnapshotStore()
    disabled = settings.model_copy(update={"store_snapshots": False})

    async with HttpFetcher(
        disabled, snapshot_store=store, transport=transport({URL: PAGE})
    ) as fetcher:
        await fetcher.fetch(URL)

    assert store.saved == 0


@pytest.mark.asyncio
async def test_oversized_body_is_not_archived(settings: Settings) -> None:
    store = MemorySnapshotStore()
    big = (200, "x" * 5000, "text/html")
    tiny = settings.model_copy(update={"snapshot_max_bytes": 1024})

    async with HttpFetcher(tiny, snapshot_store=store, transport=transport({URL: big})) as fetcher:
        page = await fetcher.fetch(URL)

    assert store.saved == 0
    assert len(page.text) == 5000, "the crawl still gets the content"


@pytest.mark.asyncio
async def test_snapshot_failure_never_fails_the_crawl(settings: Settings) -> None:
    class ExplodingStore(MemorySnapshotStore):
        async def save(self, page: Any, *, adapter: str, status_code: int | None = None) -> None:
            raise RuntimeError("disk full")

    async with HttpFetcher(
        settings, snapshot_store=ExplodingStore(), transport=transport({URL: PAGE})
    ) as fetcher:
        page = await fetcher.fetch(URL)

    assert page.status_code == 200


@pytest.mark.asyncio
async def test_per_domain_rate_is_enforced(settings: Settings) -> None:
    """One host must never exceed its configured request rate.

    The bucket's burst ceiling is ``rate`` tokens, so 6 requests at 4/s are 4 free
    and 2 throttled to 0.25s apart: ~0.5s of enforced waiting.
    """
    import time

    slow = settings.model_copy(
        update={"per_domain_rate": 4.0, "per_domain_concurrency": 1, "respect_robots": False}
    )
    urls = [f"https://site.test/{index}" for index in range(6)]
    pages = dict.fromkeys(urls, PAGE)

    async with HttpFetcher(slow, transport=transport(pages)) as fetcher:
        started = time.perf_counter()
        for url in urls:
            await fetcher.fetch(url)
        elapsed = time.perf_counter() - started

    assert elapsed >= 0.45, "the token bucket must throttle same-host traffic"


@pytest.mark.asyncio
async def test_describe_reports_run_level_counters(settings: Settings) -> None:
    async with HttpFetcher(settings, transport=transport({URL: PAGE})) as fetcher:
        await fetcher.fetch(URL)
        described = fetcher.describe()

    assert described["adapter"] == "unknown"
    assert described["requests"] == 1
    assert described["statuses"] == {"200": 1}
    assert "site.test" in described["hosts"]
