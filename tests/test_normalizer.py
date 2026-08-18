from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from federal_per_diem.census_parser import GeoMapping
from federal_per_diem.dod_parser import DODSeasonRate
from federal_per_diem.exceptions import DataValidationError
from federal_per_diem.normalizer import (
    ALASKA_ALIASES,
    AMERICAN_SAMOA_LOCALITY,
    GUAM_LOCALITY,
    INSTALLATION_ONLY_LOCALITIES,
    NORTHERN_MARIANA_LOCALITIES,
    PUERTO_RICO_MUNICIPIO_LOCALITIES,
    VIRGIN_ISLANDS_LOCALITIES,
    normalize_dod_rates,
    resolve_dod_locality,
)


def dod_row(state, locality, begin, end, lodging):
    return DODSeasonRate(
        state=state,
        locality=locality,
        season_begin=begin,
        season_end=end,
        lodging_rate=Decimal(lodging),
        local_meal_rate=Decimal("100"),
        incidental_rate=Decimal("20"),
        footnote=None,
        maximum_per_diem=Decimal(lodging) + Decimal("120"),
        rate_effective_date=date(2026, 1, 1),
        publication_date=date(2025, 10, 1),
        source_file="OCONUS.zip:10-01-25oconus.txt",
        source_url="https://example.mil/OCONUS.zip",
        source_retrieved_at=datetime(2025, 10, 1, tzinfo=timezone.utc),
        source_sha256="d" * 64,
    )


def test_hawaii_county_resolution():
    mapping = GeoMapping("96701", "HI", "Aiea", "Honolulu County", "15003")
    assert resolve_dod_locality(mapping, {"ISLE OF OAHU", "[OTHER]"}) == "ISLE OF OAHU"


def test_dod_midmonth_season_is_not_collapsed():
    rows = [
        dod_row("ALASKA", "EIELSON AFB", "04/16", "11/30", "279"),
        dod_row("ALASKA", "EIELSON AFB", "12/01", "04/15", "199"),
        dod_row("ALASKA", "[OTHER]", "01/01", "12/31", "239"),
    ]
    mapping = GeoMapping("99702", "AK", "Eielson AFB", "Fairbanks North Star Borough", "02090")
    normalized = normalize_dod_rates(rows, {"99702": mapping}, 2026)
    april = [record for record in normalized if record.month == 4]
    assert [(record.effective_start.day, record.effective_end.day) for record in april] == [(1, 15), (16, 30)]
    assert [record.lodging_rate for record in april] == [Decimal("199.00"), Decimal("279.00")]



@pytest.mark.parametrize(
    ("county_geoid", "county", "expected"),
    [
        ("78010", "St. Croix Island", "ST. CROIX"),
        ("78020", "St. John Island", "ST. JOHN"),
        ("78030", "St. Thomas Island", "ST. THOMAS"),
    ],
)
def test_virgin_islands_resolve_by_island_district(county_geoid, county, expected):
    mapping = GeoMapping("00820", "VI", None, county, county_geoid)
    localities = {"ST. CROIX", "ST. JOHN", "ST. THOMAS"}
    assert resolve_dod_locality(mapping, localities) == expected


@pytest.mark.parametrize(
    ("county_geoid", "county", "expected"),
    [
        ("69100", "Rota Municipality", "ROTA"),
        ("69110", "Saipan Municipality", "SAIPAN"),
        ("69120", "Tinian Municipality", "TINIAN"),
    ],
)
def test_northern_mariana_islands_resolve_by_municipality(county_geoid, county, expected):
    mapping = GeoMapping("96950", "MP", None, county, county_geoid)
    assert resolve_dod_locality(mapping, {"ROTA", "SAIPAN", "TINIAN"}) == expected


def test_guam_resolves_to_the_territory_wide_locality():
    mapping = GeoMapping("96913", "GU", "Tamuning-Tumon-Harmon", "Guam", "66010")
    localities = {"GUAM (INCL ALL MIL INSTAL)", "TAMUNING", "CAMP BLAZ"}
    assert resolve_dod_locality(mapping, localities) == "GUAM (INCL ALL MIL INSTAL)"


def test_american_samoa_resolves_to_the_territory_wide_locality():
    mapping = GeoMapping("96799", "AS", None, "Western District", "60050")
    localities = {"AMERICAN SAMOA", "PAGO PAGO"}
    assert resolve_dod_locality(mapping, localities) == "AMERICAN SAMOA"


