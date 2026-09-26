"""Parser for Hacker News "Ask HN: Who is hiring?" comment threads.

Hacker News is a good, deliberately public target: the Algolia search API and
the Firebase item API are documented public endpoints, and the postings inside
the threads are public company-authored text. The interesting engineering is
that the structured data is *free text inside HTML inside JSON*, so this module
has to be tolerant of the many formats companies use.

Recognised shapes (in order of preference)::

    Company | Role
    Location | Remote | Full-time
    $120k - $180k
    <p>Extra paragraphs...</p>
    <i>original post</i>  (HN renders the employer's own wording in italics)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from selectolax.parser import HTMLParser

from jobscout.models import JobItem
from jobscout.parse.html import text_of
from jobscout.parse.text import (
    detect_employment_type,
    html_to_text,
    looks_remote,
    normalise_text,
    parse_date,
    parse_salary,
    scrub_pii,
    truncate,
)

HN_ITEM_BASE: Final = "https://hirebase.firebaseio.com/v0/item"
HN_ITEM_WEB: Final = "https://news.ycombinator.com/item?id={item_id}"
HN_SEARCH_API: Final = "https://hn.algolia.com/api/v1/search"

MAX_COMMENT_CHARS: Final = 4000

#: "Acme Corp | Senior Engineer" and friends.
_ROLE_LINE: Final = re.compile(r"^(?P<company>[^|]{2,80})\s*\|\s*(?P<role>.+)$")
#: A line that is only a location / "Remote" / employment type.
_META_LINE: Final = re.compile(
    r"^(?:"
    r"(?:location|based in|office|locations?)\s*[:\-]\s*.+|"
    r"remote|fully remote|hybrid|on-?site|worldwide|anywhere|"
    r"full[- ]?time|part[- ]?time|contract(?:or)?|intern(?:ship)?|temporary"
    r")\s*$",
    re.IGNORECASE,
)
_SALARY_LINE: Final = re.compile(
    r"(?:salary|comp(?:ensation)?|pay|rate|budget)\s*[:\-]?\s*(?P<body>[^|]{1,80})",
    re.IGNORECASE,
)
_NOT_A_POSTING: Final = re.compile(
    r"\b(i'?m not hiring|we'?re not hiring|no longer hiring|not hiring|closed)\b", re.IGNORECASE
)
_URL_LINE: Final = re.compile(r"^\s*https?://\S+\s*$")


@dataclass(slots=True)
class HnPosting:
    """A structured posting extracted from one HN comment."""

    company: str
    role: str
    location: str | None = None
    remote: bool = False
    employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    description: str = ""
    links: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def comment_body_html(raw_html: str | None) -> str:
    """HN stores comment bodies as HTML with ``<p>`` paragraph breaks."""
    if not raw_html:
        return ""
    return raw_html


def parse_comment(raw_html: str | None) -> HnPosting | None:
    """Parse one comment body into a posting, or ``None`` if it is not one."""
    if not raw_html or not raw_html.strip():
        return None

    tree = HTMLParser(comment_body_html(raw_html))
    text = html_to_text(raw_html)
    if not text or _NOT_A_POSTING.search(text):
        return None

    links = _extract_links(tree, raw_html)
    italics = _extract_italics(tree)

    lines = [normalise_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    company: str | None = None
    role: str | None = None
    state = _MetaState()
    body_start = 0

    for index, line in enumerate(lines[:6]):
        role_match = _ROLE_LINE.match(line)
        if role_match and not _URL_LINE.match(line):
            company = normalise_text(role_match.group("company"))
            role = normalise_text(role_match.group("role"))
            body_start = index + 1
            break

    if not company or not role:
        return None

    # Consume the optional metadata lines directly under the role line. These are
    # frequently pipe-delimited, e.g. "Location: Berlin, Germany | Remote | Full-time".
    cursor = body_start
    while cursor < len(lines) and cursor < body_start + 4:
        line = lines[cursor]
        if _META_LINE.match(line):
            for segment in (part.strip() for part in line.split("|")):
                _apply_meta_line(segment, state)
            cursor += 1
            continue
        salary_match = _SALARY_LINE.match(line)
        if salary_match and state.salary_text is None:
            state.salary_text = normalise_text(salary_match.group("body"))
            cursor += 1
            continue
        break

    description = "\n".join(italics) if italics else "\n".join(lines[cursor:])
    description = truncate(scrub_pii(description), MAX_COMMENT_CHARS)

    haystack = " ".join([role, state.location or "", description])
    salary_min, salary_max, currency = parse_salary(state.salary_text or haystack)

    employment_type = state.employment_type or detect_employment_type(role, description)

    tags: list[str] = []
    remote = state.remote or looks_remote(haystack)
    if remote:
        tags.append("remote")
    if employment_type:
        tags.append(employment_type)
    tags.extend(_infer_tech_tags(description))

    return HnPosting(
        company=company,
        role=role,
        location=state.location,
        remote=remote,
        employment_type=employment_type,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        description=description,
        links=links,
        tags=tags,
    )


@dataclass(slots=True)
class _MetaState:
    """Metadata accumulated from the lines under a posting's role line."""

    location: str | None = None
    remote: bool = False
    employment_type: str | None = None
    salary_text: str | None = None


