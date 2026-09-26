"""Structured logging setup (structlog) shared by every entrypoint."""

from __future__ import annotations

import logging
import sys
from typing import Any, TypeAlias

import structlog

Logger: TypeAlias = structlog.typing.FilteringBoundLogger

_CONFIGURED = False


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog for the whole process.

    ``fmt="json"`` is the container/production default; ``"console"`` renders
    key=value lines that are easier to read during local development.

    Safe to call more than once: the last call wins, so an application can
    reconfigure after importing modules that logged during import.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level)
    # httpx/httpcore emit their own INFO access logs; our spans already cover them.
    for noisy in ("httpx", "httpcore", "hpack", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=False)
        if fmt == "console"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    global _CONFIGURED
    _CONFIGURED = True


def get_logger(name: str | None = None, **initial: Any) -> Logger:
    """Return a bound logger, applying defaults on first use.

    Library code (adapters, repositories) logs through this, so a sensible
    default is configured if no entrypoint has done it yet. An entrypoint that
    wants different settings calls :func:`configure_logging` itself, and that
    call is never ignored.
    """
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name, **initial)  # type: ignore[no-any-return]
