"""Multi-day travel allowance calculations built on ZIP/date lookup."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .models import TripEstimate
from .lookup import get_per_diem
from .utils import MONEY_QUANTUM, money, normalize_zip, parse_date


def estimate_trip(
    zip_code: str | int,
    start_date: date | datetime | str,
    end_date: date | datetime | str,
    *,
    travelers: int = 1,
    mileage: Decimal | float | int | str | None = None,
    mileage_rate: Decimal | float | int | str | None = None,
    database_path: Path | str | None = None,
) -> TripEstimate:
    """Estimate lodging, M&IE, and optional explicit mileage reimbursement."""

    normalized_zip = normalize_zip(zip_code)
    start = parse_date(start_date)
    end = parse_date(end_date)
    if end < start:
        raise ValueError("end_date cannot precede start_date")
    if not isinstance(travelers, int) or isinstance(travelers, bool) or travelers < 1:
        raise ValueError("travelers must be a positive integer")
    if (mileage is None) != (mileage_rate is None):
        raise ValueError("mileage and mileage_rate must be supplied together")

    travel_days = (end - start).days + 1
    nightly: list[tuple[date, Decimal]] = []
    current = start
    while current < end:
        rate = get_per_diem(normalized_zip, current, database_path=database_path)
        nightly.append((current, rate.lodging_rate))
        current += timedelta(days=1)
    lodging_total = sum((rate for _, rate in nightly), Decimal("0.00"))

    first_rate = get_per_diem(normalized_zip, start, database_path=database_path)
    first_mie = first_rate.first_last_day_mie
    if start == end:
        last_mie = Decimal("0.00")
        full_mie_total = Decimal("0.00")
        full_days = 0
    else:
        last_rate = get_per_diem(normalized_zip, end, database_path=database_path)
        last_mie = last_rate.first_last_day_mie
        full_mie_total = Decimal("0.00")
        cursor = start + timedelta(days=1)
        while cursor < end:
            full_mie_total += get_per_diem(
                normalized_zip, cursor, database_path=database_path
            ).mie_rate
            cursor += timedelta(days=1)
        full_days = max(travel_days - 2, 0)
    mie_total = first_mie + last_mie + full_mie_total

    mileage_total = Decimal("0.00")
    if mileage is not None and mileage_rate is not None:
        miles = Decimal(str(mileage))
        rate_per_mile = money(mileage_rate)
        assert rate_per_mile is not None
        if miles < 0 or rate_per_mile < 0:
            raise ValueError("mileage and mileage_rate cannot be negative")
        mileage_total = (miles * rate_per_mile).quantize(MONEY_QUANTUM)
    per_person = (lodging_total + mie_total + mileage_total).quantize(MONEY_QUANTUM)
    group = (per_person * travelers).quantize(MONEY_QUANTUM)
    return TripEstimate(
        zip_code=normalized_zip,
        start_date=start,
        end_date=end,
        travelers=travelers,
        travel_days=travel_days,
        lodging_nights=len(nightly),
        lodging_allowance=lodging_total,
        full_mie_days=full_days,
        first_day_mie=first_mie,
        last_day_mie=last_mie,
        full_day_mie=full_mie_total,
        total_mie=mie_total,
        mileage_allowance=mileage_total,
        per_person_total=per_person,
        group_total=group,
        nightly_lodging=tuple(nightly),
    )
