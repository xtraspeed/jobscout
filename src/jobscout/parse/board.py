"""Declarative, config-driven job board parsing.

Adding a new job board is a YAML file, not a Python module: list/detail
selectors, pagination strategy and which fields to pull all live in config. The
same parser therefore runs against live HTTP *and* against recorded fixtures,
which is what keeps the test suite offline.

Selectors use the ``<css>::<extraction>`` form from :mod:`jobscout.parse.html`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Self
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field
from selectolax.parser import HTMLParser, Node

from jobscout.models import JobItem
from jobscout.parse.html import (
    SelectorSpec,
    absolute_url,
    extract_all,
    extract_field,
    extract_first,
)
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

DEFAULT_TAG_SEPARATORS: Final = [",", "|", ";", "·"]
MAX_DESCRIPTION_CHARS: Final = 4000


class ListSpec(BaseModel):
    """How to read one index page of listings."""

    model_config = ConfigDict(extra="forbid")

    item_selector: str
    url: str = Field(description="selector for the detail link, e.g. 'a.job::attr(href)'")
    external_id: str | None = Field(default=None, description="optional stable id selector")
    fields: dict[str, str] = Field(default_factory=dict)
    multi_fields: set[str] = Field(default_factory=lambda: {"tags"})
    tag_separators: list[str] = Field(default_factory=lambda: list(DEFAULT_TAG_SEPARATORS))


class DetailSpec(BaseModel):
    """How to read a detail page. Fields here override the list-level values."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, str] = Field(default_factory=dict)
    multi_fields: set[str] = Field(default_factory=lambda: {"tags"})
    tag_separators: list[str] = Field(default_factory=lambda: list(DEFAULT_TAG_SEPARATORS))
    remove: str | None = Field(default="script, style, noscript")


class PaginationSpec(BaseModel):
    """Either follow a "next" link or synthesise page URLs."""

    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(default=3, ge=1, le=100)
    next_selector: str | None = None
    page_url: str | None = Field(
        default=None, description="template with '{page}', e.g. '/jobs?page={page}'"
    )
    page_query_param: str | None = None


