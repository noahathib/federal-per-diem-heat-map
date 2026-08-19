from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date

import pytest

from federal_per_diem.config import Settings
from federal_per_diem.database import build_database
from federal_per_diem.dashboard import DashboardServer, _heatmap_cells
from federal_per_diem.models import ValidationReport


@pytest.fixture
def fixed_dashboard_today(monkeypatch):
    """Keep date-window HTTP tests deterministic as the real calendar advances."""

    import federal_per_diem.dashboard as dashboard_module

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 18)

    monkeypatch.setattr(dashboard_module, "date", FixedDate)
    return FixedDate.today()


@pytest.fixture
def server(map_data_dir):
    """Run the real dashboard server on an ephemeral loopback port."""

    instance = DashboardServer(("127.0.0.1", 0), Settings(data_dir=map_data_dir))
    # A short poll interval keeps shutdown() from waiting out the 0.5s default.
    thread = threading.Thread(
        target=instance.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_address[1]}"
    finally:
        instance.shutdown()
        if instance._geo is not None:
            instance._geo.close()
        instance.server_close()
        thread.join(timeout=5)


@pytest.fixture
def rate_server(map_data_dir, source_metadata):
    """Run the dashboard with rated and multi-locality synthetic ZIPs."""

    from tests.conftest import make_rate

    settings = Settings(data_dir=map_data_dir)
    rates = [
        make_rate(
            zip_code="10001",
            state="NY",
            locality="Manhattan",
            destination_id="101",
            lodging="150.00",
            mie="80.00",
        ),
        make_rate(
            zip_code="10002",
            state="NY",
            locality="Locality A, Metro Division",
            destination_id="102",
            lodging="200.00",
            mie="90.00",
        ),
        make_rate(
            zip_code="10002",
            state="NJ",
            locality="Locality B",
            destination_id="103",
            lodging="220.00",
            mie="92.00",
        ),
        make_rate(
            zip_code="99999",
            state="NY",
            locality="Unmapped postal ZIP",
            destination_id="104",
            lodging="300.00",
            mie="100.00",
        ),
    ]
    build_database(
        settings.database_path,
        rates,
        [source_metadata],
        ValidationReport(),
        fiscal_year=2026,
        started_at=source_metadata.downloaded_at,
    )
    instance = DashboardServer(("127.0.0.1", 0), settings)
    thread = threading.Thread(
        target=instance.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_address[1]}"
    finally:
        instance.shutdown()
        if instance._geo is not None:
            instance._geo.close()
        instance.server_close()
        thread.join(timeout=5)


def get(base, path, headers=None):
    request = urllib.request.Request(base + path, headers=headers or {})
    return urllib.request.urlopen(request, timeout=15)


def post(base, path, payload):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=15)


def status_of(callable_):
    try:
        callable_()
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())
    raise AssertionError("expected an HTTP error")


# ------------------------------------------------------------------ static

def test_root_serves_the_dashboard(server):
    response = get(server, "/")
    body = response.read().decode("utf-8")
    assert response.status == 200
    assert "Federal Per Diem" in body
    assert "/static/vendor/leaflet.js" in body


def test_heatmap_page_is_a_separate_static_view(server):
    response = get(server, "/heatmap")
    body = response.read().decode("utf-8")
    assert response.status == 200
    assert "Federal Per Diem Heat Map" in body
    assert "/static/heatmap.js" in body


def test_leaflet_is_served_locally(server):
    response = get(server, "/static/vendor/leaflet.js")
    assert response.status == 200
    assert "javascript" in response.headers["Content-Type"]


def test_dashboard_assets_are_not_cached_but_vendored_ones_are(server):
    """An edit to app.js must not keep serving a stale browser copy."""

    for path in ("/", "/static/app.js", "/static/styles.css"):
        assert get(server, path).headers["Cache-Control"] == "no-cache", path
    assert "max-age" in get(server, "/static/vendor/leaflet.js").headers["Cache-Control"]


