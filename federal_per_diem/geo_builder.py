"""Generation of the dashboard map layers from Census boundary files.

The build produces state-split drawing layers under ``data/processed/geo``:

``states.geojson``
    One lightly simplified polygon per state, used for the national view.

``zcta/<ST>.geojson``
    Simplified ZIP Code Tabulation Area polygons for one state. These exist for
    drawing and for hit-testing in the browser only.

``counties/<ST>.geojson`` and ``municipal/<ST>.geojson``
    Current Census county, county-subdivision, and place boundaries. Municipal
    features preserve the Census legal/statistical area description in their
    display name instead of treating every subdivision as a township.

``localities/<ST>.geojson``
    GSA rate-area outlines only where the developer workbook defines a locality
    as one or more complete counties. Exceptions and city-only definitions are
    deliberately omitted rather than approximated with ZCTAs.

``index.npz`` and ``manifest.json``
    A ZIP-to-record table with bounding boxes, plus source provenance. The index
    lets a lookup seek straight into the unmodified government ``.shp`` file, so
    the authoritative geometry is never the simplified copy.

Simplification uses the Douglas-Peucker algorithm at a tolerance in decimal
degrees. The default of 0.001 degrees is roughly 111 metres at the equator and
sits well inside the generalization already present in a 1:500,000 cartographic
boundary file, so it removes redundant vertices rather than real detail.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .config import Settings
from .downloader import download_boundaries
from .exceptions import DataValidationError, SchemaChangeError
from .shapefile_reader import Polygon, read_dbf_table, read_polygon_shapefile
from .utils import fold_name, sha256_file


LOGGER = logging.getLogger(__name__)

STATE_SHAPEFILE_COLUMNS = ("STATEFP", "STUSPS", "NAME")
ZCTA_SHAPEFILE_COLUMNS = ("ZCTA5CE20",)
COUNTY_SHAPEFILE_COLUMNS = (
    "STATEFP",
    "GEOID",
    "NAME",
    "NAMELSAD",
    "STUSPS",
    "STATE_NAME",
    "LSAD",
)
COUSUB_SHAPEFILE_COLUMNS = (
    "STATEFP",
    "COUNTYFP",
    "GEOID",
    "NAME",
    "NAMELSAD",
    "STUSPS",
    "NAMELSADCO",
    "STATE_NAME",
    "LSAD",
    "ALAND",
    "AWATER",
)
PLACE_SHAPEFILE_COLUMNS = (
    "STATEFP",
    "GEOID",
    "NAME",
    "NAMELSAD",
    "STUSPS",
    "STATE_NAME",
    "LSAD",
    "ALAND",
    "AWATER",
)
CROSSWALK_FILENAME = "tab20_zcta520_county20_natl.txt"
RELATIONSHIP_FILES = {
    "county": (
        CROSSWALK_FILENAME,
        "GEOID_COUNTY_20",
        "AREALAND_PART",
        "AREAWATER_PART",
    ),
    "cousub": (
        "tab20_zcta520_cousub20_natl.txt",
        "GEOID_COUSUB_20",
        "AREALAND_PART",
        "AREAWATER_PART",
    ),
    "place": (
        "tab20_zcta520_place20_natl.txt",
        "GEOID_PLACE_20",
        "AREALAND_PART",
        "AREAWATER_PART",
    ),
}
INDEX_FILENAME = "index.npz"
MANIFEST_FILENAME = "manifest.json"
STATES_FILENAME = "states.geojson"


@dataclass(frozen=True, slots=True)
class MapBuildResult:
    """Summary of one map-layer build."""

    geo_dir: Path
    zcta_count: int
    state_count: int
    mapped_zcta_count: int
    unmapped_zcta_count: int
    database_zip_count: int
    covered_zip_count: int
    county_count: int
    municipal_count: int
    locality_part_count: int
    source_points: int
    written_points: int

    def to_dict(self) -> dict[str, object]:
        return {
            "geo_dir": str(self.geo_dir),
            "zcta_count": self.zcta_count,
            "state_count": self.state_count,
            "mapped_zcta_count": self.mapped_zcta_count,
            "unmapped_zcta_count": self.unmapped_zcta_count,
            "database_zip_count": self.database_zip_count,
            "covered_zip_count": self.covered_zip_count,
            "county_count": self.county_count,
            "municipal_count": self.municipal_count,
            "locality_part_count": self.locality_part_count,
            "source_points": self.source_points,
            "written_points": self.written_points,
        }


def simplify_ring(points: np.ndarray, tolerance: float) -> np.ndarray:
    """Return *points* thinned by Douglas-Peucker, preserving the closed ring.

    The iterative form avoids Python recursion limits on rings that carry
    several thousand vertices. A ring that would fall below the four points a
    closed ring requires is returned unchanged.
    """

    count = int(points.shape[0])
    if count <= 4 or tolerance <= 0:
        return points
    keep = np.zeros(count, dtype=bool)
    keep[0] = keep[count - 1] = True
    stack: list[tuple[int, int]] = [(0, count - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        segment = points[start + 1 : end]
        ax, ay = points[start]
        bx, by = points[end]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length == 0.0:
            distance = np.hypot(segment[:, 0] - ax, segment[:, 1] - ay)
        else:
            distance = (
                np.abs(dy * (segment[:, 0] - ax) - dx * (segment[:, 1] - ay)) / length
            )
        farthest = int(np.argmax(distance))
        if distance[farthest] > tolerance:
            split = start + 1 + farthest
            keep[split] = True
            stack.append((start, split))
            stack.append((split, end))
    thinned = points[keep]
    return points if thinned.shape[0] < 4 else thinned


def _signed_area(ring: np.ndarray) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)) / 2.0


def _oriented(ring: np.ndarray, counterclockwise: bool) -> np.ndarray:
    return ring if (_signed_area(ring) > 0) == counterclockwise else ring[::-1]


def polygon_to_geometry(
    polygon: Polygon,
    *,
    tolerance: float,
    decimals: int,
) -> tuple[dict[str, object] | None, int]:
    """Convert a shapefile polygon to a GeoJSON geometry and its vertex count.

    Shapefile rings are clockwise for outer boundaries and counterclockwise for
    holes. RFC 7946 uses the opposite convention, so rings are re-oriented
    rather than copied through.
    """

    groups: list[list[np.ndarray]] = []
    for ring in polygon.rings:
        simplified = simplify_ring(ring, tolerance)
        if _signed_area(simplified) < 0 or not groups:
            groups.append([_oriented(simplified, True)])
        else:
            groups[-1].append(_oriented(simplified, False))
    if not groups:
        return None, 0

    written = 0
    encoded: list[list[list[list[float]]]] = []
    for group in groups:
        rings: list[list[list[float]]] = []
        for ring in group:
            rounded = np.round(ring, decimals)
            coordinates = [[float(x), float(y)] for x, y in rounded]
            if coordinates[0] != coordinates[-1]:
                coordinates.append(list(coordinates[0]))
            if len(coordinates) < 4:
                continue
            written += len(coordinates)
            rings.append(coordinates)
        if rings:
            encoded.append(rings)
    if not encoded:
        return None, 0
    if len(encoded) == 1:
        return {"type": "Polygon", "coordinates": encoded[0]}, written
    return {"type": "MultiPolygon", "coordinates": encoded}, written


def _extract_archive(archive: Path, destination: Path) -> Path:
    """Extract *archive* and return the path of the contained ``.shp`` file."""

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        for name in names:
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise DataValidationError(f"{archive.name} contains unsafe path {name}")
        shp_names = [name for name in names if name.lower().endswith(".shp")]
        if len(shp_names) != 1:
            raise SchemaChangeError(
                f"{archive.name} holds {len(shp_names)} .shp members; expected one"
            )
        stem = Path(shp_names[0]).stem
        required = {f"{stem}{suffix}" for suffix in (".shp", ".shx", ".dbf")}
        available = {Path(name).name for name in names}
        missing = required - available
        if missing:
            raise SchemaChangeError(f"{archive.name} is missing {sorted(missing)}")
        # The .prj is not needed to read geometry but records the source CRS.
        wanted = required | {f"{stem}.prj"}
        for name in names:
            if Path(name).name in wanted:
                target = destination / Path(name).name
                if not target.exists() or target.stat().st_size == 0:
                    with bundle.open(name) as source, target.open("wb") as sink:
                        sink.write(source.read())
    return destination / f"{stem}.shp"


def _locate_relationship(settings: Settings, kind: str) -> Path:
    """Return a cached Census ZCTA relationship file, downloading if absent."""

    try:
        filename = RELATIONSHIP_FILES[kind][0]
    except KeyError as exc:
        raise ValueError(f"Unknown ZCTA relationship kind {kind!r}") from exc
    candidates = sorted(settings.raw_dir.rglob(filename))
    if candidates:
        return candidates[-1]
    from .downloader import download_file
    from .models import SourceSpec

    spec = SourceSpec(
        key=f"census_{kind}",
        agency="U.S. Census Bureau",
        dataset_name=f"2020 Census ZCTA-to-{kind.title()} Relationship File",
        fiscal_year=0,
        url=getattr(settings, f"census_{kind}_url"),
        filename=filename,
        expected_extensions=(".txt",),
        expected_content_types=("text/plain", "application/octet-stream"),
    )
    metadata = download_file(spec, settings.geo_raw_dir, settings)
    assert metadata.local_path is not None
    return metadata.local_path


def _locate_crosswalk(settings: Settings) -> Path:
    """Compatibility wrapper for the ZCTA-to-county relationship file."""

    return _locate_relationship(settings, "county")


def zcta_relationships(
    path: Path | str,
    geoid_column: str,
    land_column: str = "AREALAND_PART",
    water_column: str = "AREAWATER_PART",
) -> dict[str, list[str]]:
    """Return intersecting GEOIDs for each ZCTA, ordered by shared area.

    All positive-area intersections are retained. This is used by ZIP search to
    disclose that ZIP, county, and municipal systems can disagree instead of
    assigning a single jurisdiction from the ZCTA's centre.
    """

    frame = pd.read_csv(path, sep="|", dtype=str, encoding="utf-8-sig")
    required = {"GEOID_ZCTA5_20", geoid_column, land_column, water_column}
    missing = required - set(frame.columns)
    if missing:
        raise SchemaChangeError(f"{Path(path).name} is missing {sorted(missing)}")
    frame = frame[
        frame["GEOID_ZCTA5_20"].notna() & frame[geoid_column].notna()
    ].copy()
    frame["_area"] = (
        pd.to_numeric(frame[land_column], errors="coerce").fillna(0)
        + pd.to_numeric(frame[water_column], errors="coerce").fillna(0)
    )
    frame = frame[frame["_area"] > 0]
    frame["_zcta"] = frame["GEOID_ZCTA5_20"].str.zfill(5)
    frame["_geoid"] = frame[geoid_column].str.strip()
    grouped = (
        frame.groupby(["_zcta", "_geoid"], as_index=False)["_area"]
        .sum()
        .sort_values(["_zcta", "_area", "_geoid"], ascending=[True, False, True])
    )
    result: dict[str, list[str]] = {}
    for zcta, rows in grouped.groupby("_zcta", sort=True):
        result[str(zcta)] = [str(value) for value in rows["_geoid"]]
    return result


def zcta_state_assignment(
    crosswalk_path: Path | str,
    fips_to_usps: dict[str, str],
) -> dict[str, str]:
    """Map each ZCTA to the state holding its largest intersecting area.

    A ZCTA may straddle a state line. Grouping it under its largest-area state
    matches the policy the rate pipeline already uses for Census parts and only
    affects which map layer draws it; the ZIP itself stays resolvable either way.
    """

    frame = pd.read_csv(crosswalk_path, sep="|", dtype=str, encoding="utf-8-sig")
    required = {"GEOID_ZCTA5_20", "GEOID_COUNTY_20", "AREALAND_PART", "AREAWATER_PART"}
    missing = required - set(frame.columns)
    if missing:
        raise SchemaChangeError(
            f"{Path(crosswalk_path).name} is missing {sorted(missing)}"
        )
    frame = frame[frame["GEOID_ZCTA5_20"].notna() & frame["GEOID_COUNTY_20"].notna()]
    area = pd.to_numeric(frame["AREALAND_PART"], errors="coerce").fillna(0) + (
        pd.to_numeric(frame["AREAWATER_PART"], errors="coerce").fillna(0)
    )
    grouped = pd.DataFrame(
        {
            "zcta": frame["GEOID_ZCTA5_20"].str.zfill(5),
            "fips": frame["GEOID_COUNTY_20"].str[:2],
            "area": area,
        }
    )
    totals = grouped.groupby(["zcta", "fips"], as_index=False)["area"].sum()
    totals = totals.sort_values(["zcta", "area"], ascending=[True, False])
    best = totals.drop_duplicates("zcta", keep="first")
    return {
        str(row.zcta): fips_to_usps[str(row.fips)]
        for row in best.itertuples()
        if str(row.fips) in fips_to_usps
    }


def _database_zip_codes(database_path: Path) -> set[str]:
    if not database_path.exists():
        LOGGER.warning("Rate database %s not found; coverage flags omitted", database_path)
        return set()
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT DISTINCT zip_code FROM locations").fetchall()
    return {str(row[0]) for row in rows}


def _write_geojson(path: Path, features: Sequence[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "features": list(features)}
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
    temporary.replace(path)


def _display_area_name(name: str, namelsad: str) -> tuple[str, str]:
    """Return a readable Census display name and its published area type."""

    raw_name = str(name).strip()
    published = str(namelsad).strip() or raw_name
    suffix = published[len(raw_name) :].strip() if published.startswith(raw_name) else ""
    area_type = suffix.title() if suffix else "Geographic Area"
    area_type = re.sub(r"\b(Cdp|Ccd|Ut)\b", lambda match: match.group(1).upper(), area_type)
    display = f"{raw_name} {area_type}" if suffix else published
    return display, area_type


def _municipal_priority(source_type: str, area_type: str) -> int:
    """Order overlapping municipal references without claiming legal status."""

    folded = fold_name(area_type)
    if source_type == "place":
        return 2 if folded == "CDP" else 5
    governmental_words = {
        "BOROUGH",
        "CITY",
        "CITY AND BOROUGH",
        "MUNICIPALITY",
        "MUNICIPIO",
        "TOWN",
        "TOWNSHIP",
        "VILLAGE",
    }
    return 4 if folded in governmental_words else 1


def _convert_boundary_layer(
    shapefile: Path,
    *,
    columns: Sequence[str],
    tolerance: float,
    decimals: int,
    selected: set[str] | None,
    feature_builder: object,
) -> tuple[dict[str, list[dict[str, object]]], int, int]:
    """Convert one national Census polygon file into state feature buckets."""

    shapes, rows = read_polygon_shapefile(shapefile, columns=list(columns))
    by_state: dict[str, list[dict[str, object]]] = {}
    source_points = 0
    written_points = 0
    seen: set[str] = set()
    try:
        for record, row in enumerate(rows):
            state = row.get("STUSPS", "").upper()
            if not state or (selected is not None and state not in selected):
                continue
            geoid = row.get("GEOID", "")
            if not geoid or geoid in seen:
                raise DataValidationError(
                    f"{shapefile.name} repeats or omits a required GEOID"
                )
            seen.add(geoid)
            polygon = shapes.polygon(record)
            source_points += polygon.point_count
            geometry, count = polygon_to_geometry(
                polygon, tolerance=tolerance, decimals=decimals
            )
            if geometry is None:
                continue
            written_points += count
            feature = feature_builder(row, geometry)  # type: ignore[operator]
            by_state.setdefault(state, []).append(feature)
    finally:
        shapes.close()
    return by_state, source_points, written_points


def _county_feature(row: dict[str, str], geometry: dict[str, object]) -> dict[str, object]:
    return {
        "type": "Feature",
        "id": row["GEOID"],
        "properties": {
            "geoid": row["GEOID"],
            "name": row["NAME"],
            "displayName": row["NAMELSAD"],
            "state": row["STUSPS"],
            "stateName": row["STATE_NAME"],
            "type": "County or equivalent",
            "lsad": row["LSAD"],
            "sourceType": "county",
        },
        "geometry": geometry,
    }


def _municipal_feature(
    row: dict[str, str],
    geometry: dict[str, object],
    *,
    source_type: str,
) -> dict[str, object]:
    display_name, area_type = _display_area_name(row["NAME"], row["NAMELSAD"])
    properties: dict[str, object] = {
        "geoid": row["GEOID"],
        "name": row["NAME"],
        "displayName": display_name,
        "type": area_type,
        "state": row["STUSPS"],
        "stateName": row["STATE_NAME"],
        "lsad": row["LSAD"],
        "sourceType": source_type,
        "priority": _municipal_priority(source_type, area_type),
        "areaLand": int(row["ALAND"] or 0),
        "areaWater": int(row["AWATER"] or 0),
    }
    if source_type == "county_subdivision":
        properties["county"] = row["NAMELSADCO"]
        properties["countyGeoid"] = row["STATEFP"] + row["COUNTYFP"]
    return {
        "type": "Feature",
        "id": f"{'cousub' if source_type == 'county_subdivision' else 'place'}:{row['GEOID']}",
        "properties": properties,
        "geometry": geometry,
    }


_COUNTY_SUFFIX = re.compile(
    r"\s+(CITY AND BOROUGH|CENSUS AREA|CONSOLIDATED GOVERNMENT|MUNICIPALITY|"
    r"MUNICIPIO|BOROUGH|COUNTY|PARISH|CITY)$"
)


def _county_label(value: object, state: str) -> str:
    text = "" if pd.isna(value) else str(value).strip()
    text = re.sub(rf",\s*{re.escape(state)}\s*$", "", text, flags=re.IGNORECASE)
    return fold_name(text)


def _county_base(value: str) -> str:
    return _COUNTY_SUFFIX.sub("", fold_name(value)).strip()


def county_defined_locality_specs(
    frame: pd.DataFrame,
    county_features: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    """Return GSA localities whose published definition is complete counties.

    The function accepts only exact county sets. Definitions containing city
    limits, exclusions, installations, or other prose fail closed and receive
    no polygon.
    """

    normalized_columns = {
        re.sub(r"[^a-z0-9]", "", str(column).lower()): column
        for column in frame.columns
    }
    required = {"destinationid", "name", "county", "locationdefined", "state"}
    missing = required - set(normalized_columns)
    if missing:
        raise SchemaChangeError(
            f"GSA locality workbook is missing {sorted(missing)}"
        )
    columns = {key: normalized_columns[key] for key in required}

    lookup: dict[tuple[str, str], list[dict[str, object]]] = {}
    for state, features in county_features.items():
        for feature in features:
            properties = feature["properties"]
            assert isinstance(properties, dict)
            for label in (properties.get("displayName"), properties.get("name")):
                key = (state, fold_name(str(label or "")))
                lookup.setdefault(key, []).append(feature)

    records: list[dict[str, str]] = []
    for _, row in frame.iterrows():
        destination_id = "" if pd.isna(row[columns["destinationid"]]) else str(
            row[columns["destinationid"]]
        ).split(".0", 1)[0].strip()
        state = "" if pd.isna(row[columns["state"]]) else str(
            row[columns["state"]]
        ).strip().upper()
        if not destination_id or destination_id == "0" or not state:
            continue
        records.append(
            {
                "destinationId": destination_id,
                "state": state,
                "locality": "" if pd.isna(row[columns["name"]]) else str(
                    row[columns["name"]]
                ).strip(),
                "county": "" if pd.isna(row[columns["county"]]) else str(
                    row[columns["county"]]
                ).strip(),
                "definition": "" if pd.isna(row[columns["locationdefined"]]) else str(
                    row[columns["locationdefined"]]
                ).strip(),
            }
        )

    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for record in records:
        grouped.setdefault((record["state"], record["destinationId"]), []).append(record)

    specs: list[dict[str, object]] = []
    for (state, destination_id), rows in sorted(grouped.items()):
        definitions = {row["definition"] for row in rows if row["definition"]}
        localities = {row["locality"] for row in rows if row["locality"]}
        county_labels = {row["county"] for row in rows if row["county"]}
        if len(definitions) != 1 or len(localities) != 1 or not county_labels:
            continue
        resolved: list[dict[str, object]] = []
        for label in sorted(county_labels):
            matches = lookup.get((state, _county_label(label, state)), [])
            if len(matches) != 1:
                resolved = []
                break
            resolved.append(matches[0])
        if not resolved:
            continue
        definition = next(iter(definitions))
        definition_parts = {
            _county_base(part)
            for part in re.split(r"\s*/\s*", definition)
            if part.strip()
        }
        resolved_parts = {
            _county_base(str(feature["properties"]["name"]))  # type: ignore[index]
            for feature in resolved
        }
        if definition_parts != resolved_parts:
            continue
        specs.append(
            {
                "state": state,
                "destinationId": destination_id,
                "locality": next(iter(localities)),
                "definition": definition,
                "counties": resolved,
            }
        )
    return specs


def _gsa_locality_workbook(settings: Settings) -> tuple[Path, int] | None:
    if not settings.database_path.is_file():
        return None
    with closing(sqlite3.connect(settings.database_path)) as connection:
        row = connection.execute(
            """SELECT fiscal_year, filename FROM sources
               WHERE agency = 'GSA' AND dataset_name LIKE '%ZIP Code%'
               ORDER BY fiscal_year DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        return None
    fiscal_year, filename = int(row[0]), str(row[1])
    candidates = sorted((settings.raw_dir / f"FY{fiscal_year}").rglob(filename))
    return (candidates[0], fiscal_year) if candidates else None


