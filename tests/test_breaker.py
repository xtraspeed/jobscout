"""Circuit breaker state machine, driven by an injected clock."""

from __future__ import annotations

import pytest

from jobscout.fetch.breaker import CircuitBreaker, State


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_starts_closed_and_allows_traffic() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=30, clock=Clock())

    assert breaker.state("example.com") is State.CLOSED
    for _ in range(10):
        assert breaker.allow("example.com")


def test_opens_after_threshold_consecutive_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=30, clock=Clock())

    for _ in range(2):
        breaker.record_failure("example.com")
        assert breaker.allow("example.com")

    breaker.record_failure("example.com")
    assert breaker.state("example.com") is State.OPEN
    assert not breaker.allow("example.com"), "an open breaker must skip the request"


def test_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=30, clock=Clock())

    breaker.record_failure("example.com")
    breaker.record_failure("example.com")
    breaker.record_success("example.com")
    breaker.record_failure("example.com")

    assert breaker.state("example.com") is State.CLOSED
    assert breaker.allow("example.com")


def test_half_open_after_cooldown_admits_one_trial() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=30, clock=clock)
    breaker.record_failure("example.com")
    breaker.record_failure("example.com")
    assert breaker.state("example.com") is State.OPEN

    clock.advance(29)
    assert breaker.state("example.com") is State.OPEN, "still cooling down"

    clock.advance(2)
    assert breaker.state("example.com") is State.HALF_OPEN
    assert breaker.allow("example.com"), "half-open admits exactly one trial"
    assert not breaker.allow("example.com"), "and only one"


def test_failed_trial_reopens_immediately() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=10, clock=clock)
    breaker.record_failure("a.com")
    breaker.record_failure("a.com")
    clock.advance(11)

    assert breaker.allow("a.com")
    breaker.record_failure("a.com")
    assert breaker.state("a.com") is State.OPEN


def test_successful_trial_closes_the_breaker() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)
    breaker.record_failure("a.com")
    clock.advance(11)

    assert breaker.allow("a.com")
    breaker.record_success("a.com")
    assert breaker.state("a.com") is State.CLOSED


def test_breakers_are_independent_per_host() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=60, clock=Clock())
    breaker.record_failure("bad.com")

    assert not breaker.allow("bad.com")
    assert breaker.allow("good.com"), "one bad host must not block another"


def test_snapshot_and_reset() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=60, clock=Clock())
    breaker.record_failure("bad.com")
    assert breaker.snapshot() == {"bad.com": "open"}

    breaker.reset()
    assert breaker.snapshot() == {}


def test_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
