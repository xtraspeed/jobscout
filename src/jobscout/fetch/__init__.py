"""HTTP fetching: pooled client, politeness, retries, breaker, archiving."""

from __future__ import annotations

from jobscout.fetch.breaker import CircuitBreaker, State
from jobscout.fetch.client import FetchStats, HttpFetcher
from jobscout.fetch.ratelimit import DomainLimiter, TokenBucket, host_of
from jobscout.fetch.retry import RetryPolicy, retry_after_seconds
from jobscout.fetch.robots import RobotsTxt, RobotsVerdict
from jobscout.fetch.snapshot import MemorySnapshotStore, NullSnapshotStore, SnapshotStore
from jobscout.fetch.transport import fixture_transport, json_transport

__all__ = [
    "CircuitBreaker",
    "DomainLimiter",
    "FetchStats",
    "HttpFetcher",
    "MemorySnapshotStore",
    "NullSnapshotStore",
    "RetryPolicy",
    "RobotsTxt",
    "RobotsVerdict",
    "SnapshotStore",
    "State",
    "TokenBucket",
    "fixture_transport",
    "host_of",
    "json_transport",
    "retry_after_seconds",
]
