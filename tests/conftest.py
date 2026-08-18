from __future__ import annotations

import struct
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from federal_per_diem.models import NormalizedRate, SourceMetadata
from federal_per_diem.utils import first_last_day


# --------------------------------------------------------------------------
# Synthetic ESRI shapefiles
#
# These build byte-for-byte valid polygon shapefiles so the reader is exercised
# against the real binary layout rather than a mock.
# --------------------------------------------------------------------------


def _polygon_record(rings):
    points = [point for ring in rings for point in ring]
    starts, cursor = [], 0
    for ring in rings:
        starts.append(cursor)
        cursor += len(ring)
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    body = struct.pack("<i", 5)
    body += struct.pack("<4d", min(xs), min(ys), max(xs), max(ys))
    body += struct.pack("<ii", len(rings), len(points))
    body += b"".join(struct.pack("<i", start) for start in starts)
    body += b"".join(struct.pack("<2d", x, y) for x, y in points)
    return body


def _shapefile_header(shape_type, length_bytes, bounds):
    raw = struct.pack(">i", 9994) + b"\x00" * 20
    raw += struct.pack(">i", length_bytes // 2)
    raw += struct.pack("<ii", 1000, shape_type)
    raw += struct.pack("<4d", *bounds)
    raw += struct.pack("<4d", 0.0, 0.0, 0.0, 0.0)
    return raw


def _dbf_bytes(field_name, values, width=5):
    header = struct.pack(
        "<BBBBIHH", 0x03, 26, 1, 1, len(values), 32 + 32 + 1, width + 1
    )
    header += b"\x00" * 20
    descriptor = field_name.encode("ascii").ljust(11, b"\x00")
    descriptor += b"C" + b"\x00" * 4 + bytes([width]) + b"\x00" * 15
    body = b"".join(b" " + value.encode("ascii").ljust(width) for value in values)
    return header + descriptor + b"\x0d" + body + b"\x1a"


def write_shapefile(directory, name, records, attributes, field="ZCTA5CE20"):
    """Write a minimal polygon .shp/.shx/.dbf triple; return the .shp path."""

    bodies = [_polygon_record(rings) for rings in records]
    offsets, cursor, payload = [], 100, b""
    for index, body in enumerate(bodies, start=1):
        offsets.append((cursor, len(body)))
        payload += struct.pack(">ii", index, len(body) // 2) + body
        cursor += 8 + len(body)

    bounds = (0.0, 0.0, 30.0, 30.0)
    (directory / f"{name}.shp").write_bytes(
        _shapefile_header(5, 100 + len(payload), bounds) + payload
    )
    index_payload = b"".join(
        struct.pack(">ii", offset // 2, length // 2) for offset, length in offsets
    )
    (directory / f"{name}.shx").write_bytes(
        _shapefile_header(5, 100 + len(index_payload), bounds) + index_payload
    )
    (directory / f"{name}.dbf").write_bytes(_dbf_bytes(field, attributes))
    return directory / f"{name}.shp"


@pytest.fixture
def shapefile_factory():
    """Return the synthetic shapefile writer."""

    return write_shapefile


BLOCK = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
HOLE = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
NEIGHBOUR = [(20, 0), (20, 5), (25, 5), (25, 0), (20, 0)]


def build_map_artifacts(geo_dir, shapefile_path):
    """Write an index, manifest, and one state layer for a two-ZCTA map."""

    import json

    import numpy as np

    (geo_dir / "zcta").mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        geo_dir / "index.npz",
        zip_codes=np.array(["10001", "10002"], dtype="<U5"),
        states=np.array(["NY", "NY"], dtype="<U2"),
        bounds=np.array([[0.0, 0.0, 10.0, 10.0], [20.0, 0.0, 25.0, 5.0]]),
        records=np.array([0, 1], dtype=np.int32),
    )
    (geo_dir / "manifest.json").write_text(
        json.dumps(
            {
                "zcta_shapefile": str(shapefile_path),
                "zcta_count": 2,
                "unmapped_zcta_count": 0,
                "generated_at": "2026-08-17T00:00:00+00:00",
                "simplify_tolerance_degrees": 0.001,
                "coordinate_reference_system": "GCS_North_American_1983",
                "states": [{"state": "NY", "name": "New York", "zcta_count": 2}],
                "sources": [],
            }
        ),
        encoding="utf-8",
    )
    (geo_dir / "states.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    (geo_dir / "zcta" / "NY.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    return geo_dir


@pytest.fixture
def map_data_dir(tmp_path):
    """A data directory laid out like a real one, with synthetic map layers."""

    source = tmp_path / "raw" / "geo"
    source.mkdir(parents=True)
    shapefile = write_shapefile(
        source, "zcta", [[BLOCK, HOLE], [NEIGHBOUR]], ["10001", "10002"]
    )
    build_map_artifacts(tmp_path / "processed" / "geo", shapefile)
    return tmp_path


@pytest.fixture
def source_metadata(tmp_path):
    path = tmp_path / "source.xlsx"
    path.write_bytes(b"test source")
    return SourceMetadata(
        agency="GSA",
        dataset_name="Test source",
        fiscal_year=2026,
        source_url="https://example.gov/source.xlsx",
        downloaded_at=datetime(2025, 8, 14, tzinfo=timezone.utc),
        filename=path.name,
        sha256="a" * 64,
        file_size=path.stat().st_size,
        local_path=path,
        validation_status="valid",
    )


def make_rate(
    *,
    zip_code="19103",
    state="PA",
    locality="Philadelphia",
    destination_id="317",
    fiscal_year=2026,
    month=8,
    start=date(2026, 8, 1),
    end=date(2026, 8, 31),
    lodging="200.00",
    mie="80.00",
    source_sha="a" * 64,
    source_file="source.xlsx",
    agency="GSA",
    standard=False,
):
    mie_value = Decimal(mie)
    return NormalizedRate(
        zip_code=zip_code,
        state=state,
        city=None if standard else locality,
        county=None,
        primary_destination=locality,
        locality=locality,
        destination_id=destination_id,
        fiscal_year=fiscal_year,
        month=month,
        effective_start=start,
        effective_end=end,
        lodging_rate=Decimal(lodging),
        mie_rate=mie_value,
        first_last_day_mie=first_last_day(mie_value),
        source_agency=agency,
        source_file=source_file,
        source_url="https://example.gov/source.xlsx",
        source_retrieved_at=datetime(2025, 8, 14, tzinfo=timezone.utc),
        source_sha256=source_sha,
        is_standard=standard,
    )

