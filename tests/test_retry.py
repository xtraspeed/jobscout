"""Retry classification, backoff shape and ``Retry-After`` parsing."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from jobscout.fetch.retry import RETRYABLE_STATUS, RetryPolicy, retry_after_seconds


def test_retryable_statuses_are_the_transient_ones() -> None:
    assert {429, 500, 502, 503, 504} <= RETRYABLE_STATUS
    # A 404 is our mistake: retrying it just wastes the site's bandwidth.
    assert 404 not in RETRYABLE_STATUS
    assert 403 not in RETRYABLE_STATUS


def test_backoff_grows_exponentially_and_is_capped() -> None:
    policy = RetryPolicy(base=0.5, cap=8.0)
    ceiling = [policy.cap]  # sample the top of each jitter range
    rng = random.Random(7)

    for attempt in range(6):
        samples = [RetryPolicy(base=0.5, cap=8.0).backoff(attempt, rng=rng) for _ in range(200)]
        ceiling.append(max(samples))
    del policy

    assert ceiling[1] <= 1.0  # attempt 0 -> base * 2**0 = 0.5, jittered up to that
    assert ceiling[2] <= 2.0
    assert ceiling[3] <= 4.0
    assert ceiling[4] <= 8.0
    assert ceiling[5] <= 8.0, "backoff must be capped"
    assert ceiling[6] <= 8.0


def test_backoff_never_exceeds_ceiling() -> None:
    policy = RetryPolicy(base=1.0, cap=4.0)
    rng = random.Random(1)
    for attempt in range(10):
        delay = policy.backoff(attempt, rng=rng)
        assert 0.0 <= delay <= policy.cap


def test_should_retry_classification() -> None:
    policy = RetryPolicy()
    assert policy.should_retry_status(429)
    assert policy.should_retry_status(503)
    assert not policy.should_retry_status(404)
    assert policy.should_retry_exception(TimeoutError())
    assert policy.should_retry_exception(OSError())
    assert not policy.should_retry_exception(ValueError())


def test_retry_after_delta_seconds() -> None:
    assert retry_after_seconds({"retry-after": "12"}) == 12.0
    assert retry_after_seconds({"Retry-After": "0"}) == 0.0
    assert retry_after_seconds({"retry-after": " 5 "}) == 5.0


def test_retry_after_http_date() -> None:
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    headers = {"retry-after": format_datetime(now + timedelta(seconds=90), usegmt=True)}

    assert retry_after_seconds(headers, now=now.timestamp()) == pytest.approx(90.0, abs=1.5)


def test_retry_after_ignores_garbage() -> None:
    assert retry_after_seconds(None) is None
    assert retry_after_seconds({}) is None
    assert retry_after_seconds({"retry-after": "soon"}) is None
