"""Public, read-only query API for the local SQLite database."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .config import Settings
from .exceptions import AmbiguousRateError, RateNotFoundError
from .models import PerDiemRate
from .utils import date_to_fiscal_year, normalize_zip, parse_date


LOOKUP_SQL = """
SELECT l.zip_code, l.state, l.city, l.county, l.locality,
       l.primary_destination, l.destination_id, r.fiscal_year,
       r.lodging_rate, r.mie_rate, r.first_last_day_mie,
       s.agency, r.source_file, s.source_url, s.downloaded_at,
       l.is_standard
FROM rates r
JOIN locations l ON l.id = r.location_id
JOIN sources s ON s.id = r.source_id
WHERE l.zip_code = ? AND r.fiscal_year = ?
  AND r.effective_start <= ? AND r.effective_end >= ?
"""


def _database_path(database_path: Path | str | None) -> Path:
    return Path(database_path) if database_path else Settings.from_env().database_path


def get_per_diem(
    zip_code: str | int,
    date: date | datetime | str,
    *,
    database_path: Path | str | None = None,
) -> PerDiemRate:
    """Return the official federal rate for a ZIP code and travel date."""

    normalized_zip = normalize_zip(zip_code)
    travel_date = parse_date(date)
    fiscal_year = date_to_fiscal_year(travel_date)
    database = _database_path(database_path)
    if not database.exists():
        raise RateNotFoundError(
            f"Rate database not found at {database}; run the refresh command first"
        )
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            LOOKUP_SQL,
            (
                normalized_zip, fiscal_year,
                travel_date.isoformat(), travel_date.isoformat(),
            ),
        ).fetchall()
    if not rows:
        raise RateNotFoundError(
            f"No federal per diem rate for ZIP {normalized_zip} on {travel_date}; "
            "the ZIP may not be represented in the official GSA/Census source files"
        )
    if len(rows) != 1:
        localities = ", ".join(
            sorted({f"{row['locality']} ({row['state']})" for row in rows})
        )
        raise AmbiguousRateError(
            f"ZIP {normalized_zip} intersects multiple published rate localities on "
            f"{travel_date}: {localities}. A ZIP alone is insufficient; use the "
            "traveler's exact duty locality."
        )
    row = rows[0]
    return PerDiemRate(
        zip_code=row["zip_code"],
        state=row["state"],
        city=row["city"],
        county=row["county"],
        locality=row["locality"],
        primary_destination=row["primary_destination"],
        destination_id=row["destination_id"],
        fiscal_year=int(row["fiscal_year"]),
        travel_date=travel_date,
        lodging_rate=Decimal(row["lodging_rate"]),
        mie_rate=Decimal(row["mie_rate"]),
        first_last_day_mie=Decimal(row["first_last_day_mie"]),
        source_agency=row["agency"],
        source_file=row["source_file"],
        source_url=row["source_url"],
        source_retrieved_at=datetime.fromisoformat(row["downloaded_at"]),
        is_standard=bool(row["is_standard"]),
    )


def get_all_rates(
    fiscal_year: int,
    *,
    database_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return all long-format rate intervals for a fiscal year."""

    database = _database_path(database_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT l.zip_code, l.state, l.city, l.county, l.locality,
                      r.fiscal_year, r.month, r.effective_start, r.effective_end,
                      r.lodging_rate, r.mie_rate, r.first_last_day_mie,
                      s.agency AS source_agency
               FROM rates r JOIN locations l ON l.id=r.location_id
               JOIN sources s ON s.id=r.source_id
               WHERE r.fiscal_year=? ORDER BY l.state, l.zip_code, r.effective_start""",
            (fiscal_year,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_state_rates(
    state: str,
    fiscal_year: int,
    *,
    database_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return underlying locality-level records for a state and fiscal year."""

    code = state.strip().upper()
    database = _database_path(database_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT l.zip_code, l.state, l.city, l.county, l.locality,
                      r.fiscal_year, r.month, r.effective_start, r.effective_end,
                      r.lodging_rate, r.mie_rate, r.first_last_day_mie,
                      s.agency AS source_agency
               FROM rates r JOIN locations l ON l.id=r.location_id
               JOIN sources s ON s.id=r.source_id
               WHERE r.fiscal_year=? AND l.state=?
               ORDER BY l.zip_code, r.effective_start""",
            (fiscal_year, code),
        ).fetchall()
    return [dict(row) for row in rows]


def compare_states(
    states: Iterable[str],
    fiscal_year: int,
    *,
    database_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return analytical summaries without reducing state rates to one value."""

    summaries = []
    for requested in states:
        code = requested.strip().upper()
        rows = get_state_rates(code, fiscal_year, database_path=database_path)
        if not rows:
            continue
        lodging = [Decimal(row["lodging_rate"]) for row in rows]
        mie = [Decimal(row["mie_rate"]) for row in rows]
        by_locality: dict[str, set[Decimal]] = {}
        for row in rows:
            by_locality.setdefault(row["locality"], set()).add(Decimal(row["lodging_rate"]))
        summaries.append(
            {
                "state": code,
                "zip_code_count": len({row["zip_code"] for row in rows}),
                "locality_count": len(by_locality),
                "minimum_lodging_rate": min(lodging),
                "maximum_lodging_rate": max(lodging),
                "median_lodging_rate": median(lodging),
                "minimum_mie_rate": min(mie),
                "maximum_mie_rate": max(mie),
                "seasonal_locality_count": sum(len(values) > 1 for values in by_locality.values()),
            }
        )
    return summaries


def explain_rate(
    zip_code: str | int,
    date_value: date | datetime | str,
    *,
    database_path: Path | str | None = None,
) -> str:
    """Return a concise audit explanation for the selected rate."""

    rate = get_per_diem(zip_code, date_value, database_path=database_path)
    resolution = (
        "the published standard/catch-all rate"
        if rate.is_standard
        else f"the {rate.locality} federal per diem locality"
    )
    return (
        f"ZIP {rate.zip_code} mapped to {resolution}.\n\n"
        f"Travel date {rate.travel_date:%B %d, %Y} falls within FY{rate.fiscal_year}. "
        f"The applicable {rate.travel_date:%B} lodging interval and locality M&IE "
        f"rate were selected.\n\nSource: {rate.source_agency}, {rate.source_file}. "
        f"Downloaded {rate.source_retrieved_at.date().isoformat()} from {rate.source_url}"
    )