@pytest.mark.parametrize(
    "path",
    [
        "/static/../config.py",
        "/static/../../pyproject.toml",
        "/static/%2e%2e/config.py",
        "/static/nothing.js",
    ],
)
def test_static_paths_cannot_escape_the_asset_root(server, path):
    code, _ = status_of(lambda: get(server, path))
    assert code == 404


def test_unknown_route_is_a_404(server):
    code, body = status_of(lambda: get(server, "/api/nope"))
    assert code == 404 and "No route" in body["error"]


# ----------------------------------------------------------------- context

def test_context_reports_map_and_database_state(server, fixed_dashboard_today):
    payload = json.loads(get(server, "/api/context").read())
    assert payload["map"]["available"] is True
    assert payload["map"]["zctaCount"] == 2
    assert payload["map"]["exactHitTesting"] is True
    assert payload["database"]["exists"] is False
    assert payload["today"] == fixed_dashboard_today.isoformat()
    assert payload["travelWindow"] == {
        "start": "2026-08-18",
        "end": "2027-08-18",
    }


# ----------------------------------------------------------------- heat map

def test_heatmap_requires_an_existing_database(server):
    code, body = status_of(lambda: get(server, "/api/heatmap?date=2026-08-17"))
    assert code == 503
    assert "database not found" in body["error"].lower()


@pytest.mark.parametrize(
    "path",
    [
        "/api/heatmap",
        "/api/heatmap?date=tomorrow",
        "/api/heatmap?date=2026-08-17&state=New%20York",
        "/api/heatmap?date=2026-08-17&state=NY&state=PA",
    ],
)
def test_heatmap_rejects_bad_parameters(server, path):
    code, _ = status_of(lambda: get(server, path))
    assert code == 400


def test_national_heatmap_summarizes_only_unambiguous_zip_rates(rate_server):
    payload = json.loads(get(rate_server, "/api/heatmap?date=2026-08-17").read())
    assert payload["scope"] == "nation"
    assert payload["travelDate"] == "2026-08-17"
    assert payload["fiscalYear"] == 2026
    assert payload["rateDate"] == "2026-08-17"
    assert payload["rateFiscalYear"] == 2026
    assert payload["rateStatus"] == "official"
    assert payload["ratedZipCount"] == 1
    assert payload["ambiguousZipCount"] == 1
    # 99999 has a rate but no generated Census ZCTA polygon, so it is not map data.
    assert payload["states"] == [
        {
            "state": "NY",
            "ratedZipCount": 1,
            "ambiguousZipCount": 1,
            "lodgingRate": 150.0,
            "mieRate": 80.0,
            "firstLastDayMie": 60.0,
        }
    ]
    assert payload["cells"] == [
        {
            "id": "NY-0-5",
            "state": "NY",
            "latitude": 2.5,
            "longitude": 22.5,
            "ratedZipCount": 0,
            "ambiguousZipCount": 1,
            "lodgingRate": None,
            "mieRate": None,
            "firstLastDayMie": None,
        },
        {
            "id": "NY-5-0",
            "state": "NY",
            "latitude": 5.0,
            "longitude": 5.0,
            "ratedZipCount": 1,
            "ambiguousZipCount": 0,
            "lodgingRate": 150.0,
            "mieRate": 80.0,
            "firstLastDayMie": 60.0,
        },
    ]


def test_national_cells_preserve_a_small_local_high_rate():
    rows = [
        {
            "zip_code": "10001",
            "candidate_count": 1,
            "lodging_rate": 110.0,
            "mie_rate": 68.0,
            "first_last_day_mie": 51.0,
        },
        {
            "zip_code": "10002",
            "candidate_count": 1,
            "lodging_rate": 250.0,
            "mie_rate": 92.0,
            "first_last_day_mie": 69.0,
        },
    ]

    class SameCellGeo:
        @staticmethod
        def center_for_zip(zip_code):
            return (40.0, -75.0)

    cells = _heatmap_cells([(row, "NY") for row in rows], SameCellGeo())
    assert len(cells) == 1
    assert cells[0]["lodgingRate"] == 250.0
    assert cells[0]["mieRate"] == 92.0
    assert cells[0]["ratedZipCount"] == 2


