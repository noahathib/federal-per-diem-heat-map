from __future__ import annotations

import numpy as np
import pytest

from federal_per_diem.exceptions import SchemaChangeError
from federal_per_diem.geo_builder import (
    polygon_to_geometry,
    simplify_ring,
    zcta_state_assignment,
)
from federal_per_diem.shapefile_reader import Polygon


def ring(points):
    return np.array(points, dtype=np.float64)


# Shapefile outer rings run clockwise; holes run counterclockwise.
OUTER_CW = ring([(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)])
HOLE_CCW = ring([(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)])
SECOND_CW = ring([(20, 20), (20, 24), (24, 24), (24, 20), (20, 20)])


def signed_area(coordinates):
    total = 0.0
    for (x1, y1), (x2, y2) in zip(coordinates, coordinates[1:]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


# --------------------------------------------------------------- simplify

def test_simplify_drops_collinear_points():
    line = ring([(0, 0), (1, 0), (2, 0), (3, 0), (3, 3), (0, 3), (0, 0)])
    simplified = simplify_ring(line, 0.001)
    assert len(simplified) < len(line)
    assert tuple(simplified[0]) == (0.0, 0.0)
    assert tuple(simplified[-1]) == (0.0, 0.0)


def test_simplify_keeps_detail_above_the_tolerance():
    spike = ring([(0, 0), (1, 0), (2, 5), (3, 0), (4, 0), (0, 0)])
    kept = simplify_ring(spike, 0.5)
    assert any(abs(point[1] - 5) < 1e-9 for point in kept)


def test_simplify_is_a_no_op_below_four_points():
    triangle = ring([(0, 0), (1, 0), (0, 1), (0, 0)])
    np.testing.assert_array_equal(simplify_ring(triangle, 10.0), triangle)


def test_simplify_never_collapses_a_ring_below_four_points():
    square = ring([(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)])
    assert len(simplify_ring(square, 1000.0)) >= 4


def test_zero_tolerance_preserves_every_vertex():
    np.testing.assert_array_equal(simplify_ring(OUTER_CW, 0.0), OUTER_CW)


# ------------------------------------------------------------- geometry

def test_single_ring_becomes_a_counterclockwise_polygon():
    geometry, count = polygon_to_geometry(
        Polygon((0, 0, 10, 10), (OUTER_CW,)), tolerance=0.0, decimals=5
    )
    assert geometry["type"] == "Polygon"
    assert count == 5
    # RFC 7946 requires a counterclockwise exterior ring.
    assert signed_area(geometry["coordinates"][0]) > 0


def test_hole_follows_its_outer_ring_and_winds_clockwise():
    geometry, _ = polygon_to_geometry(
        Polygon((0, 0, 10, 10), (OUTER_CW, HOLE_CCW)), tolerance=0.0, decimals=5
    )
    assert geometry["type"] == "Polygon"
    assert len(geometry["coordinates"]) == 2
    assert signed_area(geometry["coordinates"][0]) > 0
    assert signed_area(geometry["coordinates"][1]) < 0


def test_two_outer_rings_become_a_multipolygon():
    geometry, _ = polygon_to_geometry(
        Polygon((0, 0, 24, 24), (OUTER_CW, HOLE_CCW, SECOND_CW)),
        tolerance=0.0,
        decimals=5,
    )
    assert geometry["type"] == "MultiPolygon"
    assert len(geometry["coordinates"]) == 2
    assert len(geometry["coordinates"][0]) == 2
    assert len(geometry["coordinates"][1]) == 1


def test_rings_are_closed_after_rounding():
    geometry, _ = polygon_to_geometry(
        Polygon((0, 0, 10, 10), (OUTER_CW,)), tolerance=0.0, decimals=1
    )
    ring_out = geometry["coordinates"][0]
    assert ring_out[0] == ring_out[-1]


def test_coordinates_honour_the_decimal_setting():
    precise = ring(
        [(0.123456, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.123456, 0.0)]
    )
    geometry, _ = polygon_to_geometry(
        Polygon((0, 0, 1, 1), (precise,)), tolerance=0.0, decimals=3
    )
    assert 0.123 in [abs(value) for point in geometry["coordinates"][0] for value in point]


def test_polygon_without_rings_yields_no_geometry():
    geometry, count = polygon_to_geometry(
        Polygon((0, 0, 0, 0), ()), tolerance=0.0, decimals=5
    )
    assert geometry is None and count == 0


# --------------------------------------------------- state assignment

CROSSWALK_HEADER = (
    "GEOID_ZCTA5_20|GEOID_COUNTY_20|AREALAND_PART|AREAWATER_PART\n"
)


def write_crosswalk(tmp_path, rows):
    path = tmp_path / "tab20_zcta520_county20_natl.txt"
    path.write_text(CROSSWALK_HEADER + "".join(rows), encoding="utf-8")
    return path


def test_zcta_is_assigned_to_its_largest_area_state(tmp_path):
    path = write_crosswalk(
        tmp_path,
        [
            "12345|42001|100|0\n",   # PA, small
            "12345|34001|900|0\n",   # NJ, large
        ],
    )
    assignment = zcta_state_assignment(path, {"42": "PA", "34": "NJ"})
    assert assignment["12345"] == "NJ"


def test_water_area_counts_toward_the_largest_part(tmp_path):
    path = write_crosswalk(
        tmp_path,
        [
            "12345|42001|100|900\n",
            "12345|34001|500|0\n",
        ],
    )
    assert zcta_state_assignment(path, {"42": "PA", "34": "NJ"})["12345"] == "PA"


def test_parts_within_one_state_are_summed(tmp_path):
    path = write_crosswalk(
        tmp_path,
        [
            "12345|42001|400|0\n",
            "12345|42003|400|0\n",
            "12345|34001|500|0\n",
        ],
    )
    assert zcta_state_assignment(path, {"42": "PA", "34": "NJ"})["12345"] == "PA"


def test_unknown_state_fips_is_omitted(tmp_path):
    path = write_crosswalk(tmp_path, ["96910|66010|100|0\n"])
    assert zcta_state_assignment(path, {"42": "PA"}) == {}


def test_leading_zero_zctas_are_preserved(tmp_path):
    path = write_crosswalk(tmp_path, ["01001|25013|100|0\n"])
    assert zcta_state_assignment(path, {"25": "MA"}) == {"01001": "MA"}


def test_missing_crosswalk_column_is_a_schema_change(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("GEOID_ZCTA5_20|GEOID_COUNTY_20\n12345|42001\n", encoding="utf-8")
    with pytest.raises(SchemaChangeError, match="AREALAND_PART"):
        zcta_state_assignment(path, {"42": "PA"})
