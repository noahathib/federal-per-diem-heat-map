"""Resolution of a map coordinate to a ZIP Code Tabulation Area.

Hit-testing runs against the unmodified Census ``.shp`` file rather than the
simplified polygons the browser draws. The generated index supplies a bounding
box per ZCTA, which narrows a click to a handful of candidates; each candidate
is then tested exactly with an even-odd ray cast over its full-precision rings.
Display simplification therefore cannot change which ZIP a click resolves to.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import Settings
from .exceptions import DataValidationError
from .geo_builder import INDEX_FILENAME, MANIFEST_FILENAME
from .shapefile_reader import PolygonShapefile


EARTH_RADIUS_KM = 6371.0088
NEAREST_CANDIDATES = 24


@dataclass(frozen=True, slots=True)
class PointResolution:
    """Outcome of resolving one clicked coordinate."""

    zip_code: str
    state: str
    latitude: float
    longitude: float
    exact: bool
    distance_km: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "zip": self.zip_code,
            "state": self.state,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "exact": self.exact,
            "distance_km": round(self.distance_km, 4),
        }


def _ring_crossings(ring: np.ndarray, longitude: float, latitude: float) -> int:
    """Return how many ring edges a ray cast west from the point crosses."""

    y1 = ring[:-1, 1]
    y2 = ring[1:, 1]
    straddles = (y1 > latitude) != (y2 > latitude)
    if not straddles.any():
        return 0
    x1 = ring[:-1, 0][straddles]
    x2 = ring[1:, 0][straddles]
    ya = y1[straddles]
    yb = y2[straddles]
    crossing_x = x1 + (latitude - ya) * (x2 - x1) / (yb - ya)
    return int(np.count_nonzero(longitude < crossing_x))


def _point_in_polygon(rings: tuple[np.ndarray, ...], longitude: float, latitude: float) -> bool:
    crossings = sum(_ring_crossings(ring, longitude, latitude) for ring in rings)
    return crossings % 2 == 1


def _degrees_to_km(dx: float, dy: float, latitude: float) -> float:
    scale = math.cos(math.radians(latitude))
    return math.radians(math.hypot(dx * scale, dy)) * EARTH_RADIUS_KM


class ZctaGeometryIndex:
    """Read-only index over the generated map layers and the source shapefile."""

    def __init__(
        self,
        geo_dir: Path | str | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or Settings.from_env()
        self.geo_dir = Path(geo_dir) if geo_dir else settings.geo_dir
        index_path = self.geo_dir / INDEX_FILENAME
        manifest_path = self.geo_dir / MANIFEST_FILENAME
        if not index_path.exists() or not manifest_path.exists():
            raise DataValidationError(
                f"Map layers are missing from {self.geo_dir}; "
                "run scripts/build_map_data.py first"
            )
        self.manifest: dict[str, Any] = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        with np.load(index_path, allow_pickle=False) as payload:
            self.zip_codes = payload["zip_codes"]
            self.states = payload["states"]
            self.bounds = payload["bounds"]
            self.records = payload["records"]
        self._position = {
            str(code): position for position, code in enumerate(self.zip_codes)
        }
        self._shapefile_path = Path(str(self.manifest["zcta_shapefile"]))
        self._shapes: PolygonShapefile | None = None
        self._lock = threading.Lock()

    @property
    def shapefile_available(self) -> bool:
        """Whether the source shapefile is present for exact hit-testing."""

        return self._shapefile_path.exists()

    def _open(self) -> PolygonShapefile:
        if self._shapes is None:
            if not self.shapefile_available:
                raise DataValidationError(
                    f"Source shapefile {self._shapefile_path} is missing; "
                    "re-run scripts/build_map_data.py"
                )
            self._shapes = PolygonShapefile(self._shapefile_path)
        return self._shapes

    def close(self) -> None:
        with self._lock:
            if self._shapes is not None:
                self._shapes.close()
                self._shapes = None

    def state_summaries(self) -> list[dict[str, Any]]:
        """Return the per-state summary recorded at build time."""

        return list(self.manifest.get("states", []))

    def has_zip(self, zip_code: str) -> bool:
        return zip_code in self._position

    def state_for_zip(self, zip_code: str) -> str | None:
        """Return the display state assigned to a generated ZCTA layer."""

        position = self._position.get(zip_code)
        if position is None:
            return None
        return str(self.states[position]) or None

    def zip_entry(self, zip_code: str) -> dict[str, Any] | None:
        """Return the state and bounding box recorded for *zip_code*."""

        position = self._position.get(zip_code)
        if position is None:
            return None
        xmin, ymin, xmax, ymax = (float(value) for value in self.bounds[position])
        return {
            "zip": zip_code,
            "state": str(self.states[position]) or None,
            "bounds": [[ymin, xmin], [ymax, xmax]],
            "center": [(ymin + ymax) / 2.0, (xmin + xmax) / 2.0],
        }

    def resolve(self, latitude: float, longitude: float) -> PointResolution | None:
        """Return the ZCTA containing the point, or the nearest one if none does."""

        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise ValueError("latitude and longitude must be finite numbers")
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            raise ValueError("latitude or longitude is outside the valid range")

        inside = np.nonzero(
            (self.bounds[:, 0] <= longitude)
            & (longitude <= self.bounds[:, 2])
            & (self.bounds[:, 1] <= latitude)
            & (latitude <= self.bounds[:, 3])
        )[0]
        with self._lock:
            shapes = self._open()
            for position in inside:
                polygon = shapes.polygon(int(self.records[position]))
                if _point_in_polygon(polygon.rings, longitude, latitude):
                    return PointResolution(
                        zip_code=str(self.zip_codes[position]),
                        state=str(self.states[position]),
                        latitude=latitude,
                        longitude=longitude,
                        exact=True,
                    )
            return self._nearest(shapes, latitude, longitude)

    def _nearest(
        self, shapes: PolygonShapefile, latitude: float, longitude: float
    ) -> PointResolution | None:
        if self.bounds.shape[0] == 0:
            return None
        dx = np.maximum(
            np.maximum(self.bounds[:, 0] - longitude, longitude - self.bounds[:, 2]), 0.0
        )
        dy = np.maximum(
            np.maximum(self.bounds[:, 1] - latitude, latitude - self.bounds[:, 3]), 0.0
        )
        scale = math.cos(math.radians(latitude))
        approximate = np.hypot(dx * scale, dy)
        limit = min(NEAREST_CANDIDATES, approximate.shape[0])
        candidates = np.argpartition(approximate, limit - 1)[:limit]

        best_position, best_distance = None, math.inf
        for position in candidates:
            polygon = shapes.polygon(int(self.records[position]))
            for ring in polygon.rings:
                offsets = ring - np.array([longitude, latitude])
                distance = float(
                    np.min(np.hypot(offsets[:, 0] * scale, offsets[:, 1]))
                )
                if distance < best_distance:
                    best_distance, best_position = distance, position
        if best_position is None:
            return None
        return PointResolution(
            zip_code=str(self.zip_codes[best_position]),
            state=str(self.states[best_position]),
            latitude=latitude,
            longitude=longitude,
            exact=False,
            distance_km=math.radians(best_distance) * EARTH_RADIUS_KM,
        )
