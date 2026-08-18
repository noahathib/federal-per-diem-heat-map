"""Normalize source-specific locality rates into canonical ZIP/date intervals."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .census_parser import GeoMapping
from .dod_parser import DODSeasonRate
from .exceptions import DataValidationError
from .models import NormalizedRate
from .utils import (
    date_to_fiscal_year,
    first_last_day,
    fold_name,
    month_range,
    strip_county_suffix,
)


ALASKA_ALIASES = {
    "UTQIAGVIK": "BARROW",
    "UNALASKA": "DUTCH HARBOR-UNALASKA",
    "KENAI": "KENAI-SOLDOTNA",
    "SOLDOTNA": "KENAI-SOLDOTNA",
    "SITKA": "SITKA-MT. EDGECUMBE",
    "SAINT GEORGE": "ST. GEORGE",
}

# The DTMO area name for each USPS code the Census crosswalk can produce.
DTMO_AREA_NAMES = {
    "AK": "ALASKA",
    "HI": "HAWAII",
    "AS": "AMERICAN SAMOA",
    "GU": "GUAM",
    "MP": "NORTHERN MARIANA ISLANDS",
    "PR": "PUERTO RICO",
    "VI": "VIRGIN ISLANDS (U.S.)",
}

CATCH_ALL_LOCALITY = "[OTHER]"

# Localities that exist only as a military installation. A civilian ZCTA is
# never resolved to one of these, however close the installation is; DTMO's
# published catch-all is the correct answer for the surrounding civilian
# geography. This is the same rule the Alaska and Hawaii policies already
# follow, stated explicitly so the territory tables cannot violate it.
INSTALLATION_ONLY_LOCALITIES = frozenset(
    {
        "CAMP BLAZ",
        "JOINT REGION MARIANAS (ANDERSEN)",
        "JOINT REGION MARIANAS (NAVAL BASE)",
        "FT. BUCHANAN [INCL GSA SVC CTR, GUAYNABO]",
        "SABANA SECA [INCL ALL MILITARY]",
        "LUIS MUNOZ MARIN IAP AGS",
    }
)

# Puerto Rico's DTMO localities are named for municipios, so the municipio (the
# Census county equivalent) is what decides the rate -- not the Census "place",
# which in Puerto Rico is a zona urbana or comunidad sitting inside a municipio.
# Two localities carry a bracketed installation that the locality includes; the
# locality is still the civil municipality, so matching its civil name is exact.
PUERTO_RICO_MUNICIPIO_LOCALITIES = {
    "AGUADILLA": "AGUADILLA",
    "BAYAMON": "BAYAMON",
    "CAROLINA": "CAROLINA",
    "CEIBA": "CEIBA",
    "CULEBRA": "CULEBRA",
    "FAJARDO": "FAJARDO [INCL ROOSEVELT RDS NAVSTAT]",
    "HUMACAO": "HUMACAO",
    "LUQUILLO": "LUQUILLO",
    "MAYAGUEZ": "MAYAGUEZ",
    "PONCE": "PONCE",
    "RIO GRANDE": "RIO GRANDE",
    "SAN JUAN": "SAN JUAN & NAV RES STA",
    "VIEQUES": "VIEQUES",
}

# The U.S. Virgin Islands and the Northern Mariana Islands price by island, and
# their Census county equivalents *are* those islands, so the county GEOID is an
# exact key rather than a name match.
VIRGIN_ISLANDS_LOCALITIES = {
    "78010": "ST. CROIX",
    "78020": "ST. JOHN",
    "78030": "ST. THOMAS",
}

NORTHERN_MARIANA_LOCALITIES = {
    "69100": "ROTA",
    "69110": "SAIPAN",
    "69120": "TINIAN",
}

# Guam and American Samoa each publish one territory-wide civilian locality and
# sit in a single Census county equivalent, so every ZCTA resolves to it. Guam
# also publishes TAMUNING and three installations at the identical rate; naming
# the territory-wide locality avoids inferring a narrower one from ZIP geography.
GUAM_LOCALITY = "GUAM (INCL ALL MIL INSTAL)"
AMERICAN_SAMOA_LOCALITY = "AMERICAN SAMOA"


def _name_key(value: str | None) -> str:
    return fold_name(value)


def _confirm(candidate: str | None, available_localities: set[str]) -> str | None:
    """Accept a candidate only if DTMO published it, else fall back honestly.

    Returns the area's published catch-all when the candidate is absent, and
    ``None`` when the area publishes no catch-all -- there is then no defensible
    rate for the ZIP and the caller must refuse to invent one.
    """

    if candidate is not None and candidate in INSTALLATION_ONLY_LOCALITIES:
        raise DataValidationError(
            f"Refusing to resolve a civilian ZIP to installation locality {candidate}"
        )
    if candidate is not None and candidate in available_localities:
        return candidate
    if CATCH_ALL_LOCALITY in available_localities:
        return CATCH_ALL_LOCALITY
    return None


def _resolve_alaska(mapping: GeoMapping, available_localities: set[str]) -> str | None:
    place = (mapping.place or "").upper()
    candidate = ALASKA_ALIASES.get(place, place)
    if candidate in available_localities:
        return candidate
    candidate_key = _name_key(candidate)
    for locality in available_localities:
        if _name_key(locality) == candidate_key:
            return locality
    return _confirm(None, available_localities)


def _resolve_hawaii(mapping: GeoMapping, available_localities: set[str]) -> str | None:
    place = (mapping.place or "").upper()
    place_key = _name_key(place)
    if place_key in {"URBAN HONOLULU", "HONOLULU"}:
        return _confirm("HONOLULU", available_localities)
    if place_key == "KAPOLEI":
        return _confirm("KAPOLEI", available_localities)
    if place_key in {"LIHUE", "LIHUE EAST"}:
        return _confirm("LIHUE", available_localities)
    if mapping.county_geoid == "15001":
        return _confirm(
            "ISLE OF HAWAII: HILO"
            if place_key == "HILO"
            else "ISLE OF HAWAII: LOCATIONS OTHER THAN HILO",
            available_localities,
        )
    if mapping.county_geoid == "15003":
        return _confirm("ISLE OF OAHU", available_localities)
    if mapping.county_geoid == "15007":
        return _confirm("ISLE OF KAUAI", available_localities)
    if mapping.county_geoid == "15005":
        return _confirm("ISLE OF MOLOKAI", available_localities)
    if mapping.county_geoid == "15009":
        subdivision = _name_key(mapping.county_subdivision)
        if "LANAI" in subdivision:
            return _confirm("ISLE OF LANAI", available_localities)
        if "MOLOKAI" in subdivision:
            return _confirm("ISLE OF MOLOKAI", available_localities)
        return _confirm("ISLE OF MAUI", available_localities)
    return _confirm(None, available_localities)


def _resolve_puerto_rico(
    mapping: GeoMapping, available_localities: set[str]
) -> str | None:
    municipio = _name_key(strip_county_suffix(mapping.county))
    return _confirm(
        PUERTO_RICO_MUNICIPIO_LOCALITIES.get(municipio), available_localities
    )


def resolve_dod_locality(
    mapping: GeoMapping,
    available_localities: set[str],
) -> str | None:
    """Resolve a Census ZCTA mapping to the DTMO locality that prices it.

    Returns the area's published catch-all when no specific locality applies,
    or ``None`` when the area publishes no catch-all and nothing matched.
    """

    if mapping.state == "AK":
        return _resolve_alaska(mapping, available_localities)
    if mapping.state == "HI":
        return _resolve_hawaii(mapping, available_localities)
    if mapping.state == "PR":
        return _resolve_puerto_rico(mapping, available_localities)
    if mapping.state == "VI":
        return _confirm(
            VIRGIN_ISLANDS_LOCALITIES.get(mapping.county_geoid), available_localities
        )
    if mapping.state == "MP":
        return _confirm(
            NORTHERN_MARIANA_LOCALITIES.get(mapping.county_geoid), available_localities
        )
    if mapping.state == "GU":
        return _confirm(GUAM_LOCALITY, available_localities)
    if mapping.state == "AS":
        return _confirm(AMERICAN_SAMOA_LOCALITY, available_localities)
    raise DataValidationError(
        f"No DTMO locality policy for state {mapping.state!r} (ZIP {mapping.zip_code})"
    )


def _snapshot_for_month(
    rows: list[DODSeasonRate], target_start: date
) -> date:
    snapshots = sorted({row.publication_date for row in rows})
    eligible = [snapshot for snapshot in snapshots if snapshot <= target_start]
    return eligible[-1] if eligible else snapshots[0]


def _rate_for_day(
    rows: list[DODSeasonRate],
    state_name: str,
    locality: str,
    snapshot: date,
    day: date,
) -> DODSeasonRate:
    candidates = [
        row
        for row in rows
        if row.state == state_name
        and row.locality == locality
        and row.publication_date == snapshot
        and row.applies_on(day)
    ]
    if not candidates and locality != CATCH_ALL_LOCALITY:
        candidates = [
            row
            for row in rows
            if row.state == state_name
            and row.locality == CATCH_ALL_LOCALITY
            and row.publication_date == snapshot
            and row.applies_on(day)
        ]
    if len(candidates) != 1:
        raise DataValidationError(
            f"Expected one DTMO rate for {state_name}/{locality} on {day} "
            f"in snapshot {snapshot}, found {len(candidates)}"
        )
    return candidates[0]


def normalize_dod_rates(
    rows: list[DODSeasonRate],
    crosswalk: dict[str, GeoMapping],
    fiscal_year: int,
) -> list[NormalizedRate]:
    """Expand DTMO localities to non-CONUS ZCTAs and exact date intervals."""

    if not rows:
        raise DataValidationError("No DTMO rows supplied")
    latest_snapshot = max(row.publication_date for row in rows)
    latest_localities: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row.publication_date == latest_snapshot:
            latest_localities[row.state].add(row.locality)

    output: list[NormalizedRate] = []
    unresolved: list[str] = []
    for mapping in crosswalk.values():
        try:
            state_name = DTMO_AREA_NAMES[mapping.state]
        except KeyError:
            raise DataValidationError(
                f"Census crosswalk produced unsupported area {mapping.state!r} "
                f"for ZIP {mapping.zip_code}"
            ) from None
        if state_name not in latest_localities:
            raise DataValidationError(
                f"DTMO snapshot {latest_snapshot} publishes no localities for "
                f"{state_name}, but the Census crosswalk has {mapping.state} ZIPs"
            )
        locality = resolve_dod_locality(mapping, latest_localities[state_name])
        if locality is None:
            # The area publishes no catch-all row, so there is no published rate
            # for this ZIP. Record it and fail below rather than guess one.
            unresolved.append(
                f"{mapping.zip_code} ({mapping.state}, {mapping.county})"
            )
            continue
        for month in (10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9):
            year = fiscal_year - 1 if month >= 10 else fiscal_year
            month_start, month_end = month_range(year, month)
            if date_to_fiscal_year(month_start) != fiscal_year:
                continue
            snapshot = _snapshot_for_month(rows, month_start)
            day = month_start
            segment_start = day
            current = _rate_for_day(rows, state_name, locality, snapshot, day)
            while day < month_end:
                next_day = day + timedelta(days=1)
                following = _rate_for_day(
                    rows, state_name, locality, snapshot, next_day
                )
                if (
                    following.lodging_rate,
                    following.mie_rate,
                    following.source_file,
                ) != (current.lodging_rate, current.mie_rate, current.source_file):
                    output.append(
                        _canonical_dod_rate(
                            mapping, locality, fiscal_year, month, segment_start, day, current
                        )
                    )
                    segment_start = next_day
                    current = following
                day = next_day
            output.append(
                _canonical_dod_rate(
                    mapping, locality, fiscal_year, month, segment_start, month_end, current
                )
            )
    if unresolved:
        raise DataValidationError(
            f"{len(unresolved)} non-CONUS ZIPs matched no published DTMO locality "
            "and their area publishes no catch-all row: "
            + ", ".join(sorted(unresolved)[:20])
        )
    return output


def _canonical_dod_rate(
    mapping: GeoMapping,
    locality: str,
    fiscal_year: int,
    month: int,
    effective_start: date,
    effective_end: date,
    source: DODSeasonRate,
) -> NormalizedRate:
    mie = source.mie_rate
    return NormalizedRate(
        zip_code=mapping.zip_code,
        state=mapping.state,
        city=mapping.place,
        county=mapping.county,
        primary_destination=locality,
        locality=locality,
        destination_id=f"DTMO:{mapping.state}:{_name_key(locality).replace(' ', '-')}",
        fiscal_year=fiscal_year,
        month=month,
        effective_start=effective_start,
        effective_end=effective_end,
        lodging_rate=source.lodging_rate,
        mie_rate=mie,
        first_last_day_mie=first_last_day(mie),
        source_agency="DoD/DTMO",
        source_file=source.source_file,
        source_url=source.source_url,
        source_retrieved_at=source.source_retrieved_at,
        source_sha256=source.source_sha256,
        is_standard=locality == CATCH_ALL_LOCALITY,
    )
