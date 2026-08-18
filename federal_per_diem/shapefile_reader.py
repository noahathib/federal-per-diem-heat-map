"""Reader for the ESRI shapefile subset published in Census boundary files.

This implements only what the Census cartographic boundary files actually use:
the Polygon shape type and the dBASE III attribute table. Field layouts follow
the ESRI Shapefile Technical Description (ESRI White Paper, July 1998):

* The 100-byte main file header stores the file code and file length in
  big-endian 32-bit integers and the version, shape type, and bounding box in
  little-endian. File length is counted in 16-bit words including the header.
* Each record has an 8-byte big-endian header of record number and content
  length, also in 16-bit words.
* A Polygon record stores a little-endian shape type, a four-double bounding
  box, ``NumParts``, ``NumPoints``, a ``NumParts`` array of ring start indexes,
  and a ``NumPoints`` array of X/Y double pairs.
* The index file repeats the 100-byte header, then one 8-byte big-endian
  offset/content-length pair per record, in 16-bit words.

Keeping a reader here means the downloaded government file stays the single
geometric authority: nothing is re-projected, rounded, or copied before it is
used to resolve a point.
"""

from __future__ import annotations

import mmap
import struct
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Iterator, Sequence

import numpy as np

from .exceptions import DataValidationError, SchemaChangeError


HEADER_SIZE = 100
RECORD_HEADER_SIZE = 8
FILE_CODE = 9994
VERSION = 1000

SHAPE_NULL = 0
SHAPE_POLYGON = 5

DBF_HEADER_SIZE = 32
DBF_FIELD_SIZE = 32
DBF_TERMINATOR = 0x0D
DBF_RECORD_PRESENT = 0x20
DBF_RECORD_DELETED = 0x2A


@dataclass(frozen=True, slots=True)
class ShapefileHeader:
    """Decoded 100-byte main or index file header."""

    file_code: int
    file_length_bytes: int
    version: int
    shape_type: int
    bounding_box: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class DbfField:
    """One dBASE field descriptor."""

    name: str
    type_code: str
    length: int
    decimals: int


@dataclass(frozen=True, slots=True)
class Polygon:
    """One polygon record as its raw rings, in file coordinate order."""

    bounding_box: tuple[float, float, float, float]
    rings: tuple[np.ndarray, ...]

    @property
    def point_count(self) -> int:
        return sum(int(ring.shape[0]) for ring in self.rings)


def parse_header(raw: bytes, source: str) -> ShapefileHeader:
    """Decode and validate a 100-byte shapefile header."""

    if len(raw) < HEADER_SIZE:
        raise SchemaChangeError(f"{source} is shorter than a 100-byte shapefile header")
    file_code, = struct.unpack_from(">i", raw, 0)
    file_length_words, = struct.unpack_from(">i", raw, 24)
    version, shape_type = struct.unpack_from("<ii", raw, 28)
    bounding_box = struct.unpack_from("<4d", raw, 36)
    if file_code != FILE_CODE:
        raise SchemaChangeError(
            f"{source} has file code {file_code}; expected {FILE_CODE}"
        )
    if version != VERSION:
        raise SchemaChangeError(f"{source} has version {version}; expected {VERSION}")
    if shape_type not in {SHAPE_NULL, SHAPE_POLYGON}:
        raise SchemaChangeError(
            f"{source} holds shape type {shape_type}; this reader supports "
            f"polygons ({SHAPE_POLYGON}) only"
        )
    if file_length_words <= 0:
        raise SchemaChangeError(f"{source} reports a non-positive file length")
    return ShapefileHeader(
        file_code=file_code,
        file_length_bytes=file_length_words * 2,
        version=version,
        shape_type=shape_type,
        bounding_box=(
            float(bounding_box[0]),
            float(bounding_box[1]),
            float(bounding_box[2]),
            float(bounding_box[3]),
        ),
    )


