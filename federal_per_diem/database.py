"""SQLite schema, bulk loading, exports, and known-good promotion."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import Settings
from .exceptions import DataValidationError
from .models import NormalizedRate, SourceMetadata, ValidationReport
from .validation import validate_database


SCHEMA_VERSION = 1


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    agency TEXT NOT NULL,
    dataset_name TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    downloaded_at TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    parser_version TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    UNIQUE(filename, sha256)
);
CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    zip_code TEXT NOT NULL CHECK(length(zip_code) = 5),
    state TEXT NOT NULL CHECK(length(state) = 2),
    city TEXT,
    county TEXT,
    primary_destination TEXT,
    locality TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    is_standard INTEGER NOT NULL CHECK(is_standard IN (0, 1)),
    UNIQUE(zip_code, state, locality, destination_id)
);
CREATE TABLE rates (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    fiscal_year INTEGER NOT NULL,
    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
    effective_start TEXT NOT NULL,
    effective_end TEXT NOT NULL,
    lodging_rate TEXT NOT NULL,
    mie_rate TEXT NOT NULL,
    first_last_day_mie TEXT NOT NULL,
    source_file TEXT NOT NULL,
    UNIQUE(location_id, fiscal_year, effective_start, effective_end)
);
CREATE TABLE refresh_history (
    id INTEGER PRIMARY KEY,
    fiscal_year INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    source_count INTEGER NOT NULL,
    validation_json TEXT NOT NULL
);
CREATE INDEX idx_locations_zip ON locations(zip_code);
CREATE INDEX idx_locations_state ON locations(state);
CREATE INDEX idx_rates_lookup ON rates(fiscal_year, effective_start, effective_end);
CREATE INDEX idx_rates_location_fy_month ON rates(location_id, fiscal_year, month);
"""


