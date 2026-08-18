"""Smoke test the deployed GitHub Pages heat map using only the standard library."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


class SmokeFailure(RuntimeError):
    """A production smoke-test assertion failed."""


def _fetch(url: str, *, expected_status: int = 200) -> tuple[bytes, dict[str, str]]:
    request = Request(url, headers={"User-Agent": "federal-per-diem-pages-smoke/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            body = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
        headers = {key.lower(): value for key, value in exc.headers.items()}
    except URLError as exc:
        raise SmokeFailure(f"Could not reach {url}: {exc.reason}") from exc
    if status != expected_status:
        raise SmokeFailure(f"Expected HTTP {expected_status} from {url}, got {status}")
    return body, headers


def _json(url: str) -> dict[str, Any]:
    body, _ = _fetch(url)
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"Invalid JSON at {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise SmokeFailure(f"Expected a JSON object at {url}")
    return value


def _require(condition: object, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _asset(html: str, pattern: str, label: str) -> str:
    match = re.search(pattern, html)
    if not match:
        raise SmokeFailure(f"Could not find the versioned {label} in index.html")
    return match.group(1)


def smoke(base_url: str, reference_site: Path | None = None) -> dict[str, Any]:
    """Validate the live shell, assets, date bundle, search index, and map data."""

    base = base_url.rstrip("/") + "/"
    index_body, index_headers = _fetch(base)
    index = index_body.decode("utf-8")
    _require("Federal Per Diem Heat Map" in index, "The live page title is missing")
    _require('data-static-map="true"' in index, "The live page is not the static map")
    _require("Site guide" not in index, "The removed site guide is still linked")
    _require('id="map-hint" class="hint" role="status" aria-live="polite"' in index,
             "Map status feedback is not exposed as a live region")
    _require('id="toast" class="toast" role="status" aria-live="polite"' in index,
             "Validation feedback is not exposed as a live region")

    script_ref = _asset(index, r'src="([^\"]*heatmap\.js\?v=[^\"]+)"', "map script")
    style_ref = _asset(index, r'href="([^\"]*styles\.css\?v=[^\"]+)"', "stylesheet")
    script = _fetch(urljoin(base, script_ref))[0].decode("utf-8")
    _fetch(urljoin(base, style_ref))
    for marker in ("state-gradient-layer", 'map.on("zoomanim", this._animateZoom, this)',
                   "zip-search-input"):
        _require(marker in script, f"The deployed map script is missing {marker!r}")
    _require("L.circleMarker" not in script, "Legacy circle hotspots are still deployed")

    context = _json(urljoin(base, "data/context.json"))
    travel_window = context.get("travelWindow", {})
    start = date.fromisoformat(travel_window["start"])
    end = date.fromisoformat(travel_window["end"])
    _require(context.get("today") == start.isoformat(), "Travel-window start differs from today")
    _require((end - start).days in {365, 366}, "Travel window is not one year")

    national = _json(urljoin(base, "data/national.json"))
    layout = national.get("cellLayout", [])
    dates = national.get("dates", {})
    _require(len(layout) >= 1_000, "National hotspot grid is unexpectedly sparse")
    _require(start.isoformat() in dates and end.isoformat() in dates,
             "National data does not span the complete travel window")
    start_values = dates[start.isoformat()].get("cellValues", [])
    _require(len(start_values) == len(layout), "National cell layout/value lengths differ")
    lodging_values = [row[2] for row in start_values if len(row) > 2 and row[2] is not None]
    _require(lodging_values and max(lodging_values) >= 400,
             "National data no longer preserves high-cost lodging hotspots")

    zip_index = _json(urljoin(base, "data/zip-index.json")).get("zips", {})
    for zip_code, state in (("02108", "MA"), ("19103", "PA")):
        _require(zip_index.get(zip_code, [None])[0] == state,
                 f"ZIP {zip_code} is missing from the search index")
    pa_rates = _json(urljoin(base, "data/rates/PA.json")).get("zips", {})
    _require("19103" in pa_rates, "Representative Philadelphia rates are missing")
    _fetch(urljoin(base, "data/geo/states.geojson"))
    _fetch(urljoin(base, "data/geo/zcta/PA.geojson"))

    removed_guide = urljoin(base, quote("Using the GSA Rate Map URL.html"))
    _fetch(removed_guide, expected_status=404)

    if reference_site is not None:
        local_index = (reference_site / "index.html").read_text(encoding="utf-8")
        local_script_ref = _asset(
            local_index, r'src="([^\"]*heatmap\.js\?v=[^\"]+)"', "local map script"
        )
        _require(script_ref == local_script_ref,
                 f"Pages is stale: live script {script_ref!r}, expected {local_script_ref!r}")
        local_context = json.loads(
            (reference_site / "data" / "context.json").read_text(encoding="utf-8")
        )
        _require(context.get("generatedAt") == local_context.get("generatedAt"),
                 "Pages is still serving an older generated data bundle")

    return {
        "url": base,
        "script": script_ref,
        "travelStart": start.isoformat(),
        "travelEnd": end.isoformat(),
        "nationalCells": len(layout),
        "contentType": index_headers.get("content-type"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Deployed Pages base URL")
    parser.add_argument("--reference-site", type=Path)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--delay", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.attempts < 1:
        parser.error("--attempts must be at least 1")
    reference_site = args.reference_site.resolve() if args.reference_site else None

    last_error: SmokeFailure | None = None
    for attempt in range(1, args.attempts + 1):
        try:
            result = smoke(args.url, reference_site)
            print(json.dumps(result, indent=2))
            return 0
        except (SmokeFailure, KeyError, ValueError) as exc:
            last_error = exc if isinstance(exc, SmokeFailure) else SmokeFailure(str(exc))
            if attempt < args.attempts:
                print(f"Attempt {attempt}/{args.attempts} failed: {last_error}")
                time.sleep(args.delay)
    assert last_error is not None
    print(f"Smoke test failed: {last_error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
