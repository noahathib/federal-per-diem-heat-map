from __future__ import annotations

import struct

import numpy as np
import pytest

from federal_per_diem.exceptions import DataValidationError, SchemaChangeError
from federal_per_diem.shapefile_reader import (
    PolygonShapefile,
    read_dbf_table,
    read_index,
    read_polygon_shapefile,
)


SQUARE = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
TRIANGLE = [(2.0, 2.0), (2.0, 3.0), (3.0, 2.0), (2.0, 2.0)]


@pytest.fixture
def sample_shapefile(tmp_path, shapefile_factory):
    return shapefile_factory(
        tmp_path, "sample", [[SQUARE], [TRIANGLE]], ["19103", "19104"]
    )


def test_reads_polygons_and_attributes(sample_shapefile):
    shapes, attributes = read_polygon_shapefile(
        sample_shapefile, columns=["ZCTA5CE20"]
    )
    try:
        assert len(shapes) == 2
        assert [row["ZCTA5CE20"] for row in attributes] == ["19103", "19104"]
        first = shapes.polygon(0)
        assert len(first.rings) == 1
        assert first.point_count == 5
        assert first.bounding_box == (0.0, 0.0, 1.0, 1.0)
        np.testing.assert_allclose(first.rings[0], np.array(SQUARE))
    finally:
        shapes.close()


def test_multi_ring_polygon_is_split_into_parts(tmp_path, shapefile_factory):
    shp = shapefile_factory(tmp_path, "rings", [[SQUARE, TRIANGLE]], ["10001"])
    shapes, _ = read_polygon_shapefile(shp)
    try:
        polygon = shapes.polygon(0)
        assert len(polygon.rings) == 2
        assert polygon.rings[0].shape == (5, 2)
        assert polygon.rings[1].shape == (4, 2)
    finally:
        shapes.close()


def test_rings_survive_closing_the_file(sample_shapefile):
    """Ring arrays must own their data, not view a closed memory map."""

    shapes, _ = read_polygon_shapefile(sample_shapefile)
    ring = shapes.polygon(0).rings[0]
    shapes.close()
    assert float(ring[2][0]) == 1.0


def test_index_offsets_are_byte_positions(sample_shapefile):
    index = read_index(sample_shapefile.with_suffix(".shx"))
    assert index.shape == (2, 2)
    assert int(index[0, 0]) == 100


def test_dbf_reports_fields_and_strips_padding(sample_shapefile):
    fields, records = read_dbf_table(sample_shapefile.with_suffix(".dbf"))
    assert [field.name for field in fields] == ["ZCTA5CE20"]
    assert records[0]["ZCTA5CE20"] == "19103"


def test_missing_dbf_column_is_a_schema_change(sample_shapefile):
    with pytest.raises(SchemaChangeError, match="GEOID20"):
        read_dbf_table(sample_shapefile.with_suffix(".dbf"), columns=["GEOID20"])


def test_attribute_count_mismatch_is_rejected(tmp_path, shapefile_factory):
    shp = shapefile_factory(tmp_path, "mismatch", [[SQUARE], [TRIANGLE]], ["19103"])
    with pytest.raises(SchemaChangeError, match="attribute records"):
        read_polygon_shapefile(shp)


def test_bad_file_code_is_rejected(sample_shapefile):
    raw = bytearray(sample_shapefile.read_bytes())
    raw[0:4] = struct.pack(">i", 1234)
    sample_shapefile.write_bytes(bytes(raw))
    with pytest.raises(SchemaChangeError, match="file code"):
        PolygonShapefile(sample_shapefile)


def test_non_polygon_shape_type_is_rejected(tmp_path, shapefile_factory):
    shp = shapefile_factory(tmp_path, "points", [[SQUARE]], ["19103"])
    raw = bytearray(shp.read_bytes())
    raw[32:36] = struct.pack("<i", 1)
    shp.write_bytes(bytes(raw))
    with pytest.raises(SchemaChangeError, match="polygons"):
        PolygonShapefile(shp)


def test_truncated_file_is_rejected(sample_shapefile):
    raw = sample_shapefile.read_bytes()
    sample_shapefile.write_bytes(raw[:-16])
    with pytest.raises(SchemaChangeError, match="declares"):
        PolygonShapefile(sample_shapefile)


def test_out_of_range_ring_index_is_rejected(tmp_path, shapefile_factory):
    shp = shapefile_factory(tmp_path, "broken", [[SQUARE]], ["19103"])
    raw = bytearray(shp.read_bytes())
    # The parts array starts 44 bytes into the record contents at offset 108.
    raw[108 + 44 : 108 + 48] = struct.pack("<i", 99)
    shp.write_bytes(bytes(raw))
    shapes = PolygonShapefile(shp)
    try:
        with pytest.raises(DataValidationError, match="out-of-range"):
            shapes.polygon(0)
    finally:
        shapes.close()


def test_context_manager_closes_the_map(sample_shapefile):
    with PolygonShapefile(sample_shapefile) as shapes:
        assert len(shapes) == 2
    assert shapes._view is None
