"""Structural, logical, coverage, and regression validation."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Iterable

from .models import NormalizedRate, ValidationReport
from .utils import date_to_fiscal_year, first_last_day, month_range


STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}

# ZIP-addressable U.S. territories, priced by DTMO alongside Alaska and Hawaii.
TERRITORY_CODES = {"AS", "GU", "MP", "PR", "VI"}

# Every non-CONUS area that a complete dataset must cover. Alaska and Hawaii
# were required before the territories were ingested; all seven are required now
# so that a DTMO publication or Census relationship change that silently drops
# one fails the refresh instead of shrinking the dataset.
REQUIRED_NON_CONUS_CODES = ("AK", "HI", "AS", "GU", "MP", "PR", "VI")


def validate_rates(
    records: Iterable[NormalizedRate],
    *,
    expected_fiscal_year: int,
    previous_database: Path | None = None,
    require_all_states: bool = True,
) -> ValidationReport:
    """Validate canonical records without mutating the existing database."""

    report = ValidationReport()
    seen: set[tuple[object, ...]] = set()
    coverage: dict[tuple[str, str, int], list[tuple[date, date]]] = defaultdict(list)
    states: set[str] = set()
    zip_codes: set[str] = set()
    localities: set[tuple[str, str]] = set()
    agencies: set[str] = set()
    standard_count = 0
    record_count = 0
    lodging_values: list[Decimal] = []

    for record in records:
        record_count += 1
        states.add(record.state)
        zip_codes.add(record.zip_code)
        localities.add((record.state, record.locality))
        agencies.add(record.source_agency)
        lodging_values.append(record.lodging_rate)
        standard_count += int(record.source_agency == "GSA" and record.is_standard)
        if not re.fullmatch(r"\d{5}", record.zip_code):
            report.add("error", "invalid_zip", f"Invalid ZIP: {record.zip_code}")
        if record.state not in STATE_CODES | TERRITORY_CODES | {"DC"}:
            report.add("error", "invalid_state", f"Invalid state: {record.state}")
        if record.fiscal_year != expected_fiscal_year:
            report.add(
                "error", "wrong_fiscal_year",
                f"{record.zip_code} has FY{record.fiscal_year}; expected FY{expected_fiscal_year}",
            )
        if record.month not in range(1, 13):
            report.add("error", "invalid_month", f"Invalid month: {record.month}")
        if record.lodging_rate <= 0 or record.mie_rate <= 0:
            report.add(
                "error", "nonpositive_rate",
                f"{record.zip_code} has lodging={record.lodging_rate}, M&IE={record.mie_rate}",
            )
        if record.first_last_day_mie != first_last_day(record.mie_rate):
            report.add(
                "error", "first_last_mie",
                f"{record.zip_code} first/last M&IE does not equal 75 percent",
            )
        if record.effective_start > record.effective_end:
            report.add("error", "date_order", f"Invalid interval for {record.zip_code}")
        if (
            date_to_fiscal_year(record.effective_start) != expected_fiscal_year
            or date_to_fiscal_year(record.effective_end) != expected_fiscal_year
        ):
            report.add(
                "error", "interval_fiscal_year",
                f"{record.zip_code} interval is outside FY{expected_fiscal_year}",
            )
        if record.effective_start.month != record.month or record.effective_end.month != record.month:
            report.add(
                "error", "interval_month",
                f"{record.zip_code} interval crosses canonical months",
            )
        key = (
            record.zip_code, record.fiscal_year, record.effective_start,
            record.effective_end, record.destination_id,
        )
        if key in seen:
            report.add("error", "duplicate", f"Duplicate rate record: {key}")
        seen.add(key)
        coverage[(record.zip_code, record.destination_id, record.month)].append(
            (record.effective_start, record.effective_end)
        )

    if record_count == 0:
        report.add("error", "empty_dataset", "No normalized records were produced")
    if require_all_states:
        missing_states = STATE_CODES - states
        if missing_states:
            report.add(
                "error", "missing_states",
                f"Missing states: {', '.join(sorted(missing_states))}",
            )
        for required in REQUIRED_NON_CONUS_CODES:
            if required not in states:
                report.add("error", "missing_oconus_state", f"{required} is absent")
        if standard_count == 0:
            report.add(
                "error", "missing_standard_conus", "No standard CONUS fallback rows exist"
            )
        if not {"GSA", "DoD/DTMO"}.issubset(agencies):
            report.add(
                "error", "missing_agency",
                f"Expected GSA and DoD/DTMO records; found {sorted(agencies)}",
            )

    expected_months = set(range(1, 13))
    months_by_location: dict[tuple[str, str], set[int]] = defaultdict(set)
    for zip_code, destination_id, month in coverage:
        months_by_location[(zip_code, destination_id)].add(month)
    for (zip_code, destination_id), months in months_by_location.items():
        if months != expected_months:
            report.add(
                "error", "month_coverage",
                f"{zip_code}/{destination_id} is missing months "
                f"{sorted(expected_months - months)}",
            )
    for (zip_code, destination_id, month), intervals in coverage.items():
        calendar_year = expected_fiscal_year - 1 if month >= 10 else expected_fiscal_year
        expected_start, expected_end = month_range(calendar_year, month)
        ordered = sorted(intervals)
        cursor = expected_start
        for start, end in ordered:
            if start != cursor:
                report.add(
                    "error", "date_gap_overlap",
                    f"{zip_code}/{destination_id}/{month} expected {cursor}, "
                    f"found interval starting {start}",
                )
                break
            cursor = date.fromordinal(end.toordinal() + 1)
        if cursor != date.fromordinal(expected_end.toordinal() + 1):
            report.add(
                "error", "date_gap_overlap",
                f"{zip_code}/{destination_id}/{month} does not cover through {expected_end}",
            )

    report.metrics.update(
        {
            "record_count": record_count,
            "zip_code_count": len(zip_codes),
            "state_count": len(states & STATE_CODES),
            "territory_count": len(states & TERRITORY_CODES),
            "states": sorted(states),
            "locality_count": len(localities),
            "standard_conus_record_count": standard_count,
            "agencies": sorted(agencies),
            "median_lodging_rate": str(median(lodging_values)) if lodging_values else None,
        }
    )
    if previous_database and previous_database.exists():
        _add_regression_findings(report, previous_database, record_count, states, zip_codes)
    return report


def _add_regression_findings(
    report: ValidationReport,
    previous_database: Path,
    new_count: int,
    new_states: set[str],
    new_zips: set[str],
) -> None:
    try:
        with closing(sqlite3.connect(previous_database)) as connection:
            old_count = connection.execute("SELECT COUNT(*) FROM rates").fetchone()[0]
            old_states = {
                row[0] for row in connection.execute("SELECT DISTINCT state FROM locations")
            }
            old_zips = {
                row[0] for row in connection.execute("SELECT DISTINCT zip_code FROM locations")
            }
    except sqlite3.Error as exc:
        report.add("warning", "regression_unavailable", f"Cannot compare old database: {exc}")
        return
    if old_count and (new_count / old_count < 0.7 or new_count / old_count > 1.5):
        report.add(
            "warning", "record_count_change",
            f"Record count changed from {old_count:,} to {new_count:,}",
        )
    disappeared_states = old_states - new_states
    if disappeared_states:
        report.add(
            "error", "disappearing_states",
            f"States disappeared: {sorted(disappeared_states)}",
        )
    if old_zips and len(old_zips - new_zips) / len(old_zips) > 0.1:
        report.add(
            "warning", "missing_zip_mappings",
            f"{len(old_zips - new_zips):,} prior ZIP mappings disappeared",
        )


def validate_database(path: Path | str) -> ValidationReport:
    """Run integrity and minimum-schema checks on an existing SQLite file."""

    report = ValidationReport()
    database = Path(path)
    if not database.exists():
        report.add("error", "missing_database", f"Database not found: {database}")
        return report
    try:
        with closing(sqlite3.connect(database)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                report.add("error", "sqlite_integrity", str(integrity))
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {"rates", "locations", "sources", "refresh_history"}
            if missing := required - tables:
                report.add("error", "missing_tables", f"Missing tables: {sorted(missing)}")
                return report
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in required
            }
            report.metrics.update(counts)
            if counts["rates"] == 0 or counts["locations"] == 0:
                report.add("error", "empty_database", "Rates or locations table is empty")
            orphan_count = connection.execute(
                "SELECT COUNT(*) FROM rates r LEFT JOIN locations l ON l.id=r.location_id "
                "WHERE l.id IS NULL"
            ).fetchone()[0]
            if orphan_count:
                report.add("error", "orphan_rates", f"{orphan_count} rates lack locations")
    except sqlite3.Error as exc:
        report.add("error", "sqlite_error", str(exc))
    return report