@pytest.mark.parametrize(
    ("county", "expected"),
    [
        ("Bayamón Municipio", "BAYAMON"),
        ("Mayagüez Municipio", "MAYAGUEZ"),
        ("Ponce Municipio", "PONCE"),
        ("Fajardo Municipio", "FAJARDO [INCL ROOSEVELT RDS NAVSTAT]"),
        ("San Juan Municipio", "SAN JUAN & NAV RES STA"),
    ],
)
def test_puerto_rico_resolves_by_accented_municipio(county, expected):
    mapping = GeoMapping("00901", "PR", "Some zona urbana", county, "72127")
    localities = set(PUERTO_RICO_MUNICIPIO_LOCALITIES.values()) | {"[OTHER]"}
    assert resolve_dod_locality(mapping, localities) == expected


def test_puerto_rico_unlisted_municipio_uses_the_published_catch_all():
    mapping = GeoMapping("00969", "PR", "Guaynabo zona urbana", "Guaynabo Municipio", "72061")
    localities = set(PUERTO_RICO_MUNICIPIO_LOCALITIES.values()) | {
        "FT. BUCHANAN [INCL GSA SVC CTR, GUAYNABO]",
        "[OTHER]",
    }
    assert resolve_dod_locality(mapping, localities) == "[OTHER]"


def test_no_resolution_table_maps_a_civilian_zip_to_an_installation():
    """Guard the published policy that installations are never inferred."""

    resolved = (
        set(PUERTO_RICO_MUNICIPIO_LOCALITIES.values())
        | set(VIRGIN_ISLANDS_LOCALITIES.values())
        | set(NORTHERN_MARIANA_LOCALITIES.values())
        | {GUAM_LOCALITY, AMERICAN_SAMOA_LOCALITY}
        | set(ALASKA_ALIASES.values())
    )
    assert not resolved & INSTALLATION_ONLY_LOCALITIES


def test_territory_without_a_catch_all_refuses_to_guess():
    """The four smaller territories publish no [OTHER]; nothing may be invented."""

    mapping = GeoMapping("00805", "VI", None, "Water Island", "78099")
    assert resolve_dod_locality(mapping, {"ST. CROIX", "ST. JOHN"}) is None


def test_unsupported_area_is_rejected():
    mapping = GeoMapping("96799", "XX", None, "Nowhere", "99999")
    with pytest.raises(DataValidationError, match="No DTMO locality policy"):
        resolve_dod_locality(mapping, {"[OTHER]"})


def test_normalize_refuses_unresolvable_territory_zip():
    rows = [dod_row("VIRGIN ISLANDS (U.S.)", "ST. CROIX", "01/01", "12/31", "299")]
    mapping = GeoMapping("00805", "VI", None, "Water Island", "78099")
    with pytest.raises(DataValidationError, match="no catch-all"):
        normalize_dod_rates(rows, {"00805": mapping}, 2026)


def test_normalize_requires_dtmo_to_publish_the_area():
    rows = [dod_row("ALASKA", "[OTHER]", "01/01", "12/31", "239")]
    mapping = GeoMapping("00820", "VI", None, "St. Croix Island", "78010")
    with pytest.raises(DataValidationError, match="publishes no localities"):
        normalize_dod_rates(rows, {"00820": mapping}, 2026)


def test_virgin_islands_year_wrapping_season_is_split_by_day():
    """St. Thomas changes rate on 04/15 and 12/16, inside both months."""

    rows = [
        dod_row("VIRGIN ISLANDS (U.S.)", "ST. THOMAS", "04/15", "12/15", "324"),
        dod_row("VIRGIN ISLANDS (U.S.)", "ST. THOMAS", "12/16", "04/14", "414"),
    ]
    mapping = GeoMapping("00802", "VI", None, "St. Thomas Island", "78030")
    normalized = normalize_dod_rates(rows, {"00802": mapping}, 2026)

    april = [record for record in normalized if record.month == 4]
    assert [(r.effective_start.day, r.effective_end.day) for r in april] == [(1, 14), (15, 30)]
    assert [r.lodging_rate for r in april] == [Decimal("414.00"), Decimal("324.00")]

    december = [record for record in normalized if record.month == 12]
    assert [(r.effective_start.day, r.effective_end.day) for r in december] == [(1, 15), (16, 31)]
    assert [r.lodging_rate for r in december] == [Decimal("324.00"), Decimal("414.00")]
    assert {r.effective_start.year for r in december} == {2025}


