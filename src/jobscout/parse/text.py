"""Text normalisation, salary parsing, date parsing and PII scrubbing.

Everything here is pure and unit-testable; adapters stay thin.
"""

from __future__ import annotations

import html as _html
import re
from datetime import UTC, datetime
from typing import Final

# --- whitespace / html noise ---------------------------------------------

_MULTI_WS: Final = re.compile(r"\s+")
_SCRIPT_STYLE: Final = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAGS: Final = re.compile(r"<[^>]+>")
_BLOCK_BREAK: Final = re.compile(r"</(p|div|li|tr|h[1-6]|br)\s*/?>", re.IGNORECASE)
_BR: Final = re.compile(r"<br\s*/?>", re.IGNORECASE)

# --- salary ----------------------------------------------------------------

#: One money token: optional currency, a number, an optional k/m multiplier.
#: Token-based rather than range-regex-based because postings write ranges every
#: way imaginable ("$120k-180k", "$120k - $180k", "120,000 to 180,000").
#: The number pattern accepts comma, dot and space thousands grouping, so both
#: "120,000" (en-US) and "45 000" (en-GB/de-DE) parse correctly.
_MONEY_TOKEN: Final = re.compile(
    r"(?P<cur>[$£€₹]|\b(?:usd|eur|gbp|cad|aud|inr|chf|sek|nok|dkk|pln|ils|brl|jpy)\b)?"
    r"\s*(?P<num>\d{1,3}(?:[,\u00a0 ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<mult>[kKmM])?\b",
    re.IGNORECASE,
)
_CURRENCY_SYMBOLS: Final[dict[str, str]] = {
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
    "₹": "INR",
}
_CURRENCY_CODE: Final = re.compile(
    r"\b(usd|eur|gbp|cad|aud|inr|chf|sek|nok|dkk|pln|ils|brl|jpy)\b", re.I
)
_MULTIPLIER: Final = {"k": 1_000.0, "m": 1_000_000.0}
#: A bare "100" is ambiguous; require an explicit salary word, a currency
#: symbol/code, or a range separator before trusting any digits.
_SALARY_HINT: Final = re.compile(
    r"\b(?:salary|comp(?:ensation)?|pay|rate|base|per year|per hour|/hour|/yr|per month)\b"
    r"|[$£€₹]"
    r"|\b(?:usd|eur|gbp|cad|aud|inr|chf|sek|nok|dkk|pln|ils|brl|jpy)\b"
    r"|\d\s*(?:-|–|—|\bto\b)\s*[\d$£€₹]",
    re.IGNORECASE,
)

# --- dates -----------------------------------------------------------------

_DATE_ISO: Final = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_US: Final = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_MONTH_NAMES: Final[tuple[str, ...]] = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
#: Full names plus 3-letter abbreviations, since postings use both.
_MONTHS: Final[dict[str, int]] = {name: number for number, name in enumerate(_MONTH_NAMES, start=1)}
_MONTH_LOOKUP: Final[dict[str, int]] = {
    **_MONTHS,
    **{name[:3]: number for number, name in enumerate(_MONTH_NAMES, start=1)},
}
_MONTH_ALTERNATION: Final = "|".join(sorted(_MONTH_LOOKUP, key=len, reverse=True))
#: "February 11, 2026" / "Feb 11 2026"
_DATE_MONTH_FIRST: Final = re.compile(
    rf"\b({_MONTH_ALTERNATION})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
    re.IGNORECASE,
)
#: "11 February 2026" / "11th Feb 2026"
_DATE_DAY_FIRST: Final = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALTERNATION})\.?,?\s+(\d{{4}})\b",
    re.IGNORECASE,
)

# --- classification keywords ----------------------------------------------

_REMOTE_RE: Final = re.compile(
    r"\b(remote|work from home|wfh|distributed|anywhere|fully remote)\b", re.I
)
_EMPLOYMENT_TYPES: Final[dict[str, str]] = {
    "full-time": "full-time",
    "full time": "full-time",
    "fulltime": "full-time",
    "part-time": "part-time",
    "part time": "part-time",
    "contract": "contract",
    "contractor": "contract",
    "freelance": "contract",
    "internship": "internship",
    "intern": "internship",
    "temporary": "temporary",
}
_INTERNSHIP_RE: Final = re.compile(r"\b(intern|internship|co-?op|apprentice)\b", re.I)

# --- PII scrubbing ---------------------------------------------------------

_EMAIL: Final = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
#: Phone numbers vary wildly by country, so candidate spans are matched loosely
#: and then validated by counting digits (see `_redact_phones`).
_PHONE_CANDIDATE: Final = re.compile(r"(?<![\w$+])\+?\d[\d\s()./-]{5,24}\d(?![\w])")
_ISO_DATE: Final = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_URL_BARE: Final = re.compile(r"\bhttps?://\S+")
_LONG_DIGITS: Final = re.compile(r"\b\d{9,}\b")

#: Digit counts that a real phone number can plausibly have (E.164 allows 15).
_PHONE_MIN_DIGITS: Final = 7
_PHONE_MAX_DIGITS: Final = 15

REDACTED = "[redacted]"


def normalise_text(value: str | None) -> str:
    """Strip embedded scripts, decode HTML entities and collapse whitespace.

    Uses the stdlib unescaper rather than a hand-rolled entity table: postings
    are full of ``&euro;``/``&rsquo;``/``&#8212;`` and a partial table silently
    corrupts whatever it misses.
    """
    if not value:
        return ""
    text = _SCRIPT_STYLE.sub(" ", value)
    return _MULTI_WS.sub(" ", _html.unescape(text)).strip()


