"""Safe, cached downloads from authoritative government sources."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

from .config import Settings
from .exceptions import SourceDownloadError
from .models import SourceMetadata, SourceSpec
from .utils import sha256_file


LOGGER = logging.getLogger(__name__)


def fiscal_year_source_specs(fiscal_year: int, settings: Settings) -> list[SourceSpec]:
    """Return all source files needed to construct one federal fiscal year."""

    if not 2000 <= fiscal_year <= 2200:
        raise ValueError(f"Invalid fiscal year: {fiscal_year}")
    common_xlsx_types = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    )
    archive_types = (
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    )
    specs = [
        SourceSpec(
            key="gsa_zip",
            agency="GSA",
            dataset_name="Per Diem ZIP Code File for Developers",
            fiscal_year=fiscal_year,
            url=settings.gsa_zip_url_template.format(fiscal_year=fiscal_year),
            filename=f"FY{fiscal_year}_ZipCodeFile.xlsx",
            expected_extensions=(".xlsx",),
            expected_content_types=common_xlsx_types,
        ),
        SourceSpec(
            key="gsa_rates",
            agency="GSA",
            dataset_name="Per Diem Master Rates File",
            fiscal_year=fiscal_year,
            url=settings.gsa_rates_url_template.format(fiscal_year=fiscal_year),
            filename=f"FY{fiscal_year}_PerDiemMasterRatesFile.xlsx",
            expected_extensions=(".xlsx",),
            expected_content_types=common_xlsx_types,
        ),
    ]
    for calendar_year in (fiscal_year - 1, fiscal_year):
        specs.append(
            SourceSpec(
                key=f"dod_oconus_{calendar_year}",
                agency="DoD/DTMO",
                dataset_name="OCONUS Per Diem ASCII Archive",
                fiscal_year=fiscal_year,
                url=settings.dod_ascii_url_template.format(
                    calendar_year=calendar_year
                ),
                filename=f"OCONUS-ASCII-{calendar_year}.zip",
                expected_extensions=(".zip",),
                expected_content_types=archive_types,
            )
        )
    census_specs = (
        ("census_place", "ZCTA-to-Place Relationship File", settings.census_place_url),
        ("census_county", "ZCTA-to-County Relationship File", settings.census_county_url),
        (
            "census_cousub",
            "ZCTA-to-County Subdivision Relationship File",
            settings.census_cousub_url,
        ),
    )
    for key, name, url in census_specs:
        specs.append(
            SourceSpec(
                key=key,
                agency="U.S. Census Bureau",
                dataset_name=f"2020 Census {name}",
                fiscal_year=fiscal_year,
                url=url,
                filename=Path(url).name,
                expected_extensions=(".txt",),
                expected_content_types=("text/plain", "application/octet-stream"),
            )
        )
    return specs


def boundary_source_specs(settings: Settings) -> list[SourceSpec]:
    """Return the Census cartographic boundary archives used by the map layers.

    Cartographic boundary files are a 2020 geographic vintage, not a fiscal-year
    publication, so they are tracked separately from the rate sources and carry
    fiscal year 0.
    """

    archive_types = (
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    )
    specs = []
    for key, name, url in (
        (
            "census_state_boundary",
            "2020 Cartographic Boundary File, States (1:20,000,000)",
            settings.census_state_boundary_url,
        ),
        (
            "census_zcta_boundary",
            "2020 Cartographic Boundary File, ZIP Code Tabulation Areas (1:500,000)",
            settings.census_zcta_boundary_url,
        ),
    ):
        specs.append(
            SourceSpec(
                key=key,
                agency="U.S. Census Bureau",
                dataset_name=name,
                fiscal_year=0,
                url=url,
                filename=Path(url).name,
                expected_extensions=(".zip",),
                expected_content_types=archive_types,
            )
        )
    return specs


def _validate_payload(path: Path, spec: SourceSpec, content_type: str | None, minimum: int) -> None:
    if path.stat().st_size < minimum:
        raise SourceDownloadError(
            f"{spec.dataset_name} is implausibly small ({path.stat().st_size} bytes)"
        )
    logical_name = path.name.removesuffix(".part")
    logical_suffix = Path(logical_name).suffix.lower()
    if logical_suffix not in spec.expected_extensions:
        raise SourceDownloadError(f"Unexpected extension for {path.name}")
    if content_type and spec.expected_content_types:
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized not in spec.expected_content_types:
            LOGGER.warning(
                "Unexpected Content-Type %s for %s; checking file signature",
                normalized,
                spec.dataset_name,
            )
    with path.open("rb") as stream:
        signature = stream.read(4)
    if logical_suffix in {".xlsx", ".zip"} and not signature.startswith(b"PK"):
        guessed = mimetypes.guess_type(logical_name)[0] or "binary file"
        raise SourceDownloadError(
            f"{spec.dataset_name} is not a valid ZIP-based {guessed} payload"
        )


def _metadata_for_existing(path: Path, spec: SourceSpec) -> SourceMetadata:
    timestamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return SourceMetadata(
        agency=spec.agency,
        dataset_name=spec.dataset_name,
        fiscal_year=spec.fiscal_year,
        source_url=spec.url,
        downloaded_at=timestamp,
        filename=path.name,
        sha256=sha256_file(path),
        file_size=path.stat().st_size,
        local_path=path,
    )


def download_file(
    spec: SourceSpec,
    destination_dir: Path,
    settings: Settings,
    *,
    force: bool = False,
    session: requests.Session | None = None,
) -> SourceMetadata:
    """Download one file atomically or return validated cached metadata."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / spec.filename
    if destination.exists() and not force:
        _validate_payload(destination, spec, None, settings.min_download_bytes)
        LOGGER.info("Using cached %s", destination.name)
        return _metadata_for_existing(destination, spec)

    client = session or requests.Session()
    temporary = destination.with_name(destination.name + ".part")
    if temporary.exists():
        temporary.unlink()
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "application/zip,application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet,text/plain;q=0.9,*/*;q=0.5",
    }
    try:
        with client.get(
            spec.url,
            headers=headers,
            stream=True,
            timeout=settings.request_timeout,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            with temporary.open("xb") as stream:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        stream.write(chunk)
            _validate_payload(
                temporary,
                spec,
                response.headers.get("Content-Type"),
                settings.min_download_bytes,
            )
    except (requests.RequestException, OSError, SourceDownloadError) as exc:
        temporary.unlink(missing_ok=True)
        raise SourceDownloadError(
            f"Could not download {spec.dataset_name} from {spec.url}: {exc}"
        ) from exc

    new_hash = sha256_file(temporary)
    if destination.exists():
        if sha256_file(destination) == new_hash:
            temporary.unlink()
            LOGGER.info("Source unchanged: %s", destination.name)
            return _metadata_for_existing(destination, spec)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        preserved = destination.with_name(
            f"{destination.stem}.previous-{stamp}{destination.suffix}"
        )
        destination.replace(preserved)
        LOGGER.info("Preserved prior raw source as %s", preserved.name)
    temporary.replace(destination)
    LOGGER.info("Downloaded %s (%d bytes)", destination.name, destination.stat().st_size)
    return replace(
        _metadata_for_existing(destination, spec),
        downloaded_at=datetime.now(timezone.utc),
    )


def download_boundaries(
    *,
    force: bool = False,
    settings: Settings | None = None,
) -> dict[str, SourceMetadata]:
    """Download and cache the Census cartographic boundary archives."""

    settings = settings or Settings.from_env()
    downloaded: dict[str, SourceMetadata] = {}
    with requests.Session() as session:
        for spec in boundary_source_specs(settings):
            downloaded[spec.key] = download_file(
                spec,
                settings.geo_raw_dir,
                settings,
                force=force,
                session=session,
            )
    return downloaded


def download_fiscal_year(
    fiscal_year: int,
    *,
    force: bool = False,
    settings: Settings | None = None,
    include_keys: Iterable[str] | None = None,
) -> dict[str, SourceMetadata]:
    """Download and cache every source required for *fiscal_year*."""

    settings = settings or Settings.from_env()
    allowed = set(include_keys) if include_keys is not None else None
    raw_dir = settings.raw_dir / f"FY{fiscal_year}"
    downloaded: dict[str, SourceMetadata] = {}
    with requests.Session() as session:
        for spec in fiscal_year_source_specs(fiscal_year, settings):
            if allowed is not None and spec.key not in allowed:
                continue
            downloaded[spec.key] = download_file(
                spec,
                raw_dir,
                settings,
                force=force,
                session=session,
            )
    return downloaded
