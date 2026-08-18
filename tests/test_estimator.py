from dataclasses import replace
from datetime import date
from decimal import Decimal

from federal_per_diem.database import build_database
from federal_per_diem.estimator import estimate_trip
from federal_per_diem.models import SourceMetadata, ValidationReport

from conftest import make_rate


def test_trip_crosses_month_and_fiscal_year(tmp_path, source_metadata):
    source_2025 = replace(source_metadata, fiscal_year=2025, filename="fy2025.xlsx", sha256="b" * 64)
    records = [
        make_rate(fiscal_year=2025, month=9, start=date(2025, 9, 1), end=date(2025, 9, 30), lodging="100", source_sha="b" * 64, source_file="fy2025.xlsx"),
        make_rate(fiscal_year=2026, month=10, start=date(2025, 10, 1), end=date(2025, 10, 31), lodging="200"),
    ]
    database = tmp_path / "rates.sqlite"
    build_database(
        database,
        records,
        [replace(source_metadata, record_count=1), replace(source_2025, record_count=1)],
        ValidationReport(),
        fiscal_year=2026,
        started_at=source_metadata.downloaded_at,
    )
    estimate = estimate_trip("19103", "2025-09-29", "2025-10-03", database_path=database)
    assert estimate.travel_days == 5
    assert estimate.lodging_nights == 4
    assert estimate.lodging_allowance == Decimal("600.00")
    assert [rate for _, rate in estimate.nightly_lodging] == [
        Decimal("100.00"), Decimal("100.00"), Decimal("200.00"), Decimal("200.00")
    ]


def test_explicit_mileage_rate(tmp_path, source_metadata):
    rate = make_rate()
    database = tmp_path / "rates.sqlite"
    build_database(database, [rate], [source_metadata], ValidationReport(), fiscal_year=2026, started_at=source_metadata.downloaded_at)
    estimate = estimate_trip("19103", "2026-08-17", "2026-08-17", mileage="10", mileage_rate="0.70", database_path=database)
    assert estimate.mileage_allowance == Decimal("7.00")


def test_trip_crosses_month_boundary(tmp_path, source_metadata):
    records = [
        make_rate(
            month=1, start=date(2026, 1, 1), end=date(2026, 1, 31), lodging="100"
        ),
        make_rate(
            month=2, start=date(2026, 2, 1), end=date(2026, 2, 28), lodging="150"
        ),
    ]
    database = tmp_path / "rates.sqlite"
    build_database(
        database,
        records,
        [replace(source_metadata, record_count=2)],
        ValidationReport(),
        fiscal_year=2026,
        started_at=source_metadata.downloaded_at,
    )
    estimate = estimate_trip(
        "19103", "2026-01-30", "2026-02-02", database_path=database
    )
    assert estimate.lodging_nights == 3
    assert estimate.lodging_allowance == Decimal("350.00")

