"""Parser tests against recorded HTML: selectors, salary/date parsing, scrubbing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobscout.parse.board import BoardConfig, JobBoardParser
from jobscout.parse.html import SelectorError, SelectorSpec, absolute_url, extract_field
from jobscout.parse.text import (
    detect_employment_type,
    html_to_text,
    looks_remote,
    parse_date,
    parse_money,
    parse_salary,
    scrub_pii,
    truncate,
)

DEMO_BOARD = Path(__file__).parent / "fixtures" / "demo_board"


# ------------------------------------------------------------ selector spec --


@pytest.mark.parametrize(
    ("spec", "selector", "extraction"),
    [
        ("h1::text", "h1", "text"),
        ("a.job::attr(href)", "a.job", "attr(href)"),
        ("  time::attr(data-x)  ", "time", "attr(data-x)"),
    ],
)
def test_selector_spec_parsing(spec: str, selector: str, extraction: str) -> None:
    parsed = SelectorSpec.parse(spec)
    assert parsed.selector == selector
    assert parsed.extraction == extraction


@pytest.mark.parametrize("spec", ["h1", "h1::nope", "::text"])
def test_selector_spec_rejects_bad_input(spec: str) -> None:
    with pytest.raises(SelectorError):
        SelectorSpec.parse(spec)


def test_extract_field_on_missing_node_returns_none() -> None:
    assert extract_field("<div></div>", "span.gone::text") is None


@pytest.mark.parametrize(
    ("base", "href", "expected"),
    [
        ("https://a.test/list", "/jobs/1", "https://a.test/jobs/1"),
        ("https://a.test/list", "jobs/1", "https://a.test/jobs/1"),
        ("https://a.test/list", "#top", None),
        ("https://a.test/list", "javascript:void(0)", None),
        ("https://a.test/list", "mailto:a@b.test", None),
        ("https://a.test/list", "", None),
    ],
)
def test_absolute_url(base: str, href: str, expected: str | None) -> None:
    assert absolute_url(base, href) == expected


# ------------------------------------------------------------- text helpers --


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$120,000", 120_000.0),
        ("120k", 120_000.0),
        ("1.5M", 1_500_000.0),
        ("45 000", 45_000.0),
        ("", None),
        ("no digits here", None),
    ],
)
def test_parse_money(text: str, expected: float | None) -> None:
    assert parse_money(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$120k - $180k USD", (120_000.0, 180_000.0, "USD")),
        ("€85,000 – €105,000", (85_000.0, 105_000.0, "EUR")),
        ("£45,000", (45_000.0, 45_000.0, "GBP")),
        ("100-150 EUR per hour", (100.0, 150.0, "EUR")),
        ("$180k – $220k plus equity", (180_000.0, 220_000.0, "USD")),
        ("Competitive", (None, None, None)),
        ("", (None, None, None)),
    ],
)
def test_parse_salary(text: str, expected: tuple[float | None, float | None, str | None]) -> None:
    assert parse_salary(text) == expected


def test_parse_salary_normalises_inverted_range() -> None:
    assert parse_salary("$200k - $100k") == (100_000.0, 200_000.0, "USD")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-02-11", datetime(2026, 2, 11, tzinfo=UTC)),
        ("Feb 11, 2026", datetime(2026, 2, 11, tzinfo=UTC)),
        ("11 February 2026", datetime(2026, 2, 11, tzinfo=UTC)),
        ("02/11/2026", datetime(2026, 2, 11, tzinfo=UTC)),
        # Slash dates are read as US M/D/Y when both components are <= 12.
        ("11/02/2026", datetime(2026, 11, 2, tzinfo=UTC)),
        # ...and as D/M/Y when the first component cannot be a month.
        ("25/12/2026", datetime(2026, 12, 25, tzinfo=UTC)),
        ("not a date", None),
        ("2026-13-45", None),
    ],
)
def test_parse_date(text: str, expected: datetime | None) -> None:
    assert parse_date(text) == expected


def test_html_to_text_keeps_paragraphs() -> None:
    markup = "<div><p>First para</p><p>Second   para</p><script>bad()</script></div>"

    text = html_to_text(markup)

    assert "First para" in text
    assert "Second para" in text
    assert "bad()" not in text


def test_html_to_text_decodes_entities() -> None:
    assert html_to_text("<p>caf&eacute; &amp; bar &mdash; baz</p>").startswith("caf")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Backend Engineer, Remote (EU)", True),
        ("Fully remote", True),
        ("Berlin, Germany", False),
        ("Hybrid", False),
        (None, False),
    ],
)
def test_looks_remote(text: str | None, expected: bool) -> None:
    assert looks_remote(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Full-time", "full-time"),
        ("Contract role", "contract"),
        ("Summer internship", "internship"),
        ("Freelance", "contract"),
        ("Something else entirely", None),
    ],
)
def test_detect_employment_type(text: str, expected: str | None) -> None:
    assert detect_employment_type(text) == expected


def test_scrub_pii_removes_contact_details() -> None:
    text = "Apply to jobs@northwind.example or call +49 30 1234 5678. Ref 123456789."

    scrubbed = scrub_pii(text)

    assert "jobs@northwind.example" not in scrubbed
    assert "1234 5678" not in scrubbed
    assert "123456789" not in scrubbed
    assert "Apply to" in scrubbed


def test_scrub_pii_keeps_bare_url_path() -> None:
    assert scrub_pii("see https://careers.test/jobs/1?ref=hn") == "see https://careers.test/jobs/1"


def test_truncate_marks_ellipsis() -> None:
    assert truncate("abcdefghij", limit=5) == "abcd…"
    assert truncate("abc", limit=5) == "abc"


# ------------------------------------------------------------- board parser --


@pytest.fixture
def parser() -> JobBoardParser:
    return JobBoardParser(BoardConfig.from_yaml(DEMO_BOARD / "board.yml"))


def test_parse_list_finds_every_listing_and_next_page(parser: JobBoardParser) -> None:
    markup = (DEMO_BOARD / "list-1.html").read_text(encoding="utf-8")

    parsed = parser.parse_list(markup, "https://demo-board.example/jobs?page=1")

    assert len(parsed.detail_urls) == 3, "the promo card has no title and must be ignored"
    assert parsed.detail_urls[0] == "https://demo-board.example/jobs/1001"
    assert parsed.next_urls == ["https://demo-board.example/jobs?page=2"]


def test_parse_list_seeds_carry_index_level_fields(parser: JobBoardParser) -> None:
    markup = (DEMO_BOARD / "list-1.html").read_text(encoding="utf-8")

    parsed = parser.parse_list(markup, "https://demo-board.example/jobs?page=1")
    seed = parsed.seeds["https://demo-board.example/jobs/1001"]

    assert seed["title"] == "Senior Backend Engineer"
    assert seed["company"] == "Northwind Analytics"
    assert seed["tags"] == ["Python", "PostgreSQL", "Kubernetes"]


def test_parse_detail_merges_and_enriches(parser: JobBoardParser) -> None:
    markup = (DEMO_BOARD / "detail-1001.html").read_text(encoding="utf-8")
    seed = {
        "title": "Senior Backend Engineer",
        "company": "Northwind Analytics",
        "tags": ["Python"],
    }

    item = parser.parse_detail(markup, "https://demo-board.example/jobs/1001", seed=seed)

    assert item is not None
    assert item.title == "Senior Backend Engineer"
    # Detail page wins over the index seed.
    assert item.location == "Berlin, Germany (Hybrid)"
    assert item.salary_min == 85_000.0
    assert item.salary_max == 105_000.0
    assert item.salary_currency == "EUR"
    assert item.posted_at == datetime(2026, 2, 11, tzinfo=UTC)
    assert "ingestion pipeline" in item.description
    assert "Kafka" in item.tags
    assert item.employment_type == "full-time"


def test_parse_detail_scrubs_pii_from_description(parser: JobBoardParser) -> None:
    markup = (DEMO_BOARD / "detail-1001.html").read_text(encoding="utf-8")
    seed = {"title": "Senior Backend Engineer", "company": "Northwind Analytics"}

    item = parser.parse_detail(markup, "https://demo-board.example/jobs/1001", seed=seed)

    assert item is not None
    assert "jobs@northwind.example" not in item.description
    assert "1234 5678" not in item.description


def test_remote_inference_from_text(parser: JobBoardParser) -> None:
    markup = (DEMO_BOARD / "detail-1002.html").read_text(encoding="utf-8")
    seed = {"title": "Staff Frontend Engineer", "company": "Contoso Labs"}

    item = parser.parse_detail(markup, "https://demo-board.example/jobs/1002", seed=seed)

    assert item is not None
    assert item.remote is True
    assert "remote" in item.tags
    assert item.salary_currency == "USD"


def test_build_item_requires_title_and_company(parser: JobBoardParser) -> None:
    assert parser.build_item("https://x.test/1", {"title": "A role"}) is None
    assert parser.build_item("https://x.test/1", {"company": "Acme"}) is None
    assert parser.build_item("https://x.test/1", {}) is None


def test_external_id_is_stable_and_url_derived(parser: JobBoardParser) -> None:
    first = parser.build_item("https://x.test/jobs/1", {"title": "T", "company": "C"})
    second = parser.build_item("https://x.test/jobs/1", {"title": "T", "company": "C"})

    assert first is not None and second is not None
    assert first.external_id == second.external_id
    assert first.external_id != ""


def test_content_hash_ignores_timestamp_but_tracks_content() -> None:
    from jobscout.models import JobItem

    base = JobItem(
        source="s",
        external_id="1",
        url="https://x.test/1",
        title="T",
        company="C",
        description="D",
    )
    same = base.model_copy(update={"raw": {"different": True}})
    changed = base.model_copy(update={"description": "D2"})

    assert base.content_hash == same.content_hash
    assert base.content_hash != changed.content_hash


def test_pagination_template_and_query_param() -> None:
    config = BoardConfig.from_yaml(DEMO_BOARD / "board.yml")
    assert config.page_url(1).endswith("page=1")

    templated = config.model_copy(
        update={
            "pagination": config.pagination.model_copy(
                update={"page_url": "/jobs?p={page}", "next_selector": None}
            )
        }
    )
    assert templated.page_url(3) == "https://demo-board.example/jobs?p=3"


def test_page_url_raises_without_pagination_strategy() -> None:
    config = BoardConfig.from_yaml(DEMO_BOARD / "board.yml")
    single = config.model_copy(
        update={"pagination": config.pagination.model_copy(update={"max_pages": 1})}
    )

    with pytest.raises(ValueError):
        single.page_url(2)
