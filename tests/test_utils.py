import pytest

from federal_per_diem import InvalidZipCodeError, date_to_fiscal_year, normalize_zip
from federal_per_diem.utils import clean_geo_name, fold_name, strip_county_suffix


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-09-30", 2025),
        ("2025-10-01", 2026),
        ("2026-08-17", 2026),
    ],
)
def test_date_to_fiscal_year(value, expected):
    assert date_to_fiscal_year(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("19103", "19103"), (19103, "19103"), ("19103-1234", "19103"), (1234, "01234")],
)
def test_normalize_zip(value, expected):
    assert normalize_zip(value) == expected


@pytest.mark.parametrize("value", ["1234", "ABCDE", "19103-12", True, -1])
def test_invalid_zip(value):
    with pytest.raises(InvalidZipCodeError):
        normalize_zip(value)



@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Bayamón", "BAYAMON"),
        ("Añasco", "ANASCO"),
        ("Mayagüez", "MAYAGUEZ"),
        ("Līhuʻe", "LIHUE"),
        ("Lihue (East)", "LIHUE EAST"),
        ("ST. CROIX", "ST CROIX"),
        (None, ""),
    ],
)
def test_fold_name_folds_accents_and_punctuation(value, expected):
    assert fold_name(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("San Juan Municipio", "San Juan"),
        ("St. Croix Island", "St. Croix"),
        ("Saipan Municipality", "Saipan"),
        ("Western District", "Western"),
        ("Honolulu County", "Honolulu"),
        ("Guam", "Guam"),
        (None, ""),
    ],
)
def test_strip_county_suffix(value, expected):
    assert strip_county_suffix(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Bayamón zona urbana", "Bayamón"),
        ("Aguas Claras comunidad", "Aguas Claras"),
        ("Tamuning-Tumon-Harmon village", "Tamuning-Tumon-Harmon"),
    ],
)
def test_clean_geo_name_strips_territory_suffixes(value, expected):
    assert clean_geo_name(value) == expected
