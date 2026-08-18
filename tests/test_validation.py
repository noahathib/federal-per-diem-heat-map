from datetime import date

import pytest

from federal_per_diem.validation import REQUIRED_NON_CONUS_CODES, validate_rates

from conftest import make_rate


def test_validation_rejects_nonpositive_rate():
    record = make_rate(lodging="0", start=date(2026, 8, 1), end=date(2026, 8, 31))
    report = validate_rates([record], expected_fiscal_year=2026, require_all_states=False)
    assert not report.is_valid
    assert "nonpositive_rate" in {issue.code for issue in report.errors}



@pytest.mark.parametrize("state", ["PR", "GU", "VI", "AS", "MP"])
def test_validation_accepts_territory_state_codes(state):
    record = make_rate(state=state, agency="DoD/DTMO")
    report = validate_rates([record], expected_fiscal_year=2026, require_all_states=False)
    assert "invalid_state" not in {issue.code for issue in report.errors}


def test_validation_requires_every_non_conus_area():
    record = make_rate(state="PA")
    report = validate_rates([record], expected_fiscal_year=2026, require_all_states=True)
    absent = {
        issue.message.split()[0]
        for issue in report.errors
        if issue.code == "missing_oconus_state"
    }
    assert absent == set(REQUIRED_NON_CONUS_CODES)


def test_validation_counts_territories_separately_from_states():
    records = [
        make_rate(state="PA", zip_code="19103"),
        make_rate(state="PR", zip_code="00901", agency="DoD/DTMO"),
        make_rate(state="VI", zip_code="00802", agency="DoD/DTMO"),
    ]
    report = validate_rates(records, expected_fiscal_year=2026, require_all_states=False)
    assert report.metrics["state_count"] == 1
    assert report.metrics["territory_count"] == 2
