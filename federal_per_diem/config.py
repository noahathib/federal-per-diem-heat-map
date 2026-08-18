"""Central application configuration and authoritative source definitions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings, overridable by environment variables."""

    data_dir: Path = PACKAGE_ROOT / "data"
    request_timeout: int = 60
    min_download_bytes: int = 256
    user_agent: str = "federal-per-diem/1.0 (official-source data pipeline)"
    gsa_zip_url_template: str = (
        "https://www.gsa.gov/system/files/FY{fiscal_year}_ZipCodeFile.xlsx"
    )
    gsa_rates_url_template: str = (
        "https://www.gsa.gov/system/files/FY{fiscal_year}_PerDiemMasterRatesFile.xlsx"
    )
    dod_ascii_url_template: str = (
        "https://www.travel.dod.mil/Portals/119/Documents/Allowances/Per_Diem/"
        "OCONUS/ASCII/OCONUS-ASCII-{calendar_year}.zip"
    )
    census_place_url: str = (
        "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
        "tab20_zcta520_place20_natl.txt"
    )
    census_county_url: str = (
        "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
        "tab20_zcta520_county20_natl.txt"
    )
    census_cousub_url: str = (
        "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
        "tab20_zcta520_cousub20_natl.txt"
    )
    census_state_boundary_url: str = (
        "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_state_20m.zip"
    )
    census_zcta_boundary_url: str = (
        "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip"
    )
    map_simplify_tolerance: float = 0.001
    map_coordinate_decimals: int = 5

    @classmethod
    def from_env(cls, *, data_dir: Path | str | None = None) -> "Settings":
        """Create settings from environment variables and optional data root."""

        defaults = cls()
        return cls(
            data_dir=Path(
                data_dir or os.getenv("FEDERAL_PER_DIEM_DATA_DIR", PACKAGE_ROOT / "data")
            ).expanduser().resolve(),
            request_timeout=int(os.getenv("FEDERAL_PER_DIEM_TIMEOUT", "60")),
            min_download_bytes=int(os.getenv("FEDERAL_PER_DIEM_MIN_BYTES", "256")),
            user_agent=os.getenv(
                "FEDERAL_PER_DIEM_USER_AGENT",
                "federal-per-diem/1.0 (official-source data pipeline)",
            ),
            gsa_zip_url_template=os.getenv(
                "FEDERAL_PER_DIEM_GSA_ZIP_URL",
                defaults.gsa_zip_url_template,
            ),
            gsa_rates_url_template=os.getenv(
                "FEDERAL_PER_DIEM_GSA_RATES_URL",
                defaults.gsa_rates_url_template,
            ),
            dod_ascii_url_template=os.getenv(
                "FEDERAL_PER_DIEM_DOD_ASCII_URL",
                defaults.dod_ascii_url_template,
            ),
            census_place_url=os.getenv(
                "FEDERAL_PER_DIEM_CENSUS_PLACE_URL", defaults.census_place_url
            ),
            census_county_url=os.getenv(
                "FEDERAL_PER_DIEM_CENSUS_COUNTY_URL", defaults.census_county_url
            ),
            census_cousub_url=os.getenv(
                "FEDERAL_PER_DIEM_CENSUS_COUSUB_URL", defaults.census_cousub_url
            ),
            census_state_boundary_url=os.getenv(
                "FEDERAL_PER_DIEM_CENSUS_STATE_BOUNDARY_URL",
                defaults.census_state_boundary_url,
            ),
            census_zcta_boundary_url=os.getenv(
                "FEDERAL_PER_DIEM_CENSUS_ZCTA_BOUNDARY_URL",
                defaults.census_zcta_boundary_url,
            ),
            map_simplify_tolerance=float(
                os.getenv("FEDERAL_PER_DIEM_MAP_TOLERANCE", "0.001")
            ),
            map_coordinate_decimals=int(
                os.getenv("FEDERAL_PER_DIEM_MAP_DECIMALS", "5")
            ),
        )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def database_path(self) -> Path:
        return self.processed_dir / "federal_per_diem.sqlite"

    @property
    def csv_path(self) -> Path:
        return self.processed_dir / "federal_per_diem.csv"

    @property
    def excel_path(self) -> Path:
        return self.processed_dir / "federal_per_diem.xlsx"

    @property
    def geo_raw_dir(self) -> Path:
        """Directory holding the downloaded Census boundary archives."""

        return self.raw_dir / "geo"

    @property
    def geo_dir(self) -> Path:
        """Directory holding the generated dashboard map layers."""

        return self.processed_dir / "geo"


GSA_SOURCE_PAGE = "https://www.gsa.gov/travel/plan-a-trip/per-diem-rates/per-diem-files"
DOD_SOURCE_PAGE = (
    "https://www.travel.dod.mil/Travel-Transportation-Rates/Per-Diem/"
    "Per-Diem-Rate-Lookup/"
)
CENSUS_SOURCE_PAGE = (
    "https://www.census.gov/geographies/reference-files/2020/geo/relationship-files.html"
)
