from dataclasses import replace

import pytest

from federal_per_diem.config import Settings
from federal_per_diem.exceptions import DataValidationError
from federal_per_diem.pipeline import refresh_rates

from conftest import make_rate


def test_failed_refresh_retains_known_good_database(
    tmp_path, source_metadata, monkeypatch
):
    settings = Settings.from_env(data_dir=tmp_path / "data")
    settings.processed_dir.mkdir(parents=True)
    settings.database_path.write_bytes(b"known-good-database")
    metadata = replace(source_metadata, local_path=tmp_path / "placeholder")
    downloads = {
        "gsa_zip": metadata,
        "gsa_rates": replace(metadata, filename="master.xlsx", sha256="b" * 64),
        "dod_oconus_2025": replace(metadata, filename="dod25.zip", sha256="c" * 64),
        "dod_oconus_2026": replace(metadata, filename="dod26.zip", sha256="d" * 64),
        "census_place": replace(metadata, filename="place.txt", sha256="e" * 64),
        "census_county": replace(metadata, filename="county.txt", sha256="f" * 64),
        "census_cousub": replace(metadata, filename="cousub.txt", sha256="0" * 64),
    }
    invalid = make_rate(lodging="0")
    monkeypatch.setattr(
        "federal_per_diem.pipeline.download_fiscal_year", lambda *args, **kwargs: downloads
    )
    monkeypatch.setattr(
        "federal_per_diem.pipeline.parse_gsa_file",
        lambda *args, **kwargs: ([invalid], metadata),
    )
    monkeypatch.setattr("federal_per_diem.pipeline.parse_gsa_master_file", lambda *args: [])
    monkeypatch.setattr(
        "federal_per_diem.pipeline.parse_dod_file", lambda *args: ([], args[1])
    )
    monkeypatch.setattr("federal_per_diem.pipeline.parse_census_crosswalk", lambda *args: {})
    monkeypatch.setattr("federal_per_diem.pipeline.normalize_dod_rates", lambda *args: [])

    with pytest.raises(DataValidationError):
        refresh_rates(2026, settings=settings)
    assert settings.database_path.read_bytes() == b"known-good-database"
