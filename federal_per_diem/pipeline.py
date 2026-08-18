"""End-to-end refresh orchestration with known-good output protection."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .census_parser import parse_census_crosswalk
from .config import Settings
from .database import (
    build_database,
    export_database,
    load_retained_dataset,
    promote_outputs,
)
from .dod_parser import parse_dod_file
from .downloader import download_fiscal_year
from .exceptions import DataValidationError
from .gsa_parser import parse_gsa_file, parse_gsa_master_file
from .models import SourceMetadata, ValidationReport
from .normalizer import normalize_dod_rates
from .validation import validate_rates


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Summary returned by a refresh operation."""

    fiscal_year: int
    record_count: int
    source_count: int
    database_path: Path | None
    csv_path: Path | None
    excel_path: Path | None
    validation: ValidationReport
    promoted: bool


def refresh_rates(
    fiscal_year: int,
    *,
    force: bool = False,
    validate_only: bool = False,
    gsa_only: bool = False,
    dod_only: bool = False,
    settings: Settings | None = None,
) -> RefreshResult:
    """Download, normalize, validate, and safely promote a fiscal year."""

    if gsa_only and dod_only:
        raise ValueError("--gsa-only and --dod-only are mutually exclusive")
    settings = settings or Settings.from_env()
    started_at = datetime.now(timezone.utc)
    if gsa_only:
        include = {"gsa_zip", "gsa_rates"}
    elif dod_only:
        include = {
            f"dod_oconus_{fiscal_year - 1}", f"dod_oconus_{fiscal_year}",
            "census_place", "census_county", "census_cousub",
        }
    else:
        include = None
    LOGGER.info("Discovering official FY%d sources", fiscal_year)
    downloaded = download_fiscal_year(
        fiscal_year,
        force=force,
        settings=settings,
        include_keys=include,
    )

    records = []
    source_metadata: list[SourceMetadata] = []
    if not dod_only:
        LOGGER.info("Parsing GSA FY%d ZIP developer workbook", fiscal_year)
        gsa_records, gsa_metadata = parse_gsa_file(
            downloaded["gsa_zip"].local_path,
            downloaded["gsa_zip"],
            expected_fiscal_year=fiscal_year,
        )
        records.extend(gsa_records)
        source_metadata.append(gsa_metadata)
        master = parse_gsa_master_file(downloaded["gsa_rates"].local_path)
        source_metadata.append(
            replace(
                downloaded["gsa_rates"],
                record_count=len(master),
                parser_version="gsa-master-1.0",
                validation_status="parsed",
            )
        )
        LOGGER.info("Parsed %s GSA normalized records", f"{len(gsa_records):,}")

    if not gsa_only:
        dod_rows = []
        for calendar_year in (fiscal_year - 1, fiscal_year):
            key = f"dod_oconus_{calendar_year}"
            parsed, metadata = parse_dod_file(
                downloaded[key].local_path, downloaded[key]
            )
            dod_rows.extend(parsed)
            source_metadata.append(metadata)
        crosswalk = parse_census_crosswalk(
            downloaded["census_place"].local_path,
            downloaded["census_county"].local_path,
            downloaded["census_cousub"].local_path,
        )
        for key in ("census_place", "census_county", "census_cousub"):
            source_metadata.append(
                replace(
                    downloaded[key],
                    record_count=len(crosswalk),
                    parser_version="census-zcta-1.0",
                    validation_status="parsed",
                )
            )
        dod_records = normalize_dod_rates(dod_rows, crosswalk, fiscal_year)
        records.extend(dod_records)
        LOGGER.info("Parsed %s DTMO normalized records", f"{len(dod_records):,}")

    LOGGER.info("Validating %s normalized records", f"{len(records):,}")
    validation = validate_rates(
        records,
        expected_fiscal_year=fiscal_year,
        previous_database=settings.database_path,
        require_all_states=not (gsa_only or dod_only),
    )
    if not validation.is_valid:
        raise DataValidationError(
            "Incoming dataset failed validation; current outputs were retained: "
            + "; ".join(issue.message for issue in validation.errors[:20])
        )
    source_metadata = [
        replace(source, validation_status="valid") for source in source_metadata
    ]

    diagnostic_only = validate_only or gsa_only or dod_only
    if diagnostic_only:
        LOGGER.info("Validation succeeded; no production outputs were replaced")
        return RefreshResult(
            fiscal_year=fiscal_year,
            record_count=len(records),
            source_count=len(source_metadata),
            database_path=None,
            csv_path=None,
            excel_path=None,
            validation=validation,
            promoted=False,
        )

    settings.processed_dir.mkdir(parents=True, exist_ok=True)
    temporary_database = settings.processed_dir / "federal_per_diem.tmp.sqlite"
    temporary_csv = settings.processed_dir / "federal_per_diem.tmp.csv"
    temporary_excel = settings.processed_dir / "federal_per_diem.tmp.xlsx"
    temporary_paths = (temporary_database, temporary_csv, temporary_excel)
    for path in temporary_paths:
        path.unlink(missing_ok=True)
    try:
        retained_records, retained_sources, prior_history = load_retained_dataset(
            settings.database_path, exclude_fiscal_year=fiscal_year
        )
        if retained_records:
            LOGGER.info(
                "Retaining %s validated records from other fiscal years",
                f"{len(retained_records):,}",
            )
        combined_records = retained_records + records
        combined_sources = retained_sources + source_metadata
        LOGGER.info("Building temporary SQLite database")
        build_database(
            temporary_database,
            combined_records,
            combined_sources,
            validation,
            fiscal_year=fiscal_year,
            started_at=started_at,
            prior_refresh_history=prior_history,
            refresh_record_count=len(records),
        )
        LOGGER.info("Creating CSV and Excel exports")
        export_database(
            temporary_database, temporary_csv, temporary_excel, validation
        )
        LOGGER.info("Promoting validated outputs and archiving prior versions")
        promote_outputs(
            temporary_database, temporary_csv, temporary_excel, settings
        )
    except Exception:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
        raise
    return RefreshResult(
        fiscal_year=fiscal_year,
        record_count=len(records),
        source_count=len(source_metadata),
        database_path=settings.database_path,
        csv_path=settings.csv_path,
        excel_path=settings.excel_path,
        validation=validation,
        promoted=True,
    )