def test_future_heatmap_uses_labeled_same_season_planning_rates(
    rate_server, fixed_dashboard_today
):
    payload = json.loads(get(rate_server, "/api/heatmap?date=2027-08-17").read())
    assert payload["travelDate"] == "2027-08-17"
    assert payload["fiscalYear"] == 2027
    assert payload["rateDate"] == "2026-08-17"
    assert payload["rateFiscalYear"] == 2026
    assert payload["rateStatus"] == "planning-estimate"
    assert payload["ratedZipCount"] == 1
    assert payload["ambiguousZipCount"] == 1


def test_heatmap_does_not_project_beyond_the_one_year_planning_window(
    rate_server, fixed_dashboard_today
):
    code, body = status_of(
        lambda: get(rate_server, "/api/heatmap?date=2027-08-19")
    )
    assert code == 404
    assert "planning estimates are available" in body["error"]


def test_state_heatmap_keeps_multi_locality_zips_out_of_gradient(rate_server):
    payload = json.loads(
        get(rate_server, "/api/heatmap?date=2026-08-17&state=ny").read()
    )
    assert payload["scope"] == "NY"
    assert payload["ranges"]["lodgingRate"] == {"min": 150.0, "max": 150.0}
    by_zip = {entry["zip"]: entry for entry in payload["rates"]}
    assert by_zip["10001"]["status"] == "rated"
    assert by_zip["10001"]["lodgingRate"] == 150.0
    assert by_zip["10002"]["status"] == "ambiguous"
    assert by_zip["10002"]["candidateCount"] == 2
    assert by_zip["10002"]["candidates"] == [
        "Locality A, Metro Division",
        "Locality B",
    ]
    assert by_zip["10002"]["lodgingRate"] is None


# --------------------------------------------------------------------- geo

def test_state_layer_is_served(server):
    response = get(server, "/api/geo/states")
    assert response.status == 200
    assert json.loads(response.read())["type"] == "FeatureCollection"


@pytest.mark.parametrize("layer", ["zcta", "counties", "municipal", "localities"])
def test_state_detail_layers_are_served(server, layer):
    response = get(server, f"/api/geo/{layer}/NY")
    assert response.status == 200
    assert json.loads(response.read())["type"] == "FeatureCollection"


def test_layers_are_gzipped_when_the_client_accepts_it(server):
    response = get(server, "/api/geo/zcta/NY", {"Accept-Encoding": "gzip"})
    assert response.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(response.read()))["type"] == "FeatureCollection"


def test_layer_is_plain_when_gzip_is_not_accepted(server):
    response = get(server, "/api/geo/zcta/NY", {"Accept-Encoding": "identity"})
    assert response.headers.get("Content-Encoding") is None
    assert json.loads(response.read())["type"] == "FeatureCollection"


def test_missing_state_layer_is_a_404(server):
    code, _ = status_of(lambda: get(server, "/api/geo/zcta/CA"))
    assert code == 404


@pytest.mark.parametrize("state", ["ZZZ", "1", "N%20Y"])
def test_bad_state_codes_are_rejected(server, state):
    code, _ = status_of(lambda: get(server, f"/api/geo/zcta/{state}"))
    assert code in {400, 404}


# --------------------------------------------------------------------- zip

def test_known_zip_returns_bounds(server):
    payload = json.loads(get(server, "/api/zip/10001").read())
    assert payload["found"] is True
    assert payload["state"] == "NY"
    assert payload["bounds"] == [[0.0, 0.0], [10.0, 10.0]]
    assert payload["center"] == [5.0, 5.0]


def test_zip_without_a_boundary_is_reported_not_errored(server):
    payload = json.loads(get(server, "/api/zip/99999").read())
    assert payload["found"] is False
    assert "Tabulation Area" in payload["message"]


def test_malformed_zip_is_a_400(server):
    code, body = status_of(lambda: get(server, "/api/zip/abcde"))
    assert code == 400 and "ZIP" in body["error"]


