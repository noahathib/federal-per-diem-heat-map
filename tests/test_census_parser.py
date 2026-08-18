import pytest

from federal_per_diem.census_parser import STATE_FIPS, parse_census_crosswalk
from federal_per_diem.exceptions import DataValidationError, SchemaChangeError


def write_relationship(path, geo_column, name_column, rows):
    """Write a pipe-delimited Census relationship file with a BOM, as published."""

    header = "|".join(
        ["GEOID_ZCTA5_20", geo_column, name_column, "AREALAND_PART", "AREAWATER_PART"]
    )
    lines = [header] + ["|".join(str(field) for field in row) for row in rows]
    path.write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
    return path


COUNTIES = [
    ("99501", "02020", "Anchorage Municipality", 100, 0),
    ("96701", "15003", "Honolulu County", 100, 0),
    ("00901", "72127", "San Juan Municipio", 100, 0),
    ("00802", "78030", "St. Thomas Island", 100, 0),
    ("96913", "66010", "Guam", 100, 0),
    ("96950", "69110", "Saipan Municipality", 100, 0),
    ("96799", "60050", "Western District", 100, 0),
    ("19103", "42101", "Philadelphia County", 100, 0),
]

PLACES = [
    ("00901", "7276770", "San Juan zona urbana", 100, 0),
    ("96913", "6669120", "Tamuning-Tumon-Harmon village", 100, 0),
]

COUSUBS = [("96950", "6911090", "Saipan Municipality", 100, 0)]


@pytest.fixture()
def relationship_files(tmp_path):
    return (
        write_relationship(
            tmp_path / "place.txt", "GEOID_PLACE_20", "NAMELSAD_PLACE_20", PLACES
        ),
        write_relationship(
            tmp_path / "county.txt", "GEOID_COUNTY_20", "NAMELSAD_COUNTY_20", COUNTIES
        ),
        write_relationship(
            tmp_path / "cousub.txt", "GEOID_COUSUB_20", "NAMELSAD_COUSUB_20", COUSUBS
        ),
    )


def test_crosswalk_covers_every_zip_addressable_non_conus_area(relationship_files):
    crosswalk = parse_census_crosswalk(*relationship_files)
    assert {mapping.state for mapping in crosswalk.values()} == set(STATE_FIPS.values())
    assert set(STATE_FIPS.values()) == {"AK", "HI", "AS", "GU", "MP", "PR", "VI"}


def test_crosswalk_excludes_conus_zips(relationship_files):
    """CONUS is priced by the GSA workbook, so it must never enter this crosswalk."""

    crosswalk = parse_census_crosswalk(*relationship_files)
    assert "19103" not in crosswalk


def test_crosswalk_keeps_published_county_name_and_cleans_the_place(relationship_files):
    crosswalk = parse_census_crosswalk(*relationship_files)
    san_juan = crosswalk["00901"]
    assert san_juan.state == "PR"
    assert san_juan.county == "San Juan Municipio"
    assert san_juan.county_geoid == "72127"
    assert san_juan.place == "San Juan"
    guam = crosswalk["96913"]
    assert guam.place == "Tamuning-Tumon-Harmon"
    assert crosswalk["96950"].county_subdivision == "Saipan"


def test_crosswalk_selects_the_largest_intersecting_part(tmp_path):
    counties = [
        ("00802", "78030", "St. Thomas Island", 900, 0),
        ("00802", "78020", "St. John Island", 100, 0),
    ]
    files = (
        write_relationship(tmp_path / "p.txt", "GEOID_PLACE_20", "NAMELSAD_PLACE_20", []),
        write_relationship(
            tmp_path / "c.txt", "GEOID_COUNTY_20", "NAMELSAD_COUNTY_20", counties
        ),
        write_relationship(tmp_path / "s.txt", "GEOID_COUSUB_20", "NAMELSAD_COUSUB_20", []),
    )
    crosswalk = parse_census_crosswalk(*files)
    assert crosswalk["00802"].county_geoid == "78030"


def test_crosswalk_rejects_a_file_with_no_non_conus_rows(tmp_path):
    files = (
        write_relationship(tmp_path / "p.txt", "GEOID_PLACE_20", "NAMELSAD_PLACE_20", []),
        write_relationship(
            tmp_path / "c.txt",
            "GEOID_COUNTY_20",
            "NAMELSAD_COUNTY_20",
            [("19103", "42101", "Philadelphia County", 100, 0)],
        ),
        write_relationship(tmp_path / "s.txt", "GEOID_COUSUB_20", "NAMELSAD_COUSUB_20", []),
    )
    with pytest.raises(DataValidationError, match="no ZCTAs for"):
        parse_census_crosswalk(*files)


def test_crosswalk_detects_a_renamed_column(tmp_path):
    files = (
        write_relationship(tmp_path / "p.txt", "GEOID_PLACE_20", "NAMELSAD_PLACE_20", []),
        write_relationship(tmp_path / "c.txt", "COUNTY_GEOID", "NAMELSAD_COUNTY_20", []),
        write_relationship(tmp_path / "s.txt", "GEOID_COUSUB_20", "NAMELSAD_COUSUB_20", []),
    )
    with pytest.raises(SchemaChangeError, match="GEOID_COUNTY_20"):
        parse_census_crosswalk(*files)
