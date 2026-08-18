"""Generation of the dashboard map layers from Census boundary files.

The build produces three artifacts under ``data/processed/geo``:

``states.geojson``
    One lightly simplified polygon per state, used for the national view.

``zcta/<ST>.geojson``
    Simplified ZIP Code Tabulation Area polygons for one state. These exist for
    drawing and for hit-testing in the browser only.

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
from .utils import sha256_file


LOGGER = logging.getLogger(__name__)

STATE_SHAPEFILE_COLUMNS = ("STATEFP", "STUSPS", "NAME")
ZCTA_SHAPEFILE_COLUMNS = ("ZCTA5CE20",)
CROSSWALK_FILENAME = "tab20_zcta520_county20_natl.txt"
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


def _locate_crosswalk(settings: Settings) -> Path:
    """Return a cached ZCTA-to-county relationship file, downloading if absent."""

    candidates = sorted(settings.raw_dir.rglob(CROSSWALK_FILENAME))
    if candidates:
        return candidates[0]
    from .downloader import download_file
    from .models import SourceSpec

    spec = SourceSpec(
        key="census_county",
        agency="U.S. Census Bureau",
        dataset_name="2020 Census ZCTA-to-County Relationship File",
        fiscal_year=0,
        url=settings.census_county_url,
        filename=CROSSWALK_FILENAME,
        expected_extensions=(".txt",),
        expected_content_types=("text/plain", "application/octet-stream"),
    )
    metadata = download_file(spec, settings.geo_raw_dir, settings)
    assert metadata.local_path is not None
    return metadata.local_path


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

    crosswalk = _locate_crosswalk(settings)
    LOGGER.info("Assigning ZCTAs to states using %s", crosswalk.name)
    zcta_states = zcta_state_assignment(crosswalk, fips_to_usps)

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
                    },
                    "geometry": geometry,
                }
            )
    finally:
        zcta_shapes.close()

    geo_dir = settings.geo_dir
    (geo_dir / "zcta").mkdir(parents=True, exist_ok=True)
    for state, features in sorted(by_state.items()):
        features.sort(key=lambda feature: feature["id"])
        _write_geojson(geo_dir / "zcta" / f"{state}.geojson", features)
        LOGGER.info("Wrote %s with %d ZCTAs", f"{state}.geojson", len(features))

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
        "states": [
            {
                "state": code,
                "name": state_names.get(code, code),
                "zcta_count": counts[code],
                "rated_zip_count": covered[code],
            }
            for code in sorted(by_state)
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
        source_points=source_points,
        written_points=written_points,
    )
