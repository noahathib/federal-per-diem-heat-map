from decimal import Decimal

import pandas as pd

from federal_per_diem.gsa_parser import parse_gsa_file


def test_gsa_parser_preserves_leading_zip_and_months(tmp_path, source_metadata):
    path = tmp_path / "gsa.xlsx"
    row = {
        "DestinationID": "42",
        "Name": "Seasonal City",
        "County": "Example County, MA",
        "LocationDefined": "Example",
        "State": "MA",
        "Zip": "01234",
        "FiscalYear": "2026",
        "Oct": "100",
        "Nov": "100",
        "Dec": "100",
        "Jan": "125",
        "Feb": "125",
        "Mar": "125",
        "Apr": "150",
        "May": "150",
        "Jun": "150",
        "Jul": "150",
        "Aug": "150",
        "Sep": "100",
        "Meals": "80",
    }
    pd.DataFrame([row]).to_excel(path, index=False)
    records, _ = parse_gsa_file(path, source_metadata, expected_fiscal_year=2026)
    january = next(record for record in records if record.month == 1)
    august = next(record for record in records if record.month == 8)
    assert january.zip_code == "01234"
    assert january.lodging_rate == Decimal("125.00")
    assert august.lodging_rate == Decimal("150.00")


def test_gsa_parser_returns_twelve_records(tmp_path, source_metadata):
    path = tmp_path / "gsa.xlsx"
    months = {month: "100" for month in "Oct Nov Dec Jan Feb Mar Apr May Jun Jul Aug Sep".split()}
    frame = pd.DataFrame(
        [{
            "DestinationID": "0", "Name": "Standard Rate", "County": "",
            "LocationDefined": "", "State": "MA", "Zip": "01234",
            "FiscalYear": "2026", **months, "Meals": "68",
        }]
    )
    frame.to_excel(path, index=False)
    records, updated = parse_gsa_file(path, source_metadata, expected_fiscal_year=2026)
    assert len(records) == 12
    assert {record.zip_code for record in records} == {"01234"}
    assert records[0].is_standard
    assert records[0].lodging_rate == Decimal("100.00")
    assert records[0].first_last_day_mie == Decimal("51.00")
    assert updated.record_count == 12
