"""Read-only dashboard over the project's own command-line scripts.

Every rate the dashboard shows is produced by running the same commands a user
would type in a terminal, as a child process with no shell. The browser receives
the exact argument vector, exit status, stdout, and stderr, so the interface
stays an operator's view of the pipeline rather than a second implementation of
it.

The dashboard is deliberately view-only. It can query a rate, estimate a trip,
and validate the existing database, all of which only read. It cannot refresh
rates or rebuild map layers: those replace published data and remain manual
terminal operations, so no HTTP request can alter what the database holds.

Command arguments are never taken from the request as text. Each action has a
builder that re-validates its inputs through the package's own parsers and emits
a fixed argument vector, so a request cannot introduce a new argument, a new
file path, or a shell metacharacter.

The server binds to the loopback interface by default. Binding anywhere else
exposes it to the network, so a listener on any other interface requires an HTTP
Basic password on every route, including the static assets. Requests arriving
over loopback stay unauthenticated: the password exists to stop strangers on the
network, not the person sitting at the machine. See ``auth`` for the credential
store, and note that HTTP Basic over plain HTTP sends that password in a form
anyone watching the wire can read.
"""

from __future__ import annotations

import gzip
import json
import logging
import mimetypes
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from calendar import monthrange
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .auth import LOCKOUT_SECONDS, REALM, Decision, PasswordGate
from .config import PACKAGE_ROOT, Settings
from .exceptions import DataValidationError, PerDiemError, RateNotFoundError
from .geo_lookup import ZctaGeometryIndex
from .utils import date_to_fiscal_year, normalize_zip, parse_date


LOGGER = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).resolve().parent / "static"
VENDOR_ROOT = STATIC_ROOT / "vendor"
STATE_PATTERN = re.compile(r"^[A-Z]{2}$")
MAX_REQUEST_BYTES = 64 * 1024
MAX_STREAM_CHARS = 2 * 1024 * 1024
MAX_JOBS = 200
GZIP_CACHE_ENTRIES = 8
DEFAULT_TIMEOUT = 300
MAX_DECIMAL_INPUT = Decimal("1e12")
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "*"})


@dataclass
class CommandJob:
    """One invocation of a project script."""

    id: str
    action: str
    argv: list[str]
    display: str
    status: str = "running"
    returncode: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    duration_ms: int | None = None
    parsed: Any | None = None
    error: str | None = None
    _stdout: list[str] = field(default_factory=list)
    _stderr: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, stream: str, text: str) -> None:
        with self._lock:
            buffer = self._stdout if stream == "stdout" else self._stderr
            current = sum(len(chunk) for chunk in buffer)
            if current >= MAX_STREAM_CHARS:
                return
            buffer.append(text[: MAX_STREAM_CHARS - current])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stdout = "".join(self._stdout)
            stderr = "".join(self._stderr)
        return {
            "id": self.id,
            "action": self.action,
            "command": self.display,
            "argv": self.argv,
            "status": self.status,
            "returncode": self.returncode,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "durationMs": self.duration_ms,
            "stdout": stdout,
            "stderr": stderr,
            "parsed": self.parsed,
            "error": self.error,
        }


class JobRegistry:
    """Bounded store of recent command jobs."""

    def __init__(self, limit: int = MAX_JOBS) -> None:
        self._jobs: OrderedDict[str, CommandJob] = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, job: CommandJob) -> None:
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._limit:
                self._jobs.popitem(last=False)

    def get(self, job_id: str) -> CommandJob | None:
        with self._lock:
            return self._jobs.get(job_id)


def _script(name: str) -> Path:
    return PACKAGE_ROOT / "scripts" / name


def _display(argv: list[str]) -> str:
    """Render an argument vector as a copyable, project-relative command line."""

    parts = ["python"]
    for argument in argv[1:]:
        candidate = Path(argument)
        if candidate.is_absolute():
            try:
                argument = str(candidate.relative_to(PACKAGE_ROOT))
            except ValueError:
                pass
        parts.append(shlex.quote(argument))
    return " ".join(parts)


