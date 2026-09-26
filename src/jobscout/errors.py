"""Exception hierarchy for the crawler."""

from __future__ import annotations


class JobScoutError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(JobScoutError):
    """Configuration is missing or invalid."""


class FetchError(JobScoutError):
    """A page could not be retrieved after exhausting retries."""


class RobotsDenied(FetchError):
    """``robots.txt`` disallows fetching this URL for our user agent."""


class CircuitOpen(FetchError):
    """The circuit breaker for a host is open; the request was not attempted."""


class AdapterError(JobScoutError):
    """An adapter could not complete discovery or parsing."""


class SnapshotTooLarge(JobScoutError):
    """A response exceeded ``snapshot_max_bytes`` and was not archived."""