def build_database(
    path: Path | str,
    records: Iterable[NormalizedRate],
    sources: Iterable[SourceMetadata],
    validation: ValidationReport,
    *,
    fiscal_year: int,
    started_at: datetime,
    prior_refresh_history: list[tuple[object, ...]] | None = None,
    refresh_record_count: int | None = None,
) -> None:
    """Build a complete SQLite database at a new temporary path."""

    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    database.unlink(missing_ok=True)
    source_by_file_hash = {
        (source.filename, source.sha256): source for source in sources
    }
    source_list = list(source_by_file_hash.values())
    record_list = list(records)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        connection.executemany(
            """INSERT INTO sources(
                agency, dataset_name, fiscal_year, source_url, downloaded_at,
                filename, sha256, file_size, record_count, parser_version,
                validation_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    source.agency, source.dataset_name, source.fiscal_year,
                    source.source_url, source.downloaded_at.isoformat(), source.filename,
                    source.sha256, source.file_size, source.record_count,
                    source.parser_version, source.validation_status,
                )
                for source in source_list
            ],
        )
        source_ids = {
            row[1]: row[0]
            for row in connection.execute("SELECT id, sha256 FROM sources")
        }
        location_rows: dict[tuple[object, ...], None] = {}
        for record in record_list:
            key = _location_key(record)
            location_rows[key] = None
        connection.executemany(
            """INSERT INTO locations(
                zip_code, state, city, county, primary_destination, locality,
                destination_id, is_standard
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            location_rows.keys(),
        )
        location_ids = {
            tuple(row[1:]): row[0]
            for row in connection.execute(
                "SELECT id, zip_code, state, city, county, primary_destination, "
                "locality, destination_id, is_standard FROM locations"
            )
        }
        rate_rows = []
        for record in record_list:
            source_id = source_ids.get(record.source_sha256)
            if source_id is None:
                raise DataValidationError(
                    f"No source metadata for rate hash {record.source_sha256}"
                )
            rate_rows.append(
                (
                    location_ids[_location_key(record)], source_id, record.fiscal_year,
                    record.month, record.effective_start.isoformat(),
                    record.effective_end.isoformat(), f"{record.lodging_rate:.2f}",
                    f"{record.mie_rate:.2f}", f"{record.first_last_day_mie:.2f}",
                    record.source_file,
                )
            )
        connection.executemany(
            """INSERT INTO rates(
                location_id, source_id, fiscal_year, month, effective_start,
                effective_end, lodging_rate, mie_rate, first_last_day_mie, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rate_rows,
        )
        if prior_refresh_history:
            connection.executemany(
                """INSERT INTO refresh_history(
                    fiscal_year, started_at, completed_at, status, record_count,
                    source_count, validation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                prior_refresh_history,
            )
        now = datetime.now(timezone.utc)
        connection.execute(
            """INSERT INTO refresh_history(
                fiscal_year, started_at, completed_at, status, record_count,
                source_count, validation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                fiscal_year, started_at.isoformat(), now.isoformat(), "valid",
                refresh_record_count if refresh_record_count is not None else len(record_list),
                len(source_list), json.dumps(validation.to_dict(), sort_keys=True),
            ),
        )
        connection.execute("ANALYZE")
    database_report = validate_database(database)
    if not database_report.is_valid:
        raise DataValidationError(
            "Temporary SQLite validation failed: "
            + "; ".join(issue.message for issue in database_report.errors)
        )


def _location_key(record: NormalizedRate) -> tuple[object, ...]:
    return (
        record.zip_code,
        record.state,
        record.city,
        record.county,
        record.primary_destination,
        record.locality,
        record.destination_id,
        int(record.is_standard),
    )


def load_retained_dataset(
    database_path: Path | str,
    *,
    exclude_fiscal_year: int,
) -> tuple[list[NormalizedRate], list[SourceMetadata], list[tuple[object, ...]]]:
    """Load previously validated years and refresh history for a replacement build."""

    database = Path(database_path)
    if not database.exists():
        return [], [], []
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rate_rows = connection.execute(
            """SELECT l.zip_code, l.state, l.city, l.county, l.primary_destination,
                      l.locality, l.destination_id, l.is_standard, r.fiscal_year,
                      r.month, r.effective_start, r.effective_end, r.lodging_rate,
                      r.mie_rate, r.first_last_day_mie, r.source_file,
                      s.agency, s.source_url, s.downloaded_at, s.sha256
               FROM rates r JOIN locations l ON l.id=r.location_id
               JOIN sources s ON s.id=r.source_id
               WHERE r.fiscal_year != ?""",
            (exclude_fiscal_year,),
        ).fetchall()
        source_rows = connection.execute(
            "SELECT * FROM sources WHERE fiscal_year != ?",
            (exclude_fiscal_year,),
        ).fetchall()
        history_rows = connection.execute(
            """SELECT fiscal_year, started_at, completed_at, status, record_count,
                      source_count, validation_json
               FROM refresh_history ORDER BY id"""
        ).fetchall()
    records = [
        NormalizedRate(
            zip_code=row["zip_code"], state=row["state"], city=row["city"],
            county=row["county"], primary_destination=row["primary_destination"],
            locality=row["locality"], destination_id=row["destination_id"],
            fiscal_year=int(row["fiscal_year"]), month=int(row["month"]),
            effective_start=datetime.fromisoformat(row["effective_start"]).date(),
            effective_end=datetime.fromisoformat(row["effective_end"]).date(),
            lodging_rate=Decimal(row["lodging_rate"]),
            mie_rate=Decimal(row["mie_rate"]),
            first_last_day_mie=Decimal(row["first_last_day_mie"]),
            source_agency=row["agency"], source_file=row["source_file"],
            source_url=row["source_url"],
            source_retrieved_at=datetime.fromisoformat(row["downloaded_at"]),
            source_sha256=row["sha256"], is_standard=bool(row["is_standard"]),
        )
        for row in rate_rows
    ]
    sources = [
        SourceMetadata(
            agency=row["agency"], dataset_name=row["dataset_name"],
            fiscal_year=int(row["fiscal_year"]), source_url=row["source_url"],
            downloaded_at=datetime.fromisoformat(row["downloaded_at"]),
            filename=row["filename"], sha256=row["sha256"],
            file_size=int(row["file_size"]), record_count=int(row["record_count"]),
            parser_version=row["parser_version"],
            validation_status=row["validation_status"], local_path=None,
        )
        for row in source_rows
    ]
    return records, sources, [tuple(row) for row in history_rows]


def export_database(
    database_path: Path | str,
    csv_path: Path | str,
    excel_path: Path | str,
    validation: ValidationReport,
) -> None:
    """Export normalized database views to CSV and a multi-sheet workbook."""

    database = Path(database_path)
    csv_target = Path(csv_path)
    excel_target = Path(excel_path)
    csv_target.parent.mkdir(parents=True, exist_ok=True)
    query = """
        SELECT l.zip_code, l.state, l.city, l.county, l.primary_destination,
               l.locality, l.destination_id, r.fiscal_year, r.month,
               r.effective_start, r.effective_end, r.lodging_rate, r.mie_rate,
               r.first_last_day_mie, l.is_standard, s.agency AS source_agency,
               r.source_file, s.source_url, s.downloaded_at AS source_retrieved_at,
               s.sha256 AS source_sha256
        FROM rates r
        JOIN locations l ON l.id = r.location_id
        JOIN sources s ON s.id = r.source_id
        ORDER BY l.zip_code, r.effective_start
    """
    with closing(sqlite3.connect(database)) as connection:
        rates = pd.read_sql_query(query, connection)
        locations = pd.read_sql_query(
            "SELECT * FROM locations ORDER BY state, zip_code", connection
        )
        sources = pd.read_sql_query("SELECT * FROM sources ORDER BY agency, filename", connection)
        refresh = pd.read_sql_query("SELECT * FROM refresh_history ORDER BY id", connection)
    rates.to_csv(csv_target, index=False)
    summary_rows = [
        {"kind": "metric", "code": key, "message": json.dumps(value)}
        for key, value in validation.metrics.items()
    ] + [
        {"kind": issue.severity, "code": issue.code, "message": issue.message}
        for issue in validation.issues
    ]
    with pd.ExcelWriter(excel_target, engine="openpyxl") as writer:
        rates.to_excel(writer, sheet_name="Rates", index=False)
        locations.to_excel(writer, sheet_name="Locations", index=False)
        sources.to_excel(writer, sheet_name="Sources", index=False)
        refresh.to_excel(writer, sheet_name="Refresh Log", index=False)
        pd.DataFrame(summary_rows).to_excel(
            writer, sheet_name="Validation Summary", index=False
        )
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions


def promote_outputs(
    temporary_database: Path,
    temporary_csv: Path,
    temporary_excel: Path,
    settings: Settings,
) -> None:
    """Archive previous outputs and atomically promote validated replacements."""

    settings.processed_dir.mkdir(parents=True, exist_ok=True)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    targets = (
        (temporary_database, settings.database_path),
        (temporary_csv, settings.csv_path),
        (temporary_excel, settings.excel_path),
    )
    for _, target in targets:
        if target.exists():
            archive = settings.archive_dir / f"{target.stem}.{stamp}{target.suffix}"
            shutil.copy2(target, archive)
    for temporary, target in targets:
        temporary.replace(target)