def build_map_data(
    *,
    settings: Settings | None = None,
    database_path: Path | str | None = None,
    force_download: bool = False,
    states: Iterable[str] | None = None,
) -> MapBuildResult:
    """Download, convert, and publish the dashboard map layers."""

    settings = settings or Settings.from_env()
    database = Path(database_path) if database_path else settings.database_path
    tolerance = settings.map_simplify_tolerance
    decimals = settings.map_coordinate_decimals

    LOGGER.info("Resolving Census cartographic boundary archives")
    sources = download_boundaries(force=force_download, settings=settings)
    extracted = settings.geo_raw_dir / "extracted"
    state_shp = _extract_archive(
        settings.geo_raw_dir / sources["census_state_boundary"].filename, extracted
    )
    zcta_shp = _extract_archive(
        settings.geo_raw_dir / sources["census_zcta_boundary"].filename, extracted
    )
    county_shp = _extract_archive(
        settings.geo_raw_dir / sources["census_county_boundary"].filename, extracted
    )
    cousub_shp = _extract_archive(
        settings.geo_raw_dir / sources["census_cousub_boundary"].filename, extracted
    )
    place_shp = _extract_archive(
        settings.geo_raw_dir / sources["census_place_boundary"].filename, extracted
    )

    state_shapes, state_rows = read_polygon_shapefile(
        state_shp, columns=list(STATE_SHAPEFILE_COLUMNS)
    )
    try:
        fips_to_usps = {row["STATEFP"]: row["STUSPS"] for row in state_rows}
        state_names = {row["STUSPS"]: row["NAME"] for row in state_rows}
        state_features = []
        for record, row in enumerate(state_rows):
            geometry, _ = polygon_to_geometry(
                state_shapes.polygon(record), tolerance=tolerance, decimals=decimals
            )
            if geometry is None:
                continue
            state_features.append(
                {
                    "type": "Feature",
                    "id": row["STUSPS"],
                    "properties": {"state": row["STUSPS"], "name": row["NAME"]},
                    "geometry": geometry,
                }
            )
    finally:
        state_shapes.close()

    relationship_paths = {
        kind: _locate_relationship(settings, kind) for kind in RELATIONSHIP_FILES
    }
    crosswalk = relationship_paths["county"]
    LOGGER.info("Assigning ZCTAs to states using %s", crosswalk.name)
    zcta_states = zcta_state_assignment(crosswalk, fips_to_usps)
    relationships = {
        kind: zcta_relationships(
            relationship_paths[kind],
            RELATIONSHIP_FILES[kind][1],
            RELATIONSHIP_FILES[kind][2],
            RELATIONSHIP_FILES[kind][3],
        )
        for kind in RELATIONSHIP_FILES
    }

    zcta_shapes, zcta_rows = read_polygon_shapefile(
        zcta_shp, columns=list(ZCTA_SHAPEFILE_COLUMNS)
    )
    database_zips = _database_zip_codes(database)
    selected = {code.upper() for code in states} if states else None

    try:
        zip_codes = [row["ZCTA5CE20"] for row in zcta_rows]
        if len(set(zip_codes)) != len(zip_codes):
            raise DataValidationError("Census ZCTA boundary file repeats a ZCTA code")

        by_state: dict[str, list[dict[str, object]]] = {}
        bounds = np.zeros((len(zip_codes), 4), dtype=np.float64)
        unmapped = 0
        source_points = 0
        written_points = 0
        for record, zip_code in enumerate(zip_codes):
            polygon = zcta_shapes.polygon(record)
            bounds[record] = polygon.bounding_box
            source_points += polygon.point_count
            state = zcta_states.get(zip_code)
            if state is None:
                unmapped += 1
                continue
            if selected is not None and state not in selected:
                continue
            geometry, count = polygon_to_geometry(
                polygon, tolerance=tolerance, decimals=decimals
            )
            if geometry is None:
                continue
            written_points += count
            by_state.setdefault(state, []).append(
                {
                    "type": "Feature",
                    "id": zip_code,
                    "properties": {
                        "zip": zip_code,
                        "state": state,
                        "inDatabase": zip_code in database_zips,
                        "countyGeoids": relationships["county"].get(zip_code, []),
                        "cousubGeoids": relationships["cousub"].get(zip_code, []),
                        "placeGeoids": relationships["place"].get(zip_code, []),
                    },
                    "geometry": geometry,
                }
            )
    finally:
        zcta_shapes.close()

    LOGGER.info("Converting county boundaries")
    county_by_state, county_source_points, county_written_points = (
        _convert_boundary_layer(
            county_shp,
            columns=COUNTY_SHAPEFILE_COLUMNS,
            tolerance=tolerance,
            decimals=decimals,
            selected=selected,
            feature_builder=_county_feature,
        )
    )

    LOGGER.info("Converting county-subdivision boundaries")
    cousub_by_state, cousub_source_points, cousub_written_points = (
        _convert_boundary_layer(
            cousub_shp,
            columns=COUSUB_SHAPEFILE_COLUMNS,
            tolerance=tolerance,
            decimals=decimals,
            selected=selected,
            feature_builder=lambda row, geometry: _municipal_feature(
                row, geometry, source_type="county_subdivision"
            ),
        )
    )
    LOGGER.info("Converting incorporated-place and CDP boundaries")
    place_by_state, place_source_points, place_written_points = (
        _convert_boundary_layer(
            place_shp,
            columns=PLACE_SHAPEFILE_COLUMNS,
            tolerance=tolerance,
            decimals=decimals,
            selected=selected,
            feature_builder=lambda row, geometry: _municipal_feature(
                row, geometry, source_type="place"
            ),
        )
    )

    municipal_by_state: dict[str, list[dict[str, object]]] = {}
    for state in sorted(set(cousub_by_state) | set(place_by_state)):
        features = list(cousub_by_state.get(state, []))
        coextensive = {
            (
                fold_name(str(feature["properties"]["displayName"])),  # type: ignore[index]
                feature["properties"]["areaLand"],  # type: ignore[index]
                feature["properties"]["areaWater"],  # type: ignore[index]
            )
            for feature in features
        }
        for feature in place_by_state.get(state, []):
            key = (
                fold_name(str(feature["properties"]["displayName"])),  # type: ignore[index]
                feature["properties"]["areaLand"],  # type: ignore[index]
                feature["properties"]["areaWater"],  # type: ignore[index]
            )
            if key not in coextensive:
                features.append(feature)
        features.sort(key=lambda feature: str(feature["id"]))
        municipal_by_state[state] = features

    locality_by_state: dict[str, list[dict[str, object]]] = {
        state: [] for state in county_by_state
    }
    locality_workbook = _gsa_locality_workbook(settings)
    locality_source: dict[str, object] | None = None
    if locality_workbook is not None:
        workbook, locality_fiscal_year = locality_workbook
        LOGGER.info("Deriving county-defined GSA rate areas from %s", workbook.name)
        frame = pd.read_excel(workbook, sheet_name=0, dtype=str, engine="openpyxl")
        locality_specs = county_defined_locality_specs(frame, county_by_state)
        for spec in locality_specs:
            state = str(spec["state"])
            counties = spec["counties"]
            assert isinstance(counties, list)
            county_geoids = [
                str(feature["properties"]["geoid"])  # type: ignore[index]
                for feature in counties
            ]
            for county in counties:
                county_properties = county["properties"]
                assert isinstance(county_properties, dict)
                locality_by_state.setdefault(state, []).append(
                    {
                        "type": "Feature",
                        "id": f"{state}:{spec['destinationId']}:{county_properties['geoid']}",
                        "properties": {
                            "destinationId": spec["destinationId"],
                            "locality": spec["locality"],
                            "definition": spec["definition"],
                            "state": state,
                            "stateName": county_properties["stateName"],
                            "county": county_properties["displayName"],
                            "countyGeoid": county_properties["geoid"],
                            "countyGeoids": county_geoids,
                            "coverage": "complete-counties",
                            "sourceType": "gsa_county_defined_rate_area",
                        },
                        "geometry": county["geometry"],
                    }
                )
        locality_source = {
            "agency": "GSA",
            "dataset_name": "Per Diem ZIP Code File for Developers",
            "fiscal_year": locality_fiscal_year,
            "filename": workbook.name,
            "sha256": sha256_file(workbook),
            "file_size": workbook.stat().st_size,
        }
    else:
        LOGGER.warning("No GSA ZIP workbook found; locality boundary layers are empty")

    geo_dir = settings.geo_dir
    layer_directories = ("zcta", "counties", "municipal", "localities")
    for directory in layer_directories:
        target = geo_dir / directory
        target.mkdir(parents=True, exist_ok=True)
        if selected is None:
            for stale in target.glob("*.geojson"):
                stale.unlink()
    for state, features in sorted(by_state.items()):
        features.sort(key=lambda feature: feature["id"])
        _write_geojson(geo_dir / "zcta" / f"{state}.geojson", features)
        LOGGER.info("Wrote %s with %d ZCTAs", f"{state}.geojson", len(features))

    all_codes = {
        str(feature["id"])
        for feature in state_features
        if selected is None or str(feature["id"]) in selected
    }
    for state in sorted(all_codes):
        counties = sorted(
            county_by_state.get(state, []), key=lambda feature: str(feature["id"])
        )
        municipal = municipal_by_state.get(state, [])
        localities = sorted(
            locality_by_state.get(state, []), key=lambda feature: str(feature["id"])
        )
        _write_geojson(geo_dir / "counties" / f"{state}.geojson", counties)
        _write_geojson(geo_dir / "municipal" / f"{state}.geojson", municipal)
        _write_geojson(geo_dir / "localities" / f"{state}.geojson", localities)
        LOGGER.info(
            "Wrote %s reference geography: %d counties, %d municipal features, "
            "%d locality parts",
            state,
            len(counties),
            len(municipal),
            len(localities),
        )

    counts = {state: len(features) for state, features in by_state.items()}
    covered = {
        state: sum(
            1 for feature in features if feature["properties"]["inDatabase"]
        )
        for state, features in by_state.items()
    }
    for feature in state_features:
        code = str(feature["id"])
        feature["properties"]["zctaCount"] = counts.get(code, 0)
        feature["properties"]["ratedZipCount"] = covered.get(code, 0)
        feature["properties"]["hasLayer"] = code in by_state
        feature["properties"]["countyCount"] = len(county_by_state.get(code, []))
        feature["properties"]["municipalCount"] = len(municipal_by_state.get(code, []))
        feature["properties"]["localityPartCount"] = len(locality_by_state.get(code, []))
    if selected is None:
        _write_geojson(geo_dir / STATES_FILENAME, state_features)

    states_array = np.array(
        [zcta_states.get(code, "") for code in zip_codes], dtype="<U2"
    )
    np.savez_compressed(
        geo_dir / INDEX_FILENAME,
        zip_codes=np.array(zip_codes, dtype="<U5"),
        states=states_array,
        bounds=bounds,
        records=np.arange(len(zip_codes), dtype=np.int32),
    )

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "simplify_tolerance_degrees": tolerance,
        "coordinate_decimals": decimals,
        "coordinate_reference_system": state_shp.with_suffix(".prj").read_text(
            encoding="utf-8"
        ).strip()
        if state_shp.with_suffix(".prj").exists()
        else None,
        "zcta_count": len(zip_codes),
        "unmapped_zcta_count": unmapped,
        "database_zip_count": len(database_zips),
        "zcta_shapefile": str(zcta_shp),
        "zcta_shapefile_sha256": sha256_file(zcta_shp),
        "boundary_layers": {
            "states": {
                "vintage": 2025,
                "feature_count": len(state_features),
                "simplify_tolerance_degrees": tolerance,
            },
            "zcta": {
                "vintage": 2020,
                "feature_count": len(zip_codes) - unmapped,
                "simplify_tolerance_degrees": tolerance,
            },
            "counties": {
                "vintage": 2025,
                "feature_count": sum(len(value) for value in county_by_state.values()),
                "simplify_tolerance_degrees": tolerance,
            },
            "municipal": {
                "vintage": 2025,
                "feature_count": sum(len(value) for value in municipal_by_state.values()),
                "county_subdivision_count": sum(
                    len(value) for value in cousub_by_state.values()
                ),
                "place_count_before_coextensive_deduplication": sum(
                    len(value) for value in place_by_state.values()
                ),
                "simplify_tolerance_degrees": tolerance,
                "normalization": (
                    "Census NAMELSAD is retained as the display type; coextensive "
                    "place/county-subdivision duplicates are represented once"
                ),
            },
            "localities": {
                "feature_count": sum(len(value) for value in locality_by_state.values()),
                "method": "GSA definitions matching complete Census counties only",
                "source": locality_source,
            },
        },
        "relationship_sources": [
            {
                "agency": "U.S. Census Bureau",
                "dataset_name": f"2020 Census ZCTA-to-{kind.title()} Relationship File",
                "source_url": getattr(settings, f"census_{kind}_url"),
                "filename": path.name,
                "sha256": sha256_file(path),
                "file_size": path.stat().st_size,
                "downloaded_at": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
            for kind, path in sorted(relationship_paths.items())
        ],
        "states": [
            {
                "state": code,
                "name": state_names.get(code, code),
                "zcta_count": counts.get(code, 0),
                "rated_zip_count": covered.get(code, 0),
                "county_count": len(county_by_state.get(code, [])),
                "municipal_count": len(municipal_by_state.get(code, [])),
                "locality_part_count": len(locality_by_state.get(code, [])),
            }
            for code in sorted(all_codes)
        ],
        "sources": [
            {
                "agency": metadata.agency,
                "dataset_name": metadata.dataset_name,
                "source_url": metadata.source_url,
                "filename": metadata.filename,
                "sha256": metadata.sha256,
                "file_size": metadata.file_size,
                "downloaded_at": metadata.downloaded_at.isoformat(),
            }
            for metadata in sources.values()
        ],
    }
    (geo_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    return MapBuildResult(
        geo_dir=geo_dir,
        zcta_count=len(zip_codes),
        state_count=len(by_state),
        mapped_zcta_count=len(zip_codes) - unmapped,
        unmapped_zcta_count=unmapped,
        database_zip_count=len(database_zips),
        covered_zip_count=sum(covered.values()),
        county_count=sum(len(value) for value in county_by_state.values()),
        municipal_count=sum(len(value) for value in municipal_by_state.values()),
        locality_part_count=sum(len(value) for value in locality_by_state.values()),
        source_points=(
            source_points
            + county_source_points
            + cousub_source_points
            + place_source_points
        ),
        written_points=(
            written_points
            + county_written_points
            + cousub_written_points
            + place_written_points
        ),
    )
