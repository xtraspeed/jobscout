"""Per-host circuit breaker.

Purpose: one slow or broken host must not stall an entire crawl. After N
consecutive failures the breaker opens and further requests to that host are
skipped immediately; after a cool-off it admits a single trial request
(half-open) to test recovery.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from jobscout.observability.metrics import CIRCUIT_OPEN


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _Host:
    state: State = State.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    trial_in_flight: bool = False


class CircuitBreaker:
    """Tracks breaker state per host.

    ``clock`` is injected for deterministic tests.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 8,
        reset_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._hosts: dict[str, _Host] = {}

    def _host(self, host: str) -> _Host:
        return self._hosts.setdefault(host, _Host())

    def state(self, host: str) -> State:
        """Current state, promoting OPEN to HALF_OPEN once the cool-off elapses."""
        entry = self._host(host)
        if entry.state is State.OPEN and self._clock() - entry.opened_at >= self.reset_seconds:
            entry.state = State.HALF_OPEN
            entry.trial_in_flight = False
        return entry.state

    def allow(self, host: str) -> bool:
        """Whether a request to ``host`` may proceed right now."""
        current = self.state(host)
        if current is State.CLOSED:
            return True
        if current is State.OPEN:
            CIRCUIT_OPEN.labels(host=host).inc()
            return False
        entry = self._host(host)
        if entry.trial_in_flight:
            return False
        entry.trial_in_flight = True
        return True

    def record_success(self, host: str) -> None:
        entry = self._host(host)
        entry.state = State.CLOSED
        entry.failures = 0
        entry.trial_in_flight = False

    def record_failure(self, host: str) -> None:
        entry = self._host(host)
        if entry.state is State.HALF_OPEN:
            entry.state = State.OPEN
            entry.opened_at = self._clock()
            entry.trial_in_flight = False
            return
        entry.failures += 1
        entry.trial_in_flight = False
        if entry.failures >= self.failure_threshold:
            entry.state = State.OPEN
            entry.opened_at = self._clock()

    def reset(self, host: str | None = None) -> None:
        if host is None:
            self._hosts.clear()
        else:
            self._hosts.pop(host, None)

    def snapshot(self) -> dict[str, str]:
        """``{host: state}`` for the run summary."""
        return {host: self.state(host).value for host in sorted(self._hosts)}
