"""Pure parsing/normalisation helpers used by adapters."""

from __future__ import annotations

from jobscout.parse.html import (
    SelectorSpec,
    absolute_url,
    extract_all,
    extract_field,
    normalise_ws,
    text_of,
)
from jobscout.parse.text import (
    detect_currency,
    detect_employment_type,
    html_to_text,
    looks_remote,
    parse_date,
    parse_money,
    parse_salary,
    scrub_pii,
    truncate,
)

__all__ = [
    "SelectorSpec",
    "absolute_url",
    "detect_currency",
    "detect_employment_type",
    "extract_all",
    "extract_field",
    "html_to_text",
    "looks_remote",
    "normalise_ws",
    "parse_date",
    "parse_money",
    "parse_salary",
    "scrub_pii",
    "text_of",
    "truncate",
]
