"""Token bucket and per-domain limiter, on an injected clock (no real waiting)."""

from __future__ import annotations

import asyncio

import pytest

from jobscout.fetch.ratelimit import DomainLimiter, TokenBucket, host_of


class FakeClock:
    """Monotonic clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_token_bucket_allows_initial_burst() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, capacity=2.0, clock=clock, sleep=clock.sleep)

    asyncio.run(bucket.acquire())
    asyncio.run(bucket.acquire())

    assert clock.sleeps == [], "the initial burst should not sleep"
    assert bucket.available == pytest.approx(0.0)


def test_token_bucket_throttles_after_burst() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, capacity=2.0, clock=clock, sleep=clock.sleep)

    for _ in range(5):
        asyncio.run(bucket.acquire())

    # 2 free, then 3 tokens at 2/s = 1.5s of enforced waiting.
    assert sum(clock.sleeps) == pytest.approx(1.5, rel=1e-6)


def test_token_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=4.0, capacity=4.0, clock=clock, sleep=clock.sleep)

    for _ in range(4):
        asyncio.run(bucket.acquire())
    assert clock.now == 0.0

    clock.now += 2.0  # 2 seconds of refill at 4/s
    asyncio.run(bucket.acquire())
    assert clock.sleeps == [], "tokens should have refilled without waiting"


def test_token_bucket_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0)
    with pytest.raises(ValueError):
        asyncio.run(TokenBucket(rate=1.0, capacity=1.0).acquire(2.0))


def test_domain_limiter_creates_one_limiter_per_host() -> None:
    clock = FakeClock()
    limiter = DomainLimiter(concurrency=2, rate=1.0, clock=clock, sleep=clock.sleep)

    first = limiter.get("example.com")
    assert limiter.get("example.com") is first, "a host must reuse its limiter"
    assert limiter.get("other.com") is not first, "hosts must not share one"
    assert limiter.hosts == ["example.com", "other.com"]


def test_domain_limiter_registers_hosts_on_acquire() -> None:
    clock = FakeClock()
    limiter = DomainLimiter(concurrency=2, rate=1000.0, clock=clock, sleep=clock.sleep)

    asyncio.run(limiter.acquire("https://example.com/a"))
    asyncio.run(limiter.acquire("https://example.com/b"))
    asyncio.run(limiter.acquire("https://other.com/a"))

    assert limiter.hosts == ["example.com", "other.com"]


def test_domain_limiter_applies_robots_crawl_delay() -> None:
    clock = FakeClock()
    limiter = DomainLimiter(concurrency=4, rate=1000.0, clock=clock, sleep=clock.sleep)

    limiter.get("slow.example")
    limiter.configure("slow.example", rate=0.5)  # 2s crawl-delay

    started_at = clock.now
    for _ in range(3):
        asyncio.run(limiter.acquire("https://slow.example/page"))

    assert clock.now - started_at == pytest.approx(4.0, rel=1e-6)


def test_host_of_handles_ports_and_case() -> None:
    assert host_of("https://Example.COM/jobs") == "example.com"
    assert host_of("http://example.com:8080/x") == "example.com:8080"
    assert host_of("https://example.com:443/x") == "example.com"
    assert host_of("https://example.com:80/x") == "example.com"