_LOCATION_PREFIX: Final = re.compile(
    r"^(?:location|based in|office|locations?)\s*[:\-]\s*", re.IGNORECASE
)
_REMOTE_MARKERS: Final = frozenset(
    {"remote", "fully remote", "work from home", "wfh", "distributed", "anywhere", "worldwide"}
)


def _apply_meta_line(segment: str, state: _MetaState) -> None:
    """Fold one pipe-delimited metadata segment into ``state``.

    Segments are markers ("Remote", "Full-time") until one is left over, which
    is taken as the location.
    """
    if not segment:
        return
    cleaned = normalise_text(_LOCATION_PREFIX.sub("", segment))
    if not cleaned:
        return
    if cleaned.lower() in _REMOTE_MARKERS:
        state.remote = True
        return
    detected = detect_employment_type(cleaned)
    if detected is not None and len(cleaned) <= 24:
        state.employment_type = detected
        return
    if state.location is None:
        state.location = cleaned


_TECH_TAGS: Final[tuple[str, ...]] = (
    "python",
    "django",
    "flask",
    "fastapi",
    "go",
    "golang",
    "rust",
    "java",
    "kotlin",
    "typescript",
    "javascript",
    "react",
    "node",
    "postgres",
    "postgresql",
    "kubernetes",
    "docker",
    "aws",
    "gcp",
    "terraform",
    "machine learning",
    "data engineering",
    "devops",
    "security",
    "mobile",
    "ios",
    "android",
)


def _infer_tech_tags(text: str) -> list[str]:
    lowered = text.lower()
    return [tag for tag in _TECH_TAGS if tag in lowered]


def _extract_links(tree: HTMLParser, raw_html: str) -> list[str]:
    """Absolute URLs, preferring hrefs and falling back to bare-text URLs."""
    links: dict[str, None] = {}
    for node in tree.css("a[href]"):
        href = node.attributes.get("href")
        if href and href.startswith(("http://", "https://")):
            links.setdefault(href.split("?")[0], None)
    if not links:
        for match in re.finditer(r"https?://[^\s<>\"']+", raw_html):
            links.setdefault(match.group(0).split("?")[0], None)
    return list(links)


def _extract_italics(tree: HTMLParser) -> list[str]:
    """HN wraps the employer's own wording in ``<i>`` tags."""
    parts = [text_of(node) for node in tree.css("i")]
    return [part for part in parts if part]


def posting_to_item(
    posting: HnPosting,
    *,
    comment_id: int,
    thread_id: int,
    source: str = "hnhiring",
) -> JobItem:
    """Convert a parsed posting into a storable :class:`JobItem`.

    One comment can advertise several roles; only the first is kept, with the
    rest noted in ``raw`` so nothing is silently dropped.
    """
    url = posting.links[0] if posting.links else HN_ITEM_WEB.format(item_id=comment_id)
    description = posting.description
    if len(posting.links) > 1:
        description = f"{description}\n\nLinks:\n" + "\n".join(posting.links[1:])

    return JobItem(
        source=source,
        external_id=str(comment_id),
        url=url,
        title=posting.role,
        company=posting.company,
        location=posting.location,
        remote=posting.remote,
        employment_type=posting.employment_type,
        salary_min=posting.salary_min,
        salary_max=posting.salary_max,
        salary_currency=posting.salary_currency,
        description=truncate(scrub_pii(description), MAX_COMMENT_CHARS),
        tags=posting.tags,
        posted_at=None,
        raw={
            "comment_id": comment_id,
            "thread_id": thread_id,
            "links": posting.links,
            "hn_url": HN_ITEM_WEB.format(item_id=comment_id),
        },
    )


def is_hiring_thread(title: str | None) -> bool:
    """True for the monthly "Who is hiring" / "Who wants to be hired" threads."""
    if not title:
        return False
    lowered = title.lower()
    return "who is hiring" in lowered or "who wants to be hired" in lowered


def thread_posted_at(created_at: Any) -> datetime | None:
    """Convert a Firebase ``created_at`` epoch into an aware datetime."""
    if isinstance(created_at, (int, float)):
        return datetime.fromtimestamp(created_at, tz=UTC)
    return parse_date(str(created_at)) if created_at else None