def read_index(shx_path: Path | str) -> np.ndarray:
    """Return an ``(n, 2)`` array of record byte offsets and byte lengths."""

    path = Path(shx_path)
    raw = path.read_bytes()
    header = parse_header(raw, path.name)
    if header.file_length_bytes != len(raw):
        raise SchemaChangeError(
            f"{path.name} declares {header.file_length_bytes} bytes but holds {len(raw)}"
        )
    body = len(raw) - HEADER_SIZE
    if body % RECORD_HEADER_SIZE:
        raise SchemaChangeError(f"{path.name} has a truncated final index record")
    words = np.frombuffer(raw, dtype=">i4", offset=HEADER_SIZE).reshape(-1, 2)
    return words.astype(np.int64) * 2


def read_dbf_table(
    dbf_path: Path | str,
    *,
    columns: Sequence[str] | None = None,
    encoding: str = "utf-8",
) -> tuple[tuple[DbfField, ...], list[dict[str, str]]]:
    """Read a dBASE III table, returning its fields and stripped string records.

    Values are returned as trimmed text. Numeric coercion is left to callers so
    that no width or padding assumption is baked into the reader.
    """

    path = Path(dbf_path)
    raw = path.read_bytes()
    if len(raw) < DBF_HEADER_SIZE:
        raise SchemaChangeError(f"{path.name} is shorter than a dBASE header")
    record_count, header_length, record_length = struct.unpack_from("<IHH", raw, 4)

    fields: list[DbfField] = []
    offset = DBF_HEADER_SIZE
    while offset < len(raw) and raw[offset] != DBF_TERMINATOR:
        if offset + DBF_FIELD_SIZE > len(raw):
            raise SchemaChangeError(f"{path.name} has a truncated field descriptor")
        descriptor = raw[offset : offset + DBF_FIELD_SIZE]
        fields.append(
            DbfField(
                name=descriptor[:11].split(b"\x00", 1)[0].decode("ascii").strip(),
                type_code=chr(descriptor[11]),
                length=descriptor[16],
                decimals=descriptor[17],
            )
        )
        offset += DBF_FIELD_SIZE
    if offset >= len(raw):
        raise SchemaChangeError(f"{path.name} has no dBASE header terminator")
    if offset + 1 != header_length:
        raise SchemaChangeError(
            f"{path.name} terminates its field descriptors at byte {offset + 1} "
            f"but declares a {header_length}-byte header"
        )
    declared = sum(field.length for field in fields) + 1
    if declared != record_length:
        raise SchemaChangeError(
            f"{path.name} field widths total {declared} bytes but records are "
            f"{record_length} bytes"
        )
    if columns is not None:
        missing = set(columns) - {field.name for field in fields}
        if missing:
            raise SchemaChangeError(f"{path.name} is missing fields {sorted(missing)}")

    wanted = set(columns) if columns is not None else {field.name for field in fields}
    spans: list[tuple[str, int, int]] = []
    cursor = 1
    for field in fields:
        if field.name in wanted:
            spans.append((field.name, cursor, cursor + field.length))
        cursor += field.length

    end = header_length + record_count * record_length
    if end > len(raw):
        raise SchemaChangeError(
            f"{path.name} declares {record_count} records but the file ends early"
        )
    records: list[dict[str, str]] = []
    for index in range(record_count):
        start = header_length + index * record_length
        row = raw[start : start + record_length]
        if row[0] == DBF_RECORD_DELETED:
            continue
        if row[0] != DBF_RECORD_PRESENT:
            raise SchemaChangeError(
                f"{path.name} record {index + 1} has deletion flag {row[0]:#04x}"
            )
        records.append(
            {
                name: row[begin:finish].decode(encoding, "replace").strip()
                for name, begin, finish in spans
            }
        )
    return tuple(fields), records