def _decimal(value: Any, label: str) -> str:
    """Validate a non-negative decimal and render it in plain notation.

    The magnitude bound keeps a value such as ``1e400`` -- a legitimate Decimal
    -- from expanding into a several-hundred-digit command-line argument.
    """

    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be a non-negative number")
    if parsed > MAX_DECIMAL_INPUT:
        raise ValueError(f"{label} is implausibly large")
    return format(parsed, "f")


def build_query(payload: dict[str, Any]) -> list[str]:
    """Build the ZIP/date rate query command."""

    argv = [
        sys.executable,
        str(_script("query_rate.py")),
        "--zip",
        normalize_zip(payload.get("zip", "")),
        "--date",
        parse_date(payload.get("date", "")).isoformat(),
        "--json",
    ]
    if payload.get("explain"):
        argv.append("--explain")
    return argv


def build_estimate(payload: dict[str, Any]) -> list[str]:
    """Build the trip-estimate command."""

    start = parse_date(payload.get("startDate", ""))
    end = parse_date(payload.get("endDate", ""))
    if end < start:
        raise ValueError("The end date cannot precede the start date")
    travelers = payload.get("travelers", 1)
    if isinstance(travelers, bool) or not isinstance(travelers, int) or travelers < 1:
        raise ValueError("travelers must be a positive integer")
    argv = [
        sys.executable,
        str(_script("estimate_trip.py")),
        "--zip",
        normalize_zip(payload.get("zip", "")),
        "--start-date",
        start.isoformat(),
        "--end-date",
        end.isoformat(),
        "--travelers",
        str(travelers),
        "--json",
    ]
    mileage = payload.get("mileage")
    mileage_rate = payload.get("mileageRate")
    has_mileage = mileage not in (None, "")
    has_rate = mileage_rate not in (None, "")
    if has_mileage != has_rate:
        raise ValueError("Mileage and mileage rate must be supplied together")
    if has_mileage and has_rate:
        argv += [
            "--mileage",
            _decimal(mileage, "mileage"),
            "--mileage-rate",
            _decimal(mileage_rate, "mileage rate"),
        ]
    return argv


def build_validate(payload: dict[str, Any]) -> list[str]:
    """Build the read-only database validation command."""

    return [sys.executable, str(_script("validate_database.py"))]


# Only read-only actions are reachable over HTTP. Refreshing rates and
# rebuilding map layers replace published data, so they are deliberately absent
# here and are run from a terminal instead. Adding an entry to this mapping is
# what makes an action reachable, so the omission is the enforcement.
COMMAND_BUILDERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "query": build_query,
    "estimate": build_estimate,
    "validate": build_validate,
}

MANUAL_COMMANDS = (
    {
        "label": "Refresh a fiscal year",
        "command": "python scripts/refresh_rates.py --fiscal-year 2026",
    },
    {
        "label": "Validate without replacing",
        "command": "python scripts/refresh_rates.py --fiscal-year 2026 --validate-only",
    },
    {
        "label": "Rebuild map layers",
        "command": "python scripts/build_map_data.py",
    },
)


def _pump(stream: Any, job: CommandJob, name: str) -> None:
    try:
        for line in iter(stream.readline, ""):
            job.append(name, line)
    finally:
        stream.close()


