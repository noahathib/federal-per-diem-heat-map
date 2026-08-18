"""Shared deterministic parsing and normalization helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .exceptions import InvalidZipCodeError


MONEY_QUANTUM = Decimal("0.01")


def parse_date(value: date | datetime | str) -> date:
    """Parse an ISO date or return a date value unchanged."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc
    raise TypeError(f"Unsupported date type: {type(value).__name__}")


def date_to_fiscal_year(value: date | datetime | str) -> int:
    """Return the federal fiscal year containing *value*."""

    parsed = parse_date(value)
    return parsed.year + (1 if parsed.month >= 10 else 0)


def normalize_zip(value: str | int) -> str:
    """Normalize a five-digit ZIP or ZIP+4 without losing leading zeros."""

    if isinstance(value, bool):
        raise InvalidZipCodeError("Boolean values are not ZIP codes")
    text = str(value).strip()
    if re.fullmatch(r"\d{5}", text):
        return text
    if re.fullmatch(r"\d{5}-\d{4}", text):
        return text[:5]
    if isinstance(value, int) and 0 <= value <= 99999:
        return f"{value:05d}"
    raise InvalidZipCodeError(
        f"Invalid ZIP code {value!r}; expected five digits or ZIP+4"
    )


def money(value: Any, *, allow_none: bool = False) -> Decimal | None:
    """Parse a numeric currency value to a two-decimal Decimal."""

    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_none:
            return None
        raise ValueError("Currency value is empty")
    text = str(value).strip().replace("$", "").replace(",", "")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Malformed currency value: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"Non-finite currency value: {value!r}")
    return parsed.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def first_last_day(mie_rate: Decimal) -> Decimal:
    """Calculate the statutory 75-percent first/last travel-day M&IE."""

    return (mie_rate * Decimal("0.75")).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP
    )


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file without loading it all in memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snake_case(value: str) -> str:
    """Convert a source column label to a stable snake_case name."""

    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip()).strip("_")
    return text.lower()


def fold_name(value: str | None) -> str:
    """Return an accent-folded uppercase key for comparing published names.

    Census publishes Spanish and Hawaiian place names with diacritics and the
    Hawaiian okina; DTMO publishes the same names in plain ASCII. Decomposing to
    NFKD and dropping combining marks lets "Bayamon" match "Bayamon" and
    "Lihue" match "LIHUE" without hand-listing every accented spelling.
    """

    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    unaccented = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    # The Hawaiian okina and apostrophes sit inside a word, so they are deleted
    # rather than turned into a separator: Lihuʻe must fold to LIHUE, while
    # "Lihue (East)" still folds to the two words LIHUE EAST.
    unaccented = re.sub(r"[ʻʼ‘’']", "", unaccented)
    return re.sub(r"[^A-Z0-9]+", " ", unaccented.upper()).strip()


def clean_geo_name(value: str | None) -> str | None:
    """Remove Census legal-area suffixes while preserving the locality name."""

    if not value:
        return None
    text = re.sub(
        r"\s+(city and borough|municipality|consolidated government|city|town|"
        r"village|borough|zona urbana|comunidad|CDP)$",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip()


def strip_county_suffix(value: str | None) -> str:
    """Drop the Census legal-area word from a county-equivalent name.

    Counties are stored exactly as published for display, so the suffix is
    removed only when matching a name against a DTMO locality. The territories
    use Municipio (Puerto Rico), Island (U.S. Virgin Islands), Municipality
    (Northern Mariana Islands), and District (American Samoa).
    """

    if not value:
        return ""
    return re.sub(
        r"\s+(municipio|municipality|island|district|county|borough|census area|"
        r"city and borough|parish)$",
        "",
        str(value).strip(),
        flags=re.IGNORECASE,
    ).strip()


def month_range(year: int, month: int) -> tuple[date, date]:
    """Return inclusive first and last dates for a calendar month."""

    start = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return start, date.fromordinal(next_month.toordinal() - 1)