def _decode_polygon(view: memoryview, offset: int, source: str) -> Polygon:
    shape_type, = struct.unpack_from("<i", view, offset)
    if shape_type == SHAPE_NULL:
        return Polygon(bounding_box=(0.0, 0.0, 0.0, 0.0), rings=())
    if shape_type != SHAPE_POLYGON:
        raise SchemaChangeError(
            f"{source} contains shape type {shape_type}; expected {SHAPE_POLYGON}"
        )
    box = struct.unpack_from("<4d", view, offset + 4)
    part_count, point_count = struct.unpack_from("<ii", view, offset + 36)
    if part_count < 1 or point_count < 1:
        raise DataValidationError(f"{source} holds a polygon with no rings or points")
    parts = np.frombuffer(view, dtype="<i4", count=part_count, offset=offset + 44)
    # Copy out of the memory map so returned rings do not keep it exported.
    points = np.frombuffer(
        view,
        dtype="<f8",
        count=point_count * 2,
        offset=offset + 44 + 4 * part_count,
    ).reshape(-1, 2).copy()
    bounds = np.append(parts.astype(np.int64), point_count)
    rings = []
    for index in range(part_count):
        start, stop = int(bounds[index]), int(bounds[index + 1])
        if not 0 <= start < stop <= point_count:
            raise DataValidationError(f"{source} has an out-of-range ring index")
        rings.append(points[start:stop])
    return Polygon(
        bounding_box=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
        rings=tuple(rings),
    )


class PolygonShapefile(AbstractContextManager["PolygonShapefile"]):
    """Random-access reader over a polygon ``.shp`` file and its ``.shx`` index.

    The ``.shp`` payload is memory-mapped, so resolving a single record reads
    only that record instead of the whole national file.
    """

    def __init__(self, shp_path: Path | str) -> None:
        self.path = Path(shp_path)
        self.index = read_index(self.path.with_suffix(".shx"))
        self._handle = self.path.open("rb")
        try:
            self._map = mmap.mmap(self._handle.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            self._handle.close()
            raise
        self._view = memoryview(self._map)
        self.header = parse_header(bytes(self._view[:HEADER_SIZE]), self.path.name)
        actual = len(self._map)
        if self.header.file_length_bytes != actual:
            self.close()
            raise SchemaChangeError(
                f"{self.path.name} declares {self.header.file_length_bytes} bytes "
                f"but holds {actual}"
            )

    def __len__(self) -> int:
        return int(self.index.shape[0])

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the memory map and file handle."""

        view = getattr(self, "_view", None)
        if view is not None:
            view.release()
            self._view = None  # type: ignore[assignment]
        mapping = getattr(self, "_map", None)
        if mapping is not None and not mapping.closed:
            mapping.close()
        handle = getattr(self, "_handle", None)
        if handle is not None and not handle.closed:
            handle.close()

    def polygon(self, record: int) -> Polygon:
        """Return the polygon stored at zero-based *record*."""

        if not 0 <= record < len(self):
            raise IndexError(f"record {record} is outside {self.path.name}")
        offset = int(self.index[record, 0])
        content = int(self.index[record, 1])
        end = offset + RECORD_HEADER_SIZE + content
        if end > len(self._map):
            raise SchemaChangeError(
                f"{self.path.name} record {record + 1} runs past the end of the file"
            )
        number, declared = struct.unpack_from(">ii", self._view, offset)
        if number != record + 1:
            raise SchemaChangeError(
                f"{self.path.name} record at offset {offset} is numbered {number}, "
                f"not {record + 1}"
            )
        if declared * 2 != content:
            raise SchemaChangeError(
                f"{self.path.name} record {number} declares {declared * 2} content "
                f"bytes but the index reports {content}"
            )
        return _decode_polygon(
            self._view, offset + RECORD_HEADER_SIZE, f"{self.path.name} record {number}"
        )

    def polygons(self) -> Iterator[Polygon]:
        """Yield every polygon in record order."""

        for record in range(len(self)):
            yield self.polygon(record)


def read_polygon_shapefile(
    shp_path: Path | str,
    *,
    columns: Sequence[str] | None = None,
) -> tuple[PolygonShapefile, list[dict[str, str]]]:
    """Open a polygon shapefile and read its attribute table alongside it.

    The ESRI specification requires one attribute record per shape, in the same
    order, so a count mismatch means the download is inconsistent.
    """

    path = Path(shp_path)
    _, attributes = read_dbf_table(path.with_suffix(".dbf"), columns=columns)
    shapes = PolygonShapefile(path)
    if len(shapes) != len(attributes):
        shapes.close()
        raise SchemaChangeError(
            f"{path.name} holds {len(shapes)} shapes but its .dbf holds "
            f"{len(attributes)} attribute records"
        )
    return shapes, attributes