def run_job(job: CommandJob, timeout: int) -> None:
    """Execute *job* and record its transcript."""

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            job.argv,
            cwd=str(PACKAGE_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            shell=False,
        )
    except OSError as exc:
        job.status = "failed"
        job.error = f"Could not start the command: {exc}"
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return

    readers = [
        threading.Thread(target=_pump, args=(process.stdout, job, "stdout"), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, job, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        job.returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        job.returncode = process.wait()
        job.error = f"The command exceeded its {timeout}s timeout and was stopped."
    for reader in readers:
        reader.join(timeout=5)

    job.duration_ms = int((time.monotonic() - started) * 1000)
    job.finished_at = datetime.now(timezone.utc).isoformat()
    job.status = "completed" if job.returncode == 0 else "failed"
    if job.returncode == 0 and "--json" in job.argv:
        stdout = job.snapshot()["stdout"]
        try:
            job.parsed = json.loads(stdout)
        except json.JSONDecodeError:
            job.error = "The command succeeded but its JSON output could not be parsed."
    elif job.returncode == 0 and job.action == "validate":
        stdout = job.snapshot()["stdout"]
        try:
            job.parsed = json.loads(stdout)
        except json.JSONDecodeError:
            pass


def database_context(settings: Settings) -> dict[str, Any]:
    """Summarize the production database for the dashboard header."""

    path = settings.database_path
    context: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return context
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            context["zipCount"] = connection.execute(
                "SELECT COUNT(DISTINCT zip_code) FROM locations"
            ).fetchone()[0]
            context["rateCount"] = connection.execute(
                "SELECT COUNT(*) FROM rates"
            ).fetchone()[0]
            context["fiscalYears"] = [
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT fiscal_year FROM rates ORDER BY fiscal_year"
                )
            ]
            coverage = connection.execute(
                "SELECT MIN(effective_start), MAX(effective_end) FROM rates"
            ).fetchone()
            context["coverageStart"] = coverage[0]
            context["coverageEnd"] = coverage[1]
            latest = connection.execute(
                "SELECT fiscal_year, completed_at, status FROM refresh_history "
                "ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            if latest is not None:
                context["lastRefresh"] = {
                    "fiscalYear": latest["fiscal_year"],
                    "completedAt": latest["completed_at"],
                    "status": latest["status"],
                }
    except sqlite3.Error as exc:
        context["error"] = str(exc)
    return context


HEATMAP_FIELDS = ("lodgingRate", "mieRate", "firstLastDayMie")
HEATMAP_GRID_SIZE = 6


def _one_year_after(value: date) -> date:
    """Return the same calendar date next year, clamping leap day to Feb. 28."""

    day = min(value.day, monthrange(value.year + 1, value.month)[1])
    return date(value.year + 1, value.month, day)


def _equivalent_date(travel_date: date, year_shift: int) -> date:
    """Move a planning date back by whole fiscal years without losing its season."""

    target_year = travel_date.year - year_shift
    day = min(travel_date.day, monthrange(target_year, travel_date.month)[1])
    return date(target_year, travel_date.month, day)


def _heatmap_range(items: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """Return one display range for each heat-map metric in *items*."""

    ranges: dict[str, dict[str, float | None]] = {}
    for field in HEATMAP_FIELDS:
        values = [float(item[field]) for item in items if item.get(field) is not None]
        ranges[field] = {
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    return ranges


def _heatmap_cells(
    mapped_rows: list[tuple[sqlite3.Row, str]],
    geo: ZctaGeometryIndex,
) -> list[dict[str, Any]]:
    """Aggregate ZIP rates into a hotspot-preserving national-view grid."""

    positioned: dict[str, list[tuple[sqlite3.Row, float, float]]] = {}
    for row, state in mapped_rows:
        center = geo.center_for_zip(row["zip_code"])
        if center is None:
            continue
        latitude, longitude = center
        positioned.setdefault(state, []).append((row, latitude, longitude))

    cells: list[dict[str, Any]] = []
    for state, state_rows in sorted(positioned.items()):
        latitudes = [latitude for _, latitude, _ in state_rows]
        longitudes = [longitude for _, _, longitude in state_rows]
        min_latitude, max_latitude = min(latitudes), max(latitudes)
        min_longitude, max_longitude = min(longitudes), max(longitudes)
        latitude_span = max_latitude - min_latitude
        longitude_span = max_longitude - min_longitude

        grouped: dict[
            tuple[int, int], list[tuple[sqlite3.Row, float, float]]
        ] = {}
        for row, latitude, longitude in state_rows:
            latitude_cell = (
                HEATMAP_GRID_SIZE // 2
                if latitude_span == 0
                else min(
                    HEATMAP_GRID_SIZE - 1,
                    int(
                        (latitude - min_latitude)
                        / latitude_span
                        * HEATMAP_GRID_SIZE
                    ),
                )
            )
            longitude_cell = (
                HEATMAP_GRID_SIZE // 2
                if longitude_span == 0
                else min(
                    HEATMAP_GRID_SIZE - 1,
                    int(
                        (longitude - min_longitude)
                        / longitude_span
                        * HEATMAP_GRID_SIZE
                    ),
                )
            )
            grouped.setdefault((latitude_cell, longitude_cell), []).append(
                (row, latitude, longitude)
            )

        for (latitude_cell, longitude_cell), cell_rows in sorted(grouped.items()):
            rated_rows = [
                row for row, _, _ in cell_rows if int(row["candidate_count"]) == 1
            ]
            cell: dict[str, Any] = {
                "id": f"{state}-{latitude_cell}-{longitude_cell}",
                "state": state,
                "latitude": round(
                    sum(latitude for _, latitude, _ in cell_rows) / len(cell_rows), 5
                ),
                "longitude": round(
                    sum(longitude for _, _, longitude in cell_rows) / len(cell_rows), 5
                ),
                "ratedZipCount": len(rated_rows),
                "ambiguousZipCount": len(cell_rows) - len(rated_rows),
            }
            for field, column in (
                ("lodgingRate", "lodging_rate"),
                ("mieRate", "mie_rate"),
                ("firstLastDayMie", "first_last_day_mie"),
            ):
                values = [float(row[column]) for row in rated_rows]
                # The country view must keep a small expensive locality visible
                # even when most ZIPs in its coarse cell use the standard rate.
                # State drill-down still exposes every exact ZIP value.
                cell[field] = max(values) if values else None
            cells.append(cell)
    return cells


def heatmap_data(
    settings: Settings,
    travel_date: date,
    geo: ZctaGeometryIndex,
    *,
    state: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Build read-only heat-map values for one date.

    ZIPs with more than one applicable locality are kept out of the numeric
    gradient. This mirrors :func:`lookup.get_per_diem`, which refuses to guess
    between multiple official rates for the same ZIP and date.
    """

    database = settings.database_path
    if not database.is_file():
        raise FileNotFoundError(
            f"Rate database not found at {database}; run the refresh command first"
        )

    query = """
        SELECT l.zip_code, COUNT(*) AS candidate_count,
               CASE WHEN COUNT(*) = 1 THEN MIN(l.locality) END AS locality,
               CASE WHEN COUNT(*) = 1 THEN MIN(l.is_standard) END AS is_standard,
               CASE WHEN COUNT(*) = 1
                    THEN MIN(CAST(r.lodging_rate AS REAL)) END AS lodging_rate,
               CASE WHEN COUNT(*) = 1
                    THEN MIN(CAST(r.mie_rate AS REAL)) END AS mie_rate,
               CASE WHEN COUNT(*) = 1
                    THEN MIN(CAST(r.first_last_day_mie AS REAL)) END AS first_last_day_mie
        FROM rates r
        JOIN locations l ON l.id = r.location_id
        WHERE r.fiscal_year = ?
          AND r.effective_start <= ? AND r.effective_end >= ?
        GROUP BY l.zip_code
        ORDER BY l.zip_code
    """
    fiscal_year = date_to_fiscal_year(travel_date)
    rate_date = travel_date
    rate_fiscal_year = fiscal_year
    rate_status = "official"
    current_date = today or date.today()
    planning_end = _one_year_after(current_date)

    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        coverage = connection.execute(
            """SELECT MIN(effective_start) AS coverage_start,
                      MAX(effective_end) AS coverage_end,
                      MAX(fiscal_year) AS latest_fiscal_year
               FROM rates"""
        ).fetchone()
        if coverage is None or coverage["latest_fiscal_year"] is None:
            raise RateNotFoundError(f"Rate database at {database} contains no rates")

        latest_fiscal_year = int(coverage["latest_fiscal_year"])
        rows = connection.execute(
            query,
            (fiscal_year, travel_date.isoformat(), travel_date.isoformat()),
        ).fetchall()

        # Official future-year files are commonly unavailable before the fiscal
        # year starts. Keep the planning map useful for the next twelve months by
        # reusing the latest loaded fiscal year's equivalent seasonal date. This
        # never writes projected rows to SQLite, and the response identifies the
        # exact official date and fiscal year supplying every displayed value.
        if (
            not rows
            and current_date <= travel_date <= planning_end
            and fiscal_year > latest_fiscal_year
        ):
            rate_fiscal_year = latest_fiscal_year
            rate_date = _equivalent_date(
                travel_date, fiscal_year - latest_fiscal_year
            )
            rows = connection.execute(
                query,
                (
                    rate_fiscal_year,
                    rate_date.isoformat(),
                    rate_date.isoformat(),
                ),
            ).fetchall()
            if rows:
                rate_status = "planning-estimate"

    if not rows:
        coverage_start = coverage["coverage_start"] or "unknown"
        coverage_end = coverage["coverage_end"] or "unknown"
        raise RateNotFoundError(
            f"No rate data for {travel_date}. Official rates are loaded from "
            f"{coverage_start} through {coverage_end}; planning estimates are "
            f"available from {current_date} through {planning_end}."
        )

    # Postal ZIP records can cross state lines, while every Census ZCTA polygon
    # is assigned to one display state when the map layer is generated. Group by
    # ZIP first (the same ambiguity rule as the canonical lookup), then attach the
    # value to that generated polygon. Postal ZIPs without a drawable ZCTA do not
    # contribute to map coverage or state medians.
    mapped_rows: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        mapped_state = geo.state_for_zip(row["zip_code"])
        if mapped_state is not None:
            mapped_rows.append((row, mapped_state))

    base: dict[str, Any] = {
        "travelDate": travel_date.isoformat(),
        "fiscalYear": fiscal_year,
        "rateDate": rate_date.isoformat(),
        "rateFiscalYear": rate_fiscal_year,
        "rateStatus": rate_status,
        "officialCoverageEnd": coverage["coverage_end"],
        "scope": state or "nation",
    }
    if state is not None:
        rates: list[dict[str, Any]] = []
        for row, mapped_state in mapped_rows:
            if mapped_state != state:
                continue
            ambiguous = int(row["candidate_count"]) != 1
            rates.append(
                {
                    "zip": row["zip_code"],
                    "status": "ambiguous" if ambiguous else "rated",
                    "candidateCount": int(row["candidate_count"]),
                    "locality": row["locality"],
                    "isStandard": bool(row["is_standard"]) if not ambiguous else None,
                    "lodgingRate": row["lodging_rate"],
                    "mieRate": row["mie_rate"],
                    "firstLastDayMie": row["first_last_day_mie"],
                }
            )
        rated = [item for item in rates if item["status"] == "rated"]
        return {
            **base,
            "state": state,
            "rates": rates,
            "ranges": _heatmap_range(rated),
            "ratedZipCount": len(rated),
            "ambiguousZipCount": len(rates) - len(rated),
        }

    by_state: dict[str, list[sqlite3.Row]] = {}
    for row, mapped_state in mapped_rows:
        by_state.setdefault(mapped_state, []).append(row)

    summaries: list[dict[str, Any]] = []
    for code, state_rows in sorted(by_state.items()):
        rated_rows = [row for row in state_rows if int(row["candidate_count"]) == 1]
        summary: dict[str, Any] = {
            "state": code,
            "ratedZipCount": len(rated_rows),
            "ambiguousZipCount": len(state_rows) - len(rated_rows),
        }
        for field, column in (
            ("lodgingRate", "lodging_rate"),
            ("mieRate", "mie_rate"),
            ("firstLastDayMie", "first_last_day_mie"),
        ):
            values = [float(row[column]) for row in rated_rows]
            summary[field] = median(values) if values else None
        summaries.append(summary)
    cells = _heatmap_cells(mapped_rows, geo)
    return {
        **base,
        "states": summaries,
        "cells": cells,
        "ranges": _heatmap_range(cells),
        "ratedZipCount": sum(item["ratedZipCount"] for item in summaries),
        "ambiguousZipCount": sum(item["ambiguousZipCount"] for item in summaries),
    }


class DashboardServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the dashboard's shared state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        settings: Settings,
        gate: PasswordGate | None = None,
    ) -> None:
        super().__init__(address, DashboardHandler)
        self.settings = settings
        self.gate = gate
        self.jobs = JobRegistry()
        self._geo: ZctaGeometryIndex | None = None
        self._geo_error: str | None = None
        self._geo_lock = threading.Lock()
        self._gzip_cache: OrderedDict[str, bytes] = OrderedDict()
        self._gzip_lock = threading.Lock()

    @property
    def geo(self) -> ZctaGeometryIndex | None:
        """Return the geometry index, loading it once on first use."""

        with self._geo_lock:
            if self._geo is None and self._geo_error is None:
                try:
                    self._geo = ZctaGeometryIndex(settings=self.settings)
                except (DataValidationError, OSError, ValueError) as exc:
                    self._geo_error = str(exc)
                    LOGGER.warning("Map layers unavailable: %s", exc)
            return self._geo

    @property
    def geo_error(self) -> str | None:
        self.geo
        return self._geo_error

    def compressed(self, path: Path) -> bytes:
        """Return gzip-compressed file bytes, caching recent map layers."""

        key = str(path)
        with self._gzip_lock:
            cached = self._gzip_cache.get(key)
            if cached is not None:
                self._gzip_cache.move_to_end(key)
                return cached
        payload = gzip.compress(path.read_bytes(), 6)
        with self._gzip_lock:
            self._gzip_cache[key] = payload
            while len(self._gzip_cache) > GZIP_CACHE_ENTRIES:
                self._gzip_cache.popitem(last=False)
        return payload


class DashboardHandler(BaseHTTPRequestHandler):
    """Request handler for the static dashboard and its JSON API."""

    server_version = "FederalPerDiemDashboard/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), format % args)

    # -- response helpers -------------------------------------------------

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        encoding: str | None = None,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        self._send(
            status,
            json.dumps(payload, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BYTES:
            raise ValueError("The request body is too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("The request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("The request body must be a JSON object")
        return payload

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    # -- authentication ---------------------------------------------------

    @property
    def gate(self) -> PasswordGate | None:
        return getattr(self.server, "gate", None)

    def _authorized(self) -> bool:
        """Return whether this request may proceed, answering it if not.

        Called before any route runs, so an unauthenticated caller reaches
        neither the API, the map layers, nor the static assets.
        """

        gate = self.gate
        if gate is None:
            return True
        client = self.client_address[0] if self.client_address else ""
        decision = gate.check(self.headers.get("Authorization"), client)
        if decision in {Decision.EXEMPT, Decision.AUTHENTICATED}:
            return True
        if decision is Decision.LOCKED_OUT:
            self._deny(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Too many wrong passwords. Wait a minute and try again.",
                retry_after=int(LOCKOUT_SECONDS),
            )
            return False
        if decision is Decision.REJECTED:
            LOGGER.warning("Rejected a wrong dashboard password from %s", client)
        self._deny(
            HTTPStatus.UNAUTHORIZED,
            "This dashboard is password protected.",
            challenge=True,
        )
        return False

    def _deny(
        self,
        status: HTTPStatus,
        message: str,
        *,
        challenge: bool = False,
        retry_after: int | None = None,
    ) -> None:
        """Answer an unauthorized request and close the connection."""

        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        if challenge:
            self.send_header(
                "WWW-Authenticate", f'Basic realm="{REALM}", charset="UTF-8"'
            )
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # A rejected POST usually still has its body waiting on the socket, and
        # this handler never reads it. Closing the connection keeps those bytes
        # from being parsed as the next request on a keep-alive connection.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- routing ----------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        path = request.path.rstrip("/") or "/"
        try:
            if not self._authorized():
                return
            if path == "/":
                self._serve_static("index.html")
            elif path == "/heatmap":
                self._serve_static("heatmap.html")
            elif path == "/api/context":
                self._api_context()
            elif path == "/api/heatmap":
                self._api_heatmap(request.query)
            elif path == "/api/geo/states":
                self._api_state_layer()
            elif path.startswith("/api/geo/zcta/"):
                self._api_zcta_layer(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/zip/"):
                self._api_zip(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/job/"):
                self._api_job(path.rsplit("/", 1)[-1])
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/") :])
            else:
                self._error(HTTPStatus.NOT_FOUND, f"No route for {path}")
        except BrokenPipeError:
            LOGGER.debug("Client disconnected during %s", path)
        except Exception as exc:  # noqa: BLE001 - surface faults to the browser
            LOGGER.exception("Unhandled error for %s", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            # Checked before the body is read, so an unauthenticated caller
            # cannot make the server parse anything it sends.
            if not self._authorized():
                return
            payload = self._body()
            if path == "/api/locate":
                self._api_locate(payload)
            elif path == "/api/run":
                self._api_run(payload)
            else:
                self._error(HTTPStatus.NOT_FOUND, f"No route for {path}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except BrokenPipeError:
            LOGGER.debug("Client disconnected during %s", path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Unhandled error for %s", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    # -- endpoints --------------------------------------------------------

    def _serve_static(self, relative: str) -> None:
        candidate = (STATIC_ROOT / relative).resolve()
        if not candidate.is_file() or STATIC_ROOT not in candidate.parents:
            self._error(HTTPStatus.NOT_FOUND, f"No asset named {relative}")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        # Only the pinned Leaflet release is immutable. The dashboard's own
        # markup, styles, and script must revalidate, or an edit to them keeps
        # serving a stale copy from the browser cache after a restart.
        vendored = VENDOR_ROOT in candidate.parents
        self._send(
            HTTPStatus.OK,
            candidate.read_bytes(),
            content_type,
            cache="max-age=86400" if vendored else "no-cache",
        )

    def _api_context(self) -> None:
        server: DashboardServer = self.server  # type: ignore[assignment]
        geo = server.geo
        today = date.today()
        payload: dict[str, Any] = {
            "database": database_context(self.settings),
            "today": today.isoformat(),
            "travelWindow": {
                "start": today.isoformat(),
                "end": _one_year_after(today).isoformat(),
            },
            "projectRoot": str(PACKAGE_ROOT),
            "python": sys.executable,
            "readOnly": True,
            "actions": sorted(COMMAND_BUILDERS),
            "manualCommands": list(MANUAL_COMMANDS),
            "map": {"available": geo is not None, "error": server.geo_error},
        }
        if geo is not None:
            manifest = geo.manifest
            payload["map"].update(
                {
                    "states": geo.state_summaries(),
                    "generatedAt": manifest.get("generated_at"),
                    "zctaCount": manifest.get("zcta_count"),
                    "unmappedZctaCount": manifest.get("unmapped_zcta_count"),
                    "simplifyToleranceDegrees": manifest.get(
                        "simplify_tolerance_degrees"
                    ),
                    "coordinateReferenceSystem": manifest.get(
                        "coordinate_reference_system"
                    ),
                    "sources": manifest.get("sources", []),
                    "exactHitTesting": geo.shapefile_available,
                }
            )
        self._json(HTTPStatus.OK, payload)

    def _api_heatmap(self, query: str) -> None:
        parameters = parse_qs(query, keep_blank_values=True)
        date_values = parameters.get("date", [])
        if len(date_values) != 1:
            self._error(HTTPStatus.BAD_REQUEST, "date is required exactly once")
            return
        try:
            travel_date = parse_date(date_values[0])
        except (PerDiemError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        state: str | None = None
        state_values = parameters.get("state", [])
        if state_values:
            if len(state_values) != 1:
                self._error(HTTPStatus.BAD_REQUEST, "state may be supplied only once")
                return
            state = state_values[0].upper()
            if not STATE_PATTERN.fullmatch(state):
                self._error(HTTPStatus.BAD_REQUEST, f"Invalid state code {state_values[0]!r}")
                return
        server: DashboardServer = self.server  # type: ignore[assignment]
        geo = server.geo
        if geo is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, server.geo_error or "No map data")
            return
        try:
            payload = heatmap_data(self.settings, travel_date, geo, state=state)
        except FileNotFoundError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            return
        except RateNotFoundError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
            return
        self._json(HTTPStatus.OK, payload)

    def _serve_geojson(self, path: Path) -> None:
        server: DashboardServer = self.server  # type: ignore[assignment]
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, f"{path.name} has not been generated")
            return
        if self._accepts_gzip():
            self._send(
                HTTPStatus.OK,
                server.compressed(path),
                "application/geo+json; charset=utf-8",
                encoding="gzip",
                cache="max-age=3600",
            )
        else:
            self._send(
                HTTPStatus.OK,
                path.read_bytes(),
                "application/geo+json; charset=utf-8",
                cache="max-age=3600",
            )

    def _api_state_layer(self) -> None:
        self._serve_geojson(self.settings.geo_dir / "states.geojson")

    def _api_zcta_layer(self, state: str) -> None:
        code = state.upper().removesuffix(".GEOJSON")
        if not STATE_PATTERN.match(code):
            self._error(HTTPStatus.BAD_REQUEST, f"Invalid state code {state!r}")
            return
        self._serve_geojson(self.settings.geo_dir / "zcta" / f"{code}.geojson")

    def _api_zip(self, raw: str) -> None:
        server: DashboardServer = self.server  # type: ignore[assignment]
        try:
            zip_code = normalize_zip(raw)
        except PerDiemError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        geo = server.geo
        if geo is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, server.geo_error or "No map data")
            return
        entry = geo.zip_entry(zip_code)
        if entry is None:
            self._json(
                HTTPStatus.OK,
                {
                    "zip": zip_code,
                    "found": False,
                    "message": (
                        f"ZIP {zip_code} has no Census ZIP Code Tabulation Area, so it "
                        "cannot be drawn. It may still have a published rate."
                    ),
                },
            )
            return
        self._json(HTTPStatus.OK, {"found": True, **entry})

    def _api_locate(self, payload: dict[str, Any]) -> None:
        server: DashboardServer = self.server  # type: ignore[assignment]
        geo = server.geo
        if geo is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, server.geo_error or "No map data")
            return
        try:
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("latitude and longitude are required numbers") from exc
        resolution = geo.resolve(latitude, longitude)
        if resolution is None:
            self._json(
                HTTPStatus.OK,
                {"found": False, "message": "No ZIP Code Tabulation Area is near that point"},
            )
            return
        self._json(HTTPStatus.OK, {"found": True, **resolution.to_dict()})

    def _api_run(self, payload: dict[str, Any]) -> None:
        server: DashboardServer = self.server  # type: ignore[assignment]
        action = str(payload.get("action", ""))
        builder = COMMAND_BUILDERS.get(action)
        if builder is None:
            raise ValueError(
                f"Unknown action {action!r}; expected one of "
                f"{', '.join(sorted(COMMAND_BUILDERS))}"
            )
        try:
            argv = builder(payload)
        except PerDiemError as exc:
            raise ValueError(str(exc)) from exc

        job = CommandJob(
            id=uuid.uuid4().hex,
            action=action,
            argv=argv,
            display=_display(argv),
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        server.jobs.add(job)
        threading.Thread(
            target=run_job,
            args=(job, DEFAULT_TIMEOUT),
            daemon=True,
            name=f"job-{action}",
        ).start()
        self._json(HTTPStatus.ACCEPTED, job.snapshot())

    def _api_job(self, job_id: str) -> None:
        server: DashboardServer = self.server  # type: ignore[assignment]
        job = server.jobs.get(job_id)
        if job is None:
            self._error(HTTPStatus.NOT_FOUND, f"No job named {job_id}")
            return
        self._json(HTTPStatus.OK, job.snapshot())


def lan_address() -> str | None:
    """Return this host's primary outbound IPv4 address, or None if it has none.

    The probe socket is a datagram socket that is only connected, so the kernel
    picks the interface it would route from without any packet being sent.
    """

    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # reserved documentation address
        except OSError:
            return None
        return probe.getsockname()[0]


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    settings: Settings | None = None,
    open_browser: bool = False,
    gate: PasswordGate | None = None,
) -> None:
    """Run the dashboard until interrupted."""

    settings = settings or Settings.from_env()
    server = DashboardServer((host, port), settings, gate)
    bound_port = server.server_address[1]
    on_every_interface = host in WILDCARD_HOSTS
    address = f"http://{'127.0.0.1' if on_every_interface else host}:{bound_port}/"
    LOGGER.info("Federal per diem dashboard listening on %s:%s", host, bound_port)
    print(f"Dashboard:  {address}")
    if on_every_interface:
        lan = lan_address()
        reachable = f"http://{lan}:{bound_port}/" if lan else "(no network interface)"
        print(f"Network:    {reachable}")
        if gate is None:
            print("            Reachable by anyone on that network, without a password.")
            print("            The dashboard is read-only, but requests do use this CPU.")
        else:
            print("            Password required. Any user name works.")
            print("            HTTP sends that password unencrypted, so treat it")
            print("            as protection from strangers, not from snooping.")
    print(f"Database:   {settings.database_path}")
    print(f"Map layers: {settings.geo_dir}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        import webbrowser

        threading.Timer(0.5, webbrowser.open, args=(address,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.shutdown()
        server.server_close()
        if server._geo is not None:
            server._geo.close()
