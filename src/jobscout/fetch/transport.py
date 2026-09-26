"""HTTP transports that serve recorded data instead of the network.

Using a transport (rather than monkeypatching the fetcher) means tests exercise
the real :class:`~jobscout.fetch.client.HttpFetcher`: rate limiting, robots
handling, retry classification, archiving and metrics all run for real, only the
socket is replaced.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Final, TypeAlias
from urllib.parse import urlsplit

import httpx

PERMISSIVE_ROBOTS: Final = "User-agent: *\nAllow: /\n"
RESTRICTIVE_ROBOTS: Final = "User-agent: *\nDisallow: /\n"

#: (status_code, body, content_type), or ``None`` for "no such page".
Resolver: TypeAlias = Callable[[str], "tuple[int, str, str] | None"]
Pages: TypeAlias = "Mapping[str, tuple[int, str, str]] | Resolver"


def fixture_transport(
    pages: Pages,
    *,
    robots: str = PERMISSIVE_ROBOTS,
    robots_status: int = 200,
) -> httpx.AsyncBaseTransport:
    """Build a transport that answers from a URL map or a resolver callable.

    ``robots.txt`` requests are served from ``robots`` so the compliance layer
    is exercised rather than bypassed.
    """

    def resolve(url: str) -> tuple[int, str, str] | None:
        if callable(pages):
            return pages(url)
        return pages.get(url)

    class _FixtureTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[str] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.requests.append(url)
            if url.endswith("/robots.txt"):
                return httpx.Response(
                    robots_status,
                    text=robots,
                    headers={"content-type": "text/plain"},
                    request=request,
                )
            resolved = resolve(url)
            if resolved is None:
                return httpx.Response(404, text="not found (fixture)", request=request)
            status, body, content_type = resolved
            return httpx.Response(
                status, text=body, headers={"content-type": content_type}, request=request
            )

    return _FixtureTransport()


def json_transport(payloads: Mapping[str, Any], **kwargs: Any) -> httpx.AsyncBaseTransport:
    """Transport serving JSON documents (for API-backed adapters).

    Documents are keyed by URL *path*: for a JSON API the path identifies the
    resource and the query string carries request parameters, so
    ``/search?query=x`` resolves to the ``/search`` document. This keeps fixtures
    from having to restate every query string an adapter builds.
    """

    def resolve(url: str) -> tuple[int, str, str] | None:
        key = urlsplit(url)._replace(query="", fragment="").geturl()
        if key not in payloads:
            return None
        return 200, json.dumps(payloads[key]), "application/json"

    return fixture_transport(resolve, **kwargs)
