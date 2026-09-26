"""selectolax helpers: a tiny CSS selector language and defensive extraction.

Selectors in adapter configs use ``<css>::<extraction>`` where extraction is
either ``text``, ``html``, or ``attr(<name>)``, e.g. ``a.job-link::attr(href)``.

Every helper is total: malformed markup or a missing node yields ``None`` or an
empty list rather than raising, because a site changing its DOM should degrade
one listing, not abort a crawl run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from selectolax.parser import HTMLParser, Node

_WS = re.compile(r"\s+")
_ATTR_RE = re.compile(r"^(?P<selector>.+?)::attr\((?P<attr>[A-Za-z_:][-\w:.]*)\)$")
_KNOWN_EXTRACTIONS: Final = frozenset({"text", "html"})


class SelectorError(ValueError):
    """The selector string is not in the ``<css>::<extraction>`` form."""


@dataclass(frozen=True, slots=True)
class SelectorSpec:
    """A parsed ``<css>::<extraction>`` selector."""

    selector: str
    extraction: str

    @classmethod
    def parse(cls, spec: str) -> SelectorSpec:
        attr_match = _ATTR_RE.match(spec.strip())
        if attr_match:
            return cls(
                selector=attr_match.group("selector").strip(),
                extraction=f"attr({attr_match.group('attr')})",
            )
        selector, separator, extraction = spec.rpartition("::")
        if not separator:
            raise SelectorError(
                f"selector must look like 'css::text' or 'css::attr(href)': {spec!r}"
            )
        extraction = extraction.strip()
        if extraction not in _KNOWN_EXTRACTIONS:
            raise SelectorError(
                f"unknown extraction {extraction!r}; use 'text', 'html' or 'attr(name)'"
            )
        if not selector.strip():
            raise SelectorError(f"selector must not be empty: {spec!r}")
        return cls(selector=selector.strip(), extraction=extraction)


def normalise_ws(value: str | None) -> str:
    """Collapse all whitespace runs to single spaces and strip the ends."""
    if not value:
        return ""
    return _WS.sub(" ", value).strip()


def text_of(node: Node | None) -> str:
    """Whitespace-normalised visible text of a node, recursing into children."""
    if node is None:
        return ""
    return normalise_ws(node.text())


NodeOrMarkup = "Node | HTMLParser | str"


def as_tree(value: Node | HTMLParser | str) -> Node | HTMLParser:
    """Accept either a parsed node or raw markup, so helpers are easy to test."""
    return HTMLParser(value) if isinstance(value, str) else value


def extract_first(root: Node | HTMLParser | str, spec: SelectorSpec) -> str | None:
    """Apply a spec to the first matching node, returning ``None`` if absent."""
    node = as_tree(root).css_first(spec.selector)
    if node is None:
        return None
    return _extract(node, spec.extraction)


def extract_all(root: Node | HTMLParser | str, spec: SelectorSpec) -> list[str]:
    """Apply a spec to every matching node, dropping empties."""
    values: list[str] = []
    for node in as_tree(root).css(spec.selector):
        value = _extract(node, spec.extraction)
        if value:
            values.append(value)
    return values


def extract_field(root: Node | HTMLParser, spec_string: str) -> str | None:
    """Convenience wrapper: parse the spec then extract the first match."""
    return extract_first(root, SelectorSpec.parse(spec_string))


def _extract(node: Node, extraction: str) -> str | None:
    if extraction == "text":
        text = text_of(node)
        return text or None
    if extraction == "html":
        html = node.html or ""
        return normalise_ws(html) or None
    attr_name = extraction.removeprefix("attr(").removesuffix(")")
    value = node.attributes.get(attr_name)
    return value or None


def absolute_url(base_url: str, href: str | None) -> str | None:
    """Resolve ``href`` against ``base_url``, tolerating junk values."""
    if not href:
        return None
    from urllib.parse import urljoin, urlparse

    candidate = href.strip()
    if not candidate or candidate.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    resolved = urljoin(base_url, candidate)
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None
    return resolved


def parse_html(markup: str) -> HTMLParser:
    """Parse a full document."""
    return HTMLParser(markup)


def parse_fragment(markup: str) -> HTMLParser:
    """Parse an HTML fragment (HN comment bodies, for example)."""
    return HTMLParser(markup)
