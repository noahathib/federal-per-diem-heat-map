from __future__ import annotations

import json

import numpy as np
import pytest

from federal_per_diem.exceptions import DataValidationError
from federal_per_diem.geo_lookup import ZctaGeometryIndex

# A 10x10 block with a 2x2 hole in the middle, plus a detached neighbour.
BLOCK = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
HOLE = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
NEIGHBOUR = [(20, 0), (20, 5), (25, 5), (25, 0), (20, 0)]


@pytest.fixture
def geo_dir(tmp_path, shapefile_factory):
    """Build a miniature but structurally real set of map artifacts."""

    source = tmp_path / "source"
    source.mkdir()
    shp = shapefile_factory(
        source, "zcta", [[BLOCK, HOLE], [NEIGHBOUR]], ["10001", "10002"]
    )

    geo = tmp_path / "geo"
    (geo / "zcta").mkdir(parents=True)
    np.savez_compressed(
        geo / "index.npz",
        zip_codes=np.array(["10001", "10002"], dtype="<U5"),
        states=np.array(["NY", "NY"], dtype="<U2"),
        bounds=np.array([[0.0, 0.0, 10.0, 10.0], [20.0, 0.0, 25.0, 5.0]]),
        records=np.array([0, 1], dtype=np.int32),
    )
    (geo / "manifest.json").write_text(
        json.dumps(
            {
                "zcta_shapefile": str(shp),
                "zcta_count": 2,
                "states": [{"state": "NY", "name": "New York", "zcta_count": 2}],
            }
        ),
        encoding="utf-8",
    )
    return geo


@pytest.fixture
def index(geo_dir):
    reader = ZctaGeometryIndex(geo_dir)
    yield reader
    reader.close()


def test_point_inside_a_polygon_resolves_exactly(index):
    result = index.resolve(2.0, 2.0)
    assert result.zip_code == "10001"
    assert result.state == "NY"
    assert result.exact is True
    assert result.distance_km == 0.0


def test_point_inside_a_hole_is_not_inside_the_polygon(index):
    """The hole is enclosed by the block, so an even-odd test must exclude it."""

    result = index.resolve(5.0, 5.0)
    assert result.exact is False
    assert result.zip_code == "10001"


def test_detached_polygon_resolves_to_its_own_zip(index):
    assert index.resolve(2.0, 22.0).zip_code == "10002"


def test_point_between_polygons_falls_back_to_the_nearest(index):
    result = index.resolve(2.0, 15.0)
    assert result.exact is False
    assert result.zip_code in {"10001", "10002"}
    assert result.distance_km > 0


def test_bounding_box_overlap_alone_does_not_decide(index):
    """(9.5, 9.5) is in the block's box and inside it; (11, 11) is in neither."""

    assert index.resolve(9.5, 9.5).exact is True
    assert index.resolve(11.0, 11.0).exact is False


def test_out_of_range_coordinates_are_rejected(index):
    with pytest.raises(ValueError):
        index.resolve(95.0, 0.0)
    with pytest.raises(ValueError):
        index.resolve(0.0, 200.0)
    with pytest.raises(ValueError):
        index.resolve(float("nan"), 0.0)


def test_zip_entry_reports_bounds_as_latitude_longitude(index):
    entry = index.zip_entry("10001")
    assert entry["state"] == "NY"
    assert entry["bounds"] == [[0.0, 0.0], [10.0, 10.0]]
    assert entry["center"] == [5.0, 5.0]


def test_unknown_zip_has_no_entry(index):
    assert index.zip_entry("99999") is None
    assert index.has_zip("10001") and not index.has_zip("99999")


def test_state_summaries_come_from_the_manifest(index):
    assert index.state_summaries() == [
        {"state": "NY", "name": "New York", "zcta_count": 2}
    ]


def test_missing_map_artifacts_raise(tmp_path):
    with pytest.raises(DataValidationError, match="build_map_data"):
        ZctaGeometryIndex(tmp_path / "absent")


def test_missing_source_shapefile_is_reported(geo_dir):
    manifest = json.loads((geo_dir / "manifest.json").read_text())
    manifest["zcta_shapefile"] = str(geo_dir / "gone.shp")
    (geo_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    reader = ZctaGeometryIndex(geo_dir)
    try:
        assert reader.shapefile_available is False
        with pytest.raises(DataValidationError, match="missing"):
            reader.resolve(2.0, 2.0)
    finally:
        reader.close()
