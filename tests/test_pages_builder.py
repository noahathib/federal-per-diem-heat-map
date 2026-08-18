from __future__ import annotations

import json
from datetime import date

from federal_per_diem.config import Settings
from federal_per_diem.database import build_database
from federal_per_diem.models import ValidationReport
from federal_per_diem.pages_builder import build_pages
from tests.conftest import make_rate


def test_build_pages_exports_relative_mobile_site(
    map_data_dir, source_metadata, tmp_path
):
    settings = Settings(data_dir=map_data_dir)
    rates = [
        make_rate(zip_code="10001", state="NY", destination_id="101"),
        make_rate(
            zip_code="10002",
            state="NY",
            locality="Locality A",
            destination_id="102",
        ),
        make_rate(
            zip_code="10002",
            state="NJ",
            locality="Locality B",
            destination_id="103",
        ),
        make_rate(zip_code="99999", state="NY", destination_id="104"),
    ]
    build_database(
        settings.database_path,
        rates,
        [source_metadata],
        ValidationReport(),
        fiscal_year=2026,
        started_at=source_metadata.downloaded_at,
    )

    output = tmp_path / "site"
    manifest = build_pages(
        settings,
        output,
        today=date(2026, 8, 17),
        end_date=date(2026, 8, 17),
    )

    assert manifest["dateCount"] == 1
    assert manifest["rateIntervalCount"] == 3
    html = (output / "index.html").read_text(encoding="utf-8")
    assert 'data-static-map="true"' in html
    assert 'src="./heatmap.js"' in html
    assert 'href="./vendor/leaflet.css"' in html
    assert "Site guide" not in html
    assert "/api/" not in html
    assert (output / ".nojekyll").exists()
    assert not (output / "Using the GSA Rate Map URL.html").exists()
    assert (output / "data" / "geo" / "zcta" / "NY.geojson").exists()

    national = json.loads((output / "data" / "national.json").read_text())
    snapshot = national["dates"]["2026-08-17"]
    assert snapshot["rateStatus"] == "official"
    assert snapshot["ratedZipCount"] == 1
    assert snapshot["ambiguousZipCount"] == 1
    assert national["cellLayout"] == [
        ["NY-0-3", "NY", 2.5, 22.5],
        ["NY-3-0", "NY", 5.0, 5.0],
    ]
    assert snapshot["cellValues"] == [
        [0, 1, None, None, None],
        [1, 0, 200.0, 80.0, 60.0],
    ]

    zip_index = json.loads((output / "data" / "zip-index.json").read_text())
    assert zip_index["zips"] == {
        "10001": ["NY", 5.0, 5.0],
        "10002": ["NY", 2.5, 22.5],
    }
    assert manifest["searchableZipCount"] == 2

    state = json.loads((output / "data" / "rates" / "NY.json").read_text())
    assert set(state["zips"]) == {"10001", "10002"}
    assert len(state["zips"]["10002"]) == 2
