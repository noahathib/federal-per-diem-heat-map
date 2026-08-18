"""Parser for GSA's official per-diem ZIP developer workbook."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

from .exceptions import DataValidationError, SchemaChangeError
from .models import NormalizedRate, SourceMetadata
from .utils import first_last_day, money, month_range, normalize_zip, snake_case


PARSER_VERSION = "gsa-zip-1.0"
MONTH_COLUMNS = {
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
}
REQUIRED_COLUMNS = {
    "destinationid",
    "name",
    "county",
    "locationdefined",
    "state",
    "zip",
    "fiscalyear",
    "meals",
    *MONTH_COLUMNS,
}


def parse_gsa_file(
    path: Path | str,
    metadata: SourceMetadata,
    *,
    expected_fiscal_year: int | None = None,
) -> tuple[list[NormalizedRate], SourceMetadata]:
    """Parse GSA's ZIP workbook into canonical ZIP/month records."""

    source_path = Path(path)
    try:
        frame = pd.read_excel(source_path, sheet_name=0, dtype=str, engine="openpyxl")
    except Exception as exc:  # pandas/openpyxl expose many format-specific errors
        raise DataValidationError(f"Cannot read GSA workbook {source_path.name}: {exc}") from exc
    frame.columns = [snake_case(column).replace("_", "") for column in frame.columns]
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise SchemaChangeError(
            f"GSA ZIP workbook is missing required columns: {sorted(missing)}"
        )
    if frame.empty:
        raise DataValidationError("GSA ZIP workbook contains no records")

    def text_value(value: object) -> str:
        return "" if pd.isna(value) else str(value).strip()

    destination_states: dict[str, set[str]] = {}
    prefix_states: dict[str, set[str]] = {}
    for source_row in frame.to_dict(orient="records"):
        source_state = text_value(source_row["state"]).upper()
        source_zip = text_value(source_row["zip"]).split(".0", 1)[0].zfill(5)
        source_destination_id = text_value(source_row["destinationid"]).split(".0", 1)[0]
        if source_state:
            destination_states.setdefault(source_destination_id, set()).add(source_state)
            for prefix_length in (4, 3, 2):
                prefix_states.setdefault(source_zip[:prefix_length], set()).add(source_state)

    output: list[NormalizedRate] = []
    for row_number, row in frame.iterrows():
        try:
            zip_code = normalize_zip(str(row["zip"]).split(".0", 1)[0].zfill(5))
            fiscal_year = int(str(row["fiscalyear"]).split(".0", 1)[0])
            if expected_fiscal_year is not None and fiscal_year != expected_fiscal_year:
                raise ValueError(
                    f"row fiscal year {fiscal_year} != expected {expected_fiscal_year}"
                )
            destination_id = text_value(row["destinationid"]).split(".0", 1)[0]
            state = text_value(row["state"]).upper()
            if not state:
                candidate_states = destination_states.get(destination_id, set())
                if destination_id == "0" or len(candidate_states) != 1:
                    for prefix_length in (4, 3, 2):
                        candidate_states = prefix_states.get(
                            zip_code[:prefix_length], set()
                        )
                        if len(candidate_states) == 1:
                            break
                if len(candidate_states) != 1:
                    raise ValueError(
                        f"blank state cannot be inferred unambiguously for ZIP {zip_code}"
                    )
                state = next(iter(candidate_states))
            destination = text_value(row["name"])
            county = text_value(row["county"]) or None
            location_defined = text_value(row["locationdefined"]) or None
            mie = money(row["meals"])
            assert mie is not None
            standard = destination_id == "0" or destination.lower() == "standard rate"
            locality = "Standard CONUS Rate" if standard else destination
            for column, month in MONTH_COLUMNS.items():
                calendar_year = fiscal_year - 1 if month >= 10 else fiscal_year
                effective_start, effective_end = month_range(calendar_year, month)
                lodging = money(row[column])
                assert lodging is not None
                output.append(
                    NormalizedRate(
                        zip_code=zip_code,
                        state=state,
                        city=None if standard else destination,
                        county=county,
                        primary_destination=destination,
                        locality=locality,
                        destination_id=destination_id,
                        fiscal_year=fiscal_year,
                        month=month,
                        effective_start=effective_start,
                        effective_end=effective_end,
                        lodging_rate=lodging,
                        mie_rate=mie,
                        first_last_day_mie=first_last_day(mie),
                        source_agency="GSA",
                        source_file=metadata.filename,
                        source_url=metadata.source_url,
                        source_retrieved_at=metadata.downloaded_at,
                        source_sha256=metadata.sha256,
                        is_standard=standard,
                    )
                )
        except (TypeError, ValueError) as exc:
            raise DataValidationError(
                f"Malformed GSA row {row_number + 2}: {exc}"
            ) from exc
    return output, replace(
        metadata,
        record_count=len(output),
        parser_version=PARSER_VERSION,
        validation_status="parsed",
    )


def parse_gsa_master_file(path: Path | str) -> pd.DataFrame:
    """Validate and return the human-oriented GSA master rates workbook."""

    source_path = Path(path)
    try:
        frame = pd.read_excel(source_path, sheet_name=0, header=1, dtype=str)
    except Exception as exc:
        raise DataValidationError(
            f"Cannot read GSA master rates workbook {source_path.name}: {exc}"
        ) from exc
    required_labels = {"ID", "STATE", "DESTINATION", "COUNTY/LOCATION DEFINED"}
    if not required_labels.issubset(set(frame.columns)):
        raise SchemaChangeError("Unexpected GSA master rates workbook schema")
    return frame