class BoardConfig(BaseModel):
    """A whole board definition, normally loaded from ``selectors/*.yml``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    start_urls: list[str] = Field(min_length=1)
    list: ListSpec
    detail: DetailSpec | None = None
    pagination: PaginationSpec = Field(default_factory=PaginationSpec)
    infer_remote: bool = True
    infer_employment_type: bool = True
    base_url: str | None = Field(default=None, description="defaults to the first start URL")

    @property
    def resolved_base_url(self) -> str:
        return self.base_url or self.start_urls[0]

    @classmethod
    def from_yaml(cls, path: str | Path) -> Self:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        return cls.model_validate(data)

    def page_url(self, page_number: int) -> str:
        """URL of list page ``page_number`` (1-based)."""
        base = self.start_urls[0]
        pagination = self.pagination
        if pagination.page_url:
            return urljoin_template(base, pagination.page_url, page=page_number)
        if pagination.page_query_param:
            return replace_query_param(base, pagination.page_query_param, page_number)
        if page_number > 1:
            raise ValueError("config defines no pagination strategy for pages > 1")
        return base


@dataclass(slots=True)
class ParsedList:
    """Result of parsing one index page."""

    detail_urls: list[str] = field(default_factory=list)
    seeds: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_urls: list[str] = field(default_factory=list)


class JobBoardParser:
    """Turns board HTML into :class:`~jobscout.models.JobItem` values."""

    def __init__(self, config: BoardConfig) -> None:
        self.config = config
        self._list_url_spec = SelectorSpec.parse(config.list.url)
        self._list_id_spec = (
            SelectorSpec.parse(config.list.external_id) if config.list.external_id else None
        )
        self._detail_specs: dict[str, SelectorSpec] = {
            name: SelectorSpec.parse(spec) for name, spec in config.list.fields.items()
        }
        if config.detail:
            self._detail_specs.update(
                {name: SelectorSpec.parse(spec) for name, spec in config.detail.fields.items()}
            )

    # -- index pages --------------------------------------------------------

    def parse_list(self, markup: str, base_url: str) -> ParsedList:
        """Extract detail URLs (plus light list-level data) and the next page."""
        tree = HTMLParser(markup)
        result = ParsedList()
        seen: set[str] = set()

        for node in tree.css(self.config.list.item_selector):
            href = extract_field(node, self.config.list.url)
            detail_url = absolute_url(base_url, href)
            if detail_url is None or detail_url in seen:
                continue
            seen.add(detail_url)
            result.detail_urls.append(detail_url)
            result.seeds[detail_url] = self._collect(node, self.config.list)

        result.next_urls = self._next_pages(tree, base_url, seen)
        return result

    def _next_pages(self, tree: HTMLParser, base_url: str, seen: set[str]) -> list[str]:
        pagination = self.config.pagination
        if pagination.next_selector:
            urls: list[str] = []
            for node in tree.css(pagination.next_selector):
                href = node.attributes.get("href")
                resolved = absolute_url(base_url, href)
                if resolved and resolved not in seen and resolved not in urls:
                    urls.append(resolved)
            return urls[:1]  # one "next" link at a time keeps depth predictable
        return []

    # -- detail pages -------------------------------------------------------

    def parse_detail(
        self, markup: str, url: str, seed: dict[str, Any] | None = None
    ) -> JobItem | None:
        """Build an item from a detail page, falling back to list-level ``seed``."""
        data: dict[str, Any] = dict(seed or {})
        if self.config.detail:
            tree = HTMLParser(markup)
            for node in tree.css(self.config.detail.remove) if self.config.detail.remove else []:
                node.decompose()
            data.update(self._collect(tree, self.config.detail))
            description_html = self._detail_html(tree)
            if description_html:
                data["description"] = description_html
        return self.build_item(url, data)

    def _detail_html(self, tree: HTMLParser) -> str:
        detail = self.config.detail
        if not detail or "description" not in detail.fields:
            return ""
        node = tree.css_first(SelectorSpec.parse(detail.fields["description"]).selector)
        return (node.html or "") if node is not None else ""

    # -- shared -------------------------------------------------------------

    def _collect(self, node: Node | HTMLParser, spec: ListSpec | DetailSpec) -> dict[str, Any]:
        """Extract a field mapping, honouring multi-value/tag fields."""
        out: dict[str, Any] = {}
        for name, selector in spec.fields.items():
            parsed = SelectorSpec.parse(selector)
            if name in spec.multi_fields:
                tags: list[str] = []
                for raw in extract_all(node, parsed):
                    tags.extend(_split_tags(raw, spec.tag_separators))
                if tags:
                    out[name] = tags
            else:
                single = extract_first(node, parsed)
                if single is not None:
                    out[name] = single
        return out

    def build_item(self, url: str, data: dict[str, Any]) -> JobItem | None:
        """Assemble a :class:`JobItem` from extracted fields.

        Returns ``None`` when title or company is missing: a row without those
        is not a listing, it is a navigation element or an ad.
        """
        title = normalise_text(data.get("title"))
        company = normalise_text(data.get("company"))
        if not title or not company:
            # A listing without a title and company is not worth storing.
            return None

        location = normalise_text(data.get("location")) or None
        summary = normalise_text(data.get("summary"))
        description_html = data.get("description") or ""
        description = html_to_text(description_html) if description_html else summary
        description = truncate(scrub_pii(description), MAX_DESCRIPTION_CHARS)

        salary_text = " ".join(
            part
            for part in (data.get("salary"), data.get("salary_min"), data.get("salary_max"))
            if part
        )
        salary_min, salary_max, currency = parse_salary(salary_text or None)

        employment_type = normalise_text(data.get("employment_type")) or None
        if employment_type is not None:
            # Canonicalise the common labels so facets group correctly, but keep
            # a board's own vocabulary when we cannot map it.
            employment_type = detect_employment_type(employment_type) or employment_type
        elif self.config.infer_employment_type:
            employment_type = detect_employment_type(title, description)

        remote = bool(data.get("remote"))
        if self.config.infer_remote and not remote:
            remote = looks_remote(location, title, description)

        posted_at: datetime | None = parse_date(data.get("posted_at"))

        tags = [normalise_text(tag) for tag in data.get("tags", []) if normalise_text(tag)]
        if remote and "remote" not in {tag.lower() for tag in tags}:
            tags.append("remote")

        return JobItem(
            source=self.config.name,
            external_id=self._external_id(url, data),
            url=url,
            title=title,
            company=company,
            location=location,
            remote=remote,
            employment_type=employment_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            description=description,
            tags=tags,
            posted_at=posted_at,
            raw={k: v for k, v in data.items() if k not in {"description"}},
        )

    def _external_id(self, url: str, data: dict[str, Any]) -> str:
        explicit = data.get("external_id")
        if explicit:
            return str(explicit)[:255]
        # Stable per-URL identifier. Not a security hash, so BLAKE2b is both
        # faster and free of SHA-1's collision weaknesses.
        digest = hashlib.blake2b(url.encode("utf-8"), digest_size=12)
        return digest.hexdigest()


# ------------------------------------------------------------------ helpers ---


def _split_tags(value: str, separators: list[str]) -> list[str]:
    if not separators:
        cleaned = normalise_text(value)
        return [cleaned] if cleaned else []
    pattern = "|".join(re.escape(sep) for sep in separators)
    return [normalise_text(part) for part in re.split(pattern, value) if normalise_text(part)]


def urljoin_template(base: str, template: str, **params: Any) -> str:
    """Join a path template (``/jobs?page={page}``) onto a base URL."""
    return urljoin(base, template.format(**params))


def replace_query_param(url: str, param: str, value: Any) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[param] = str(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
