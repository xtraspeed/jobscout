"""Prometheus metrics.

All metric names are prefixed ``jobscout_``. Labels are deliberately low
cardinality: never a raw URL, request id, or free-text value.
"""

from __future__ import annotations

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

REQUESTS = Counter(
    "jobscout_http_requests_total",
    "HTTP requests issued, by adapter and outcome.",
    labelnames=("adapter", "outcome"),
)

REQUEST_LATENCY = Histogram(
    "jobscout_http_request_duration_seconds",
    "End-to-end latency of a single HTTP request including retries.",
    labelnames=("adapter",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

PAGES_FETCHED = Counter(
    "jobscout_pages_fetched_total",
    "Pages successfully retrieved, by adapter.",
    labelnames=("adapter",),
)

RATE_LIMIT_WAIT = Histogram(
    "jobscout_ratelimit_wait_seconds",
    "Time spent waiting on the per-domain rate limiter.",
    labelnames=("adapter",),
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0),
)

RETRIES = Counter(
    "jobscout_retries_total",
    "Retries attempted, by reason.",
    labelnames=("adapter", "reason"),
)

CIRCUIT_OPEN = Counter(
    "jobscout_circuit_open_total",
    "Requests skipped because the host circuit breaker was open.",
    labelnames=("host",),
)

ITEMS_SEEN = Counter(
    "jobscout_items_seen_total",
    "Listings extracted by adapters.",
    labelnames=("adapter",),
)

ITEMS_WRITTEN = Counter(
    "jobscout_items_written_total",
    "Listings persisted, by write outcome (new/changed/unchanged).",
    labelnames=("adapter", "outcome"),
)

CRAWL_DEPTH = Gauge(
    "jobscout_crawl_queue_depth",
    "URLs still queued in the current run.",
    labelnames=("adapter",),
)

CRAWL_DURATION = Histogram(
    "jobscout_crawl_run_duration_seconds",
    "Wall-clock duration of a complete crawl run.",
    labelnames=("adapter",),
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
)

API_REQUESTS = Counter(
    "jobscout_api_requests_total",
    "Query API requests.",
    labelnames=("route", "status"),
)

DB_WRITE_SECONDS = Histogram(
    "jobscout_db_write_seconds",
    "Latency of repository write operations.",
    labelnames=("operation",),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
)


def new_registry() -> CollectorRegistry:
    """An isolated registry, used by tests so metric names never collide."""
    return CollectorRegistry()


def render_metrics(registry: CollectorRegistry | None = None) -> bytes:
    """Render a registry in Prometheus text exposition format."""
    return generate_latest(registry if registry is not None else REGISTRY)