def html_to_text(markup: str | None) -> str:
    """Convert an HTML fragment to readable plain text.

    Block-level closing tags become newlines first so paragraphs survive, then
    remaining tags are dropped and entities/whitespace are normalised.
    """
    if not markup:
        return ""
    text = _SCRIPT_STYLE.sub(" ", markup)
    text = _BLOCK_BREAK.sub("\n", text)
    text = _BR.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = _html.unescape(text)
    lines = [normalise_text(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_money(value: float | str) -> float | None:
    """Parse the first money token in ``value`` into a float.

    Handles ``"120,000"``, ``"$120k"``, ``"1.5M"``, ``"45 000"`` and bare
    numbers. Returns ``None`` when there is no number to read.
    """
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip()
    if not cleaned:
        return None
    match = _MONEY_TOKEN.search(cleaned)
    if not match:
        return None
    try:
        number = float(match.group("num").replace(",", "").replace("\u00a0", "").replace(" ", ""))
    except ValueError:  # pragma: no cover - the regex guarantees a parseable number
        return None
    return round(number * _MULTIPLIER.get((match.group("mult") or "").lower(), 1.0), 2)


def detect_currency(text: str | None) -> str | None:
    """Best-effort currency code detection from free text."""
    if not text:
        return None
    code_match = _CURRENCY_CODE.search(text)
    if code_match:
        return code_match.group(1).upper()
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return None


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Extract ``(min, max, currency)`` from free-form salary text.

    Requires a currency symbol, a currency code, or an explicit salary keyword
    before trusting any digits, because bare numbers in a job post are far more
    likely to be an experience requirement or a headcount than a salary.

    Two or more money tokens are treated as a range (and normalised if written
    backwards); a single token fills both bounds.
    """
    if not text:
        return None, None, None
    currency = detect_currency(text)
    if currency is None and not _SALARY_HINT.search(text):
        return None, None, None

    tokens: list[float] = []
    for match in _MONEY_TOKEN.finditer(text):
        raw = match.group("num").replace(",", "").replace("\u00a0", "").replace(" ", "")
        try:
            number = float(raw)
        except ValueError:  # pragma: no cover - the regex guarantees a number
            continue
        tokens.append(round(number * _MULTIPLIER.get((match.group("mult") or "").lower(), 1.0), 2))

    if not tokens:
        return None, None, currency
    if len(tokens) == 1:
        return tokens[0], tokens[0], currency
    low, high = tokens[0], tokens[1]
    return (low, high, currency) if low <= high else (high, low, currency)


def parse_date(text: str | None) -> datetime | None:
    """Parse the date formats job listings actually use, returning UTC.

    Slash-separated dates are read as US ``M/D/Y`` unless the first component
    exceeds 12, in which case the value is treated as ``D/M/Y``. Full and
    abbreviated month names are both accepted.
    """
    if not text:
        return None
    candidate = text.strip()

    iso = _DATE_ISO.search(candidate)
    if iso:
        return _safe_datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    words = _DATE_MONTH_FIRST.search(candidate)
    if words:
        month = _MONTH_LOOKUP[words.group(1).lower()]
        return _safe_datetime(int(words.group(3)), month, int(words.group(2)))

    day_first = _DATE_DAY_FIRST.search(candidate)
    if day_first:
        month = _MONTH_LOOKUP[day_first.group(2).lower()]
        return _safe_datetime(int(day_first.group(3)), month, int(day_first.group(1)))

    us = _DATE_US.search(candidate)
    if us:
        first, second, year = int(us.group(1)), int(us.group(2)), int(us.group(3))
        if first > 12 >= second:  # unambiguous D/M/Y
            month, day = second, first
        else:
            month, day = first, second
        return _safe_datetime(year, month, day)

    try:  # last resort: ISO 8601 with a time component
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _safe_datetime(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def looks_remote(*texts: str | None) -> bool:
    """True when any of the supplied text mentions remote work."""
    return any(text and _REMOTE_RE.search(text) for text in texts)


def detect_employment_type(*texts: str | None) -> str | None:
    """Infer ``full-time``/``contract``/``internship``/... from text."""
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        for needle, label in _EMPLOYMENT_TYPES.items():
            if needle in lowered:
                return label
        if _INTERNSHIP_RE.search(lowered):
            return "internship"
    return None


def _redact_phones(text: str) -> str:
    """Redact digit runs that look like phone numbers.

    A candidate span is only redacted when it holds 7-15 digits and is not an
    ISO date, which keeps ``2026-02-11`` and ``€85,000`` intact.
    """

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0).strip()
        digits = sum(character.isdigit() for character in candidate)
        if _PHONE_MIN_DIGITS <= digits <= _PHONE_MAX_DIGITS and not _ISO_DATE.match(candidate):
            return REDACTED
        return match.group(0)

    return _PHONE_CANDIDATE.sub(replace, text)


def scrub_pii(text: str | None) -> str:
    """Redact emails, phone numbers and bare URLs from stored text.

    Public job posts frequently include personal contact details. JobScout keeps
    company-level postings only, so identifiers are removed at parse time and
    never reach the database.
    """
    if not text:
        return ""
    scrubbed = _EMAIL.sub(REDACTED, text)
    scrubbed = _redact_phones(scrubbed)
    scrubbed = _LONG_DIGITS.sub(REDACTED, scrubbed)
    scrubbed = _URL_BARE.sub(lambda m: m.group(0).split("?")[0], scrubbed)
    return normalise_text(scrubbed)


def truncate(text: str, limit: int = 4000) -> str:
    """Cap stored description length to keep rows small and predictable."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
