"""Census ZCTA crosswalk adapter for the ZIP-addressable non-CONUS areas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .exceptions import DataValidationError, SchemaChangeError
from .utils import clean_geo_name


# The non-CONUS areas that have USPS ZIP codes, and therefore ZCTAs, and are
# priced by DTMO rather than by the GSA CONUS workbook. Foreign localities are
# deliberately absent: they have no ZIP code and no Census geography.
STATE_FIPS = {
    "02": "AK",
    "15": "HI",
    "60": "AS",
    "66": "GU",
    "69": "MP",
    "72": "PR",
    "78": "VI",
}


@dataclass(frozen=True, slots=True)
class GeoMapping:
    """Best-area Census place/county mapping for one ZCTA."""

    zip_code: str
    state: str
    place: str | None
    county: str
    county_geoid: str
    county_subdivision: str | None = None


def _read_relationship(path: Path | str, required: set[str]) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, sep="|", dtype=str, encoding="utf-8-sig")
    except Exception as exc:
        raise DataValidationError(f"Cannot read Census relationship file {path}: {exc}") from exc
    missing = required - set(frame.columns)
    if missing:
        raise SchemaChangeError(
            f"Census relationship file {Path(path).name} is missing {sorted(missing)}"
        )
    return frame


def _largest_part(frame: pd.DataFrame, group_column: str) -> pd.DataFrame:
    working = frame.copy()
    land = pd.to_numeric(working["AREALAND_PART"], errors="coerce").fillna(0)
    water = pd.to_numeric(working["AREAWATER_PART"], errors="coerce").fillna(0)
    working["_part_area"] = land + water
    working = working.sort_values([group_column, "_part_area"], ascending=[True, False])
    return working.drop_duplicates(group_column, keep="first")


def parse_census_crosswalk(
    place_path: Path | str,
    county_path: Path | str,
    cousub_path: Path | str,
) -> dict[str, GeoMapping]:
    """Build the best-area non-CONUS ZCTA-to-locality crosswalk."""

    base = {
        "GEOID_ZCTA5_20",
        "AREALAND_PART",
        "AREAWATER_PART",
    }
    counties = _read_relationship(
        county_path, base | {"GEOID_COUNTY_20", "NAMELSAD_COUNTY_20"}
    )
    counties = counties[
        counties["GEOID_ZCTA5_20"].notna()
        & counties["GEOID_COUNTY_20"].str[:2].isin(STATE_FIPS)
    ]
    counties = _largest_part(counties, "GEOID_ZCTA5_20")
    if counties.empty:
        raise DataValidationError(
            "Census county crosswalk has no ZCTAs for "
            f"{', '.join(sorted(set(STATE_FIPS.values())))}"
        )

    places = _read_relationship(
        place_path, base | {"GEOID_PLACE_20", "NAMELSAD_PLACE_20"}
    )
    places = places[
        places["GEOID_ZCTA5_20"].notna()
        & places["GEOID_PLACE_20"].str[:2].isin(STATE_FIPS)
    ]
    places = _largest_part(places, "GEOID_ZCTA5_20")
    place_map = places.set_index("GEOID_ZCTA5_20")["NAMELSAD_PLACE_20"].to_dict()

    cousubs = _read_relationship(
        cousub_path, base | {"GEOID_COUSUB_20", "NAMELSAD_COUSUB_20"}
    )
    cousubs = cousubs[
        cousubs["GEOID_ZCTA5_20"].notna()
        & cousubs["GEOID_COUSUB_20"].str[:2].isin(STATE_FIPS)
    ]
    cousubs = _largest_part(cousubs, "GEOID_ZCTA5_20")
    cousub_map = cousubs.set_index("GEOID_ZCTA5_20")["NAMELSAD_COUSUB_20"].to_dict()

    output: dict[str, GeoMapping] = {}
    for row in counties.to_dict(orient="records"):
        zip_code = str(row["GEOID_ZCTA5_20"]).zfill(5)
        county_geoid = str(row["GEOID_COUNTY_20"])
        output[zip_code] = GeoMapping(
            zip_code=zip_code,
            state=STATE_FIPS[county_geoid[:2]],
            place=clean_geo_name(place_map.get(zip_code)),
            county=str(row["NAMELSAD_COUNTY_20"]).strip(),
            county_geoid=county_geoid,
            county_subdivision=clean_geo_name(cousub_map.get(zip_code)),
        )
    return output
