import zipfile
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from federal_per_diem.dod_parser import parse_dod_file
from federal_per_diem.exceptions import DataValidationError


def test_dod_ascii_parser_calculates_mie(tmp_path, source_metadata):
    archive = tmp_path / "OCONUS-ASCII-2026.zip"
    content = (
        "ALASKA;ANCHORAGE;04/01;09/30;329;118;68;30;;;477;01/01/2026;\n"
        "HAWAII;HONOLULU;01/01;12/31;250;130;75;30;;;410;01/01/2026;\n"
    )
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("08-01-26oconus.txt", content)
        bundle.writestr("08-01-26oconusnm.txt", content)
    metadata = replace(
        source_metadata,
        agency="DoD/DTMO",
        filename=archive.name,
        local_path=archive,
    )
    rows, updated = parse_dod_file(archive, metadata)
    assert len(rows) == 2
    assert rows[0].mie_rate == Decimal("148.00")
    assert rows[1].locality == "HONOLULU"
    assert updated.record_count == 2



def test_dod_parser_keeps_territories_and_drops_foreign_rows(tmp_path, source_metadata):
    archive = tmp_path / "OCONUS-ASCII-2026.zip"
    content = (
        "PUERTO RICO;SAN JUAN & NAV RES STA;01/01;12/31;295;118;68;30;;;443;01/01/2026;\n"
        "VIRGIN ISLANDS (U.S.);ST. JOHN;12/16;04/14;414;120;69;30;;;564;01/01/2026;\n"
        "GUAM;GUAM (INCL ALL MIL INSTAL);01/01;12/31;179;99;59;25;;;303;01/01/2026;\n"
        "NORTHERN MARIANA ISLANDS;SAIPAN;01/01;12/31;161;90;54;23;;;274;01/01/2026;\n"
        "AMERICAN SAMOA;AMERICAN SAMOA;01/01;12/31;149;82;50;21;;;252;01/01/2026;\n"
        "GERMANY;BERLIN;01/01;12/31;220;100;60;25;;;305;01/01/2026;\n"
        "ALL PLACES NOT LISTED;ALL PLACES NOT LISTED;01/01;12/31;55;34;26;9;;;98;01/01/2026;\n"
    )
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("08-01-26oconus.txt", content)
    metadata = replace(
        source_metadata, agency="DoD/DTMO", filename=archive.name, local_path=archive
    )
    rows, updated = parse_dod_file(archive, metadata)

    assert {row.state for row in rows} == {
        "PUERTO RICO",
        "VIRGIN ISLANDS (U.S.)",
        "GUAM",
        "NORTHERN MARIANA ISLANDS",
        "AMERICAN SAMOA",
    }
    assert updated.record_count == 5
    saint_john = next(row for row in rows if row.locality == "ST. JOHN")
    assert saint_john.mie_rate == Decimal("150.00")
    assert saint_john.season_begin == "12/16"
    assert saint_john.applies_on(date(2026, 1, 5))
    assert not saint_john.applies_on(date(2026, 5, 1))


def test_dod_parser_rejects_an_archive_with_no_requested_area(tmp_path, source_metadata):
    archive = tmp_path / "OCONUS-ASCII-2026.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "08-01-26oconus.txt",
            "GERMANY;BERLIN;01/01;12/31;220;100;60;25;;;305;01/01/2026;\n",
        )
    metadata = replace(
        source_metadata, agency="DoD/DTMO", filename=archive.name, local_path=archive
    )
    with pytest.raises(DataValidationError, match="no rows for"):
        parse_dod_file(archive, metadata)