# ------------------------------------------------------------------ locate

def test_locate_resolves_a_point_inside_a_polygon(server):
    payload = json.loads(post(server, "/api/locate", {"latitude": 2, "longitude": 2}).read())
    assert payload == {
        "found": True,
        "zip": "10001",
        "state": "NY",
        "latitude": 2.0,
        "longitude": 2.0,
        "exact": True,
        "distance_km": 0.0,
    }


def test_locate_falls_back_to_the_nearest_area(server):
    payload = json.loads(
        post(server, "/api/locate", {"latitude": 2, "longitude": 15}).read()
    )
    assert payload["found"] is True and payload["exact"] is False
    assert payload["distance_km"] > 0


@pytest.mark.parametrize(
    "payload",
    [{}, {"latitude": 2}, {"latitude": "x", "longitude": 2}, {"latitude": 99, "longitude": 2}],
)
def test_locate_rejects_bad_coordinates(server, payload):
    code, _ = status_of(lambda: post(server, "/api/locate", payload))
    assert code == 400


# --------------------------------------------------------------------- run

def test_unknown_action_is_rejected(server):
    code, body = status_of(lambda: post(server, "/api/run", {"action": "rm"}))
    assert code == 400 and "Unknown action" in body["error"]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "refresh", "fiscalYear": 2026},
        {"action": "refresh", "fiscalYear": 2026, "validateOnly": True},
        {"action": "build-map"},
        {"action": "build-map", "forceDownload": True},
    ],
)
def test_data_replacing_actions_cannot_be_started_over_http(server, payload):
    """A refresh or map rebuild replaces published data; HTTP must not reach it."""

    code, body = status_of(lambda: post(server, "/api/run", payload))
    assert code == 400
    assert "Unknown action" in body["error"]
    assert "refresh" not in body["error"].split("expected one of")[-1]


def test_context_advertises_read_only_and_the_manual_commands(server):
    payload = json.loads(get(server, "/api/context").read())
    assert payload["readOnly"] is True
    assert payload["actions"] == ["estimate", "query", "validate"]
    commands = [entry["command"] for entry in payload["manualCommands"]]
    assert any("refresh_rates.py" in command for command in commands)
    assert any("build_map_data.py" in command for command in commands)


def test_lookups_still_work_while_read_only(server):
    """View-only removes writes, not the ability to select a point and query."""

    located = json.loads(
        post(server, "/api/locate", {"latitude": 2, "longitude": 2}).read()
    )
    assert located["zip"] == "10001"
    started = json.loads(
        post(
            server,
            "/api/run",
            {"action": "query", "zip": located["zip"], "date": "2026-08-17"},
        ).read()
    )
    assert started["command"].startswith("python scripts/query_rate.py")


def test_invalid_arguments_are_rejected_before_anything_runs(server):
    code, body = status_of(
        lambda: post(server, "/api/run", {"action": "query", "zip": "nope", "date": "x"})
    )
    assert code == 400 and "ZIP" in body["error"]


def test_run_starts_a_job_and_reports_its_command(server):
    started = json.loads(
        post(
            server,
            "/api/run",
            {"action": "query", "zip": "19103", "date": "2026-08-17"},
        ).read()
    )
    assert started["status"] == "running"
    assert started["command"].startswith("python scripts/query_rate.py --zip 19103")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = json.loads(get(server, f"/api/job/{started['id']}").read())
        if job["status"] != "running":
            break
        time.sleep(0.2)
    # No database exists under the temporary data directory, so the command is
    # expected to fail; what matters is that a real process ran and reported.
    assert job["status"] in {"completed", "failed"}
    assert job["returncode"] is not None
    assert job["durationMs"] >= 0


def test_unknown_job_is_a_404(server):
    code, _ = status_of(lambda: get(server, "/api/job/does-not-exist"))
    assert code == 404


def test_oversized_request_body_is_rejected(server):
    code, _ = status_of(
        lambda: post(server, "/api/locate", {"pad": "x" * 70_000, "latitude": 1})
    )
    assert code == 400
