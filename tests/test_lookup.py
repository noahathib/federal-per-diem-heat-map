from dataclasses import replace
from datetime import date

import pytest

from federal_per_diem.database import build_database
from federal_per_diem.exceptions import AmbiguousRateError
from federal_per_diem.lookup import get_per_diem
from federal_per_diem.models import ValidationReport

from conftest import make_rate


def build_test_db(tmp_path, source_metadata, records):
    database = tmp_path / "rates.sqlite"
    build_database(
        database,
        records,
        [replace(source_metadata, record_count=len(records))],
        ValidationReport(),
        fiscal_year=2026,
        started_at=source_metadata.downloaded_at,
    )
    return database


def test_standard_conus_lookup(tmp_path, source_metadata):
    rate = make_rate(zip_code="01234", state="MA", locality="Standard CONUS Rate", destination_id="0", standard=True)
    database = build_test_db(tmp_path, source_metadata, [rate])
    result = get_per_diem("01234-9999", "2026-08-17", database_path=database)
    assert result.is_standard
    assert result.locality == "Standard CONUS Rate"


def test_ambiguous_zip_is_not_silently_selected(tmp_path, source_metadata):
    records = [
        make_rate(locality="Locality A", destination_id="A"),
        make_rate(locality="Locality B", destination_id="B", lodging="250.00"),
    ]
    database = build_test_db(tmp_path, source_metadata, records)
    with pytest.raises(AmbiguousRateError):
        get_per_diem("19103", date(2026, 8, 17), database_path=database)


def test_alaska_and_hawaii_use_same_lookup_api(tmp_path, source_metadata):
    dod_source = replace(
        source_metadata,
        agency="DoD/DTMO",
        filename="OCONUS-ASCII-2026.zip",
        sha256="d" * 64,
    )
    records = [
        make_rate(
            zip_code="99501", state="AK", locality="ANCHORAGE",
            destination_id="DTMO:AK:ANCHORAGE", lodging="329", mie="148",
            source_sha="d" * 64, source_file="OCONUS-ASCII-2026.zip",
            agency="DoD/DTMO",
        ),
        make_rate(
            zip_code="96815", state="HI", locality="HONOLULU",
            destination_id="DTMO:HI:HONOLULU", lodging="202", mie="163",
            source_sha="d" * 64, source_file="OCONUS-ASCII-2026.zip",
            agency="DoD/DTMO",
        ),
    ]
    database = tmp_path / "rates.sqlite"
    build_database(
        database,
        records,
        [replace(dod_source, record_count=2)],
        ValidationReport(),
        fiscal_year=2026,
        started_at=source_metadata.downloaded_at,
    )
    alaska = get_per_diem("99501", "2026-08-17", database_path=database)
    hawaii = get_per_diem("96815", "2026-08-17", database_path=database)
    assert (alaska.state, alaska.source_agency) == ("AK", "DoD/DTMO")
    assert (hawaii.state, hawaii.source_agency) == ("HI", "DoD/DTMO")