def test_territory_record_carries_dtmo_provenance_and_is_not_standard():
    rows = [dod_row("NORTHERN MARIANA ISLANDS", "SAIPAN", "01/01", "12/31", "161")]
    mapping = GeoMapping("96950", "MP", "Saipan", "Saipan Municipality", "69110")
    normalized = normalize_dod_rates(rows, {"96950": mapping}, 2026)
    record = normalized[0]
    assert record.state == "MP"
    assert record.locality == "SAIPAN"
    assert record.destination_id == "DTMO:MP:SAIPAN"
    assert record.source_agency == "DoD/DTMO"
    assert record.is_standard is False
    assert record.first_last_day_mie == Decimal("90.00")
    assert len(normalized) == 12


def test_puerto_rico_ignores_a_place_name_that_matches_a_distant_locality():
    """ZCTA 00772 holds a place named Vieques but lies in Loiza, on the mainland.

    Resolving Puerto Rico by Census place would price this mainland ZIP at the
    Vieques island rate. Resolving by municipio, as the policy does, cannot.
    """

    mapping = GeoMapping("00772", "PR", "Vieques", "Loíza Municipio", "72087")
    localities = set(PUERTO_RICO_MUNICIPIO_LOCALITIES.values()) | {"[OTHER]"}
    assert resolve_dod_locality(mapping, localities) == "[OTHER]"


def test_puerto_rico_uses_the_municipio_when_the_place_is_a_small_comunidad():
    """00735's place is the comunidad Aguas Claras; its municipio is Ceiba."""

    mapping = GeoMapping("00735", "PR", "Aguas Claras", "Ceiba Municipio", "72037")
    localities = set(PUERTO_RICO_MUNICIPIO_LOCALITIES.values()) | {"[OTHER]"}
    assert resolve_dod_locality(mapping, localities) == "CEIBA"


HAWAII_LOCALITIES = {
    "HONOLULU",
    "KAPOLEI",
    "LIHUE",
    "ISLE OF HAWAII: HILO",
    "ISLE OF HAWAII: LOCATIONS OTHER THAN HILO",
    "ISLE OF OAHU",
    "ISLE OF KAUAI",
    "ISLE OF MOLOKAI",
    "ISLE OF LANAI",
    "ISLE OF MAUI",
    "[OTHER]",
}


@pytest.mark.parametrize(
    ("place", "county_geoid", "subdivision", "expected"),
    [
        ("Urban Honolulu", "15003", None, "HONOLULU"),
        ("Kapolei", "15003", None, "KAPOLEI"),
        ("Līhuʻe", "15007", None, "LIHUE"),
        ("Lihue (East)", "15007", None, "LIHUE"),
        ("Hilo", "15001", None, "ISLE OF HAWAII: HILO"),
        ("Kailua", "15001", None, "ISLE OF HAWAII: LOCATIONS OTHER THAN HILO"),
        ("Aiea", "15003", None, "ISLE OF OAHU"),
        ("Kapaa", "15007", None, "ISLE OF KAUAI"),
        ("Kaunakakai", "15005", None, "ISLE OF MOLOKAI"),
        ("Lanai City", "15009", "Lānaʻi CCD", "ISLE OF LANAI"),
        ("Kaunakakai", "15009", "Molokaʻi CCD", "ISLE OF MOLOKAI"),
        ("Kihei", "15009", "Makawao CCD", "ISLE OF MAUI"),
        ("Nowhere", "15999", None, "[OTHER]"),
    ],
)
def test_hawaii_island_resolution_is_unchanged(place, county_geoid, subdivision, expected):
    """Pin every Hawaii branch, including the accented Lihue and Lanai spellings."""

    mapping = GeoMapping("96700", "HI", place, "Some County", county_geoid, subdivision)
    assert resolve_dod_locality(mapping, HAWAII_LOCALITIES) == expected


def test_resolver_raises_if_a_table_ever_points_at_an_installation(monkeypatch):
    """The installation guard must fire even if a future edit breaks a table."""

    monkeypatch.setitem(
        PUERTO_RICO_MUNICIPIO_LOCALITIES,
        "GUAYNABO",
        "FT. BUCHANAN [INCL GSA SVC CTR, GUAYNABO]",
    )
    mapping = GeoMapping("00969", "PR", None, "Guaynabo Municipio", "72061")
    with pytest.raises(DataValidationError, match="Refusing to resolve"):
        resolve_dod_locality(
            mapping, {"FT. BUCHANAN [INCL GSA SVC CTR, GUAYNABO]", "[OTHER]"}
        )
