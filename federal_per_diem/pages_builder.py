"""Build a static, mobile-friendly GitHub Pages edition of the heat map."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from .config import PACKAGE_ROOT, Settings
from .dashboard import STATIC_ROOT, _one_year_after, database_context, heatmap_data
from .geo_lookup import ZctaGeometryIndex


SITE_GUIDE_FILENAME = "Using the GSA Rate Map URL.html"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _static_html() -> str:
    """Adapt the shared heat-map page to relative GitHub Pages assets."""

    html = (STATIC_ROOT / "heatmap.html").read_text(encoding="utf-8")
    versions = {
        filename: hashlib.sha256((STATIC_ROOT / filename).read_bytes()).hexdigest()[:12]
        for filename in ("styles.css", "heatmap.css", "heatmap.js")
    }
    replacements = {
        '<html lang="en">': '<html lang="en" data-static-map="true">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">': (
            '<meta name="viewport" content="width=device-width, initial-scale=1, '
            'viewport-fit=cover">\n'
            '<meta name="theme-color" content="#14507d">\n'
            '<meta name="apple-mobile-web-app-capable" content="yes">\n'
            '<link rel="manifest" href="./manifest.webmanifest">'
        ),
        'href="/static/vendor/leaflet.css"': 'href="./vendor/leaflet.css"',
        'href="/static/styles.css"': f'href="./styles.css?v={versions["styles.css"]}"',
        'href="/static/heatmap.css"': (
            f'href="./heatmap.css?v={versions["heatmap.css"]}"'
        ),
        'src="/static/vendor/leaflet.js"': 'src="./vendor/leaflet.js"',
        'src="/static/heatmap.js"': f'src="./heatmap.js?v={versions["heatmap.js"]}"',
        '<a class="ghost masthead-link" href="/">Rate dashboard</a>': (
            '<a class="ghost masthead-link" '
            'href="https://www.gsa.gov/travel/plan-a-trip/per-diem-rates" '
            'target="_blank" rel="noopener noreferrer">GSA source</a>'
        ),
        'Loading local database context&hellip;': 'Loading published rate data&hellip;',
    }
    for original, replacement in replacements.items():
        if original not in html:
            raise RuntimeError(f"Static page template marker is missing: {original}")
        html = html.replace(original, replacement)
    return html


def _prepare_output(output_dir: Path) -> None:
    """Remove only known generated paths before rebuilding the static bundle."""

    output_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("data", "vendor"):
        candidate = output_dir / directory
        if candidate.exists():
            shutil.rmtree(candidate)
    for filename in (
        "index.html",
        "styles.css",
        "heatmap.css",
        "heatmap.js",
        "manifest.webmanifest",
        SITE_GUIDE_FILENAME,
        ".nojekyll",
    ):
        candidate = output_dir / filename
        if candidate.exists():
            candidate.unlink()


def _copy_frontend(output_dir: Path) -> None:
    (output_dir / "index.html").write_text(_static_html(), encoding="utf-8")
    for filename in ("styles.css", "heatmap.css", "heatmap.js"):
        shutil.copy2(STATIC_ROOT / filename, output_dir / filename)
    shutil.copytree(STATIC_ROOT / "vendor", output_dir / "vendor")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    _write_json(
        output_dir / "manifest.webmanifest",
        {
            "name": "Federal Per Diem Heat Map",
            "short_name": "Per Diem Map",
            "start_url": "./",
            "display": "standalone",
            "background_color": "#f4f6f9",
            "theme_color": "#14507d",
        },
    )


def _export_state_rates(
    settings: Settings,
    geo: ZctaGeometryIndex,
    output_dir: Path,
) -> int:
    """Export compact interval arrays, split by drawable display state."""

    query = """
        SELECT l.zip_code, l.locality, l.is_standard,
               r.effective_start, r.effective_end,
               r.lodging_rate, r.mie_rate, r.first_last_day_mie
        FROM rates r
        JOIN locations l ON l.id = r.location_id
        ORDER BY l.zip_code, r.effective_start, l.locality
    """
    by_state: dict[str, dict[str, list[list[Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    with closing(
        sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(query):
            state = geo.state_for_zip(row["zip_code"])
            if state is None:
                continue
            by_state[state][row["zip_code"]].append(
                [
                    row["effective_start"],
                    row["effective_end"],
                    row["locality"],
                    int(row["is_standard"]),
                    float(row["lodging_rate"]),
                    float(row["mie_rate"]),
                    float(row["first_last_day_mie"]),
                ]
            )

    rate_count = 0
    for state, zips in sorted(by_state.items()):
        ordered = {zip_code: zips[zip_code] for zip_code in sorted(zips)}
        rate_count += sum(len(intervals) for intervals in ordered.values())
        _write_json(
            output_dir / "data" / "rates" / f"{state}.json",
            {"state": state, "zips": ordered},
        )
    return rate_count


def _export_zip_index(geo: ZctaGeometryIndex, output_dir: Path) -> int:
    """Export the compact ZIP/state/center lookup used by GitHub Pages."""

    centers = geo.zip_centers()
    zips = {
        zip_code: [state, round(latitude, 5), round(longitude, 5)]
        for zip_code, (state, latitude, longitude) in sorted(centers.items())
    }
    _write_json(output_dir / "data" / "zip-index.json", {"zips": zips})
    return len(zips)


def _compact_national_snapshot(
    snapshot: dict[str, Any],
    cell_layout: list[list[Any]] | None,
) -> tuple[dict[str, Any], list[list[Any]]]:
    """Store stable blob positions once and only date-varying values per day."""

    cells = snapshot.pop("cells")
    layout = [
        [cell["id"], cell["state"], cell["latitude"], cell["longitude"]]
        for cell in cells
    ]
    if cell_layout is not None and layout != cell_layout:
        raise RuntimeError("National heat-map cell layout changed between dates")
    snapshot["cellValues"] = [
        [
            cell["ratedZipCount"],
            cell["ambiguousZipCount"],
            cell["lodgingRate"],
            cell["mieRate"],
            cell["firstLastDayMie"],
        ]
        for cell in cells
    ]
    return snapshot, cell_layout or layout


def build_pages(
    settings: Settings | None = None,
    output_dir: Path | str | None = None,
    *,
    today: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Build the complete static site and return a concise build manifest."""

    settings = settings or Settings.from_env()
    output = Path(output_dir or PACKAGE_ROOT / "site").expanduser().resolve()
    current_date = today or date.today()
    planning_end = end_date or _one_year_after(current_date)
    if planning_end < current_date:
        raise ValueError("end_date cannot precede today")
    if not settings.database_path.is_file():
        raise FileNotFoundError(f"Rate database not found at {settings.database_path}")

    _prepare_output(output)
    _copy_frontend(output)

    geo = ZctaGeometryIndex(settings.geo_dir, settings=settings)
    try:
        (output / "data" / "geo").mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            settings.geo_dir / "states.geojson",
            output / "data" / "geo" / "states.geojson",
        )
        shutil.copytree(
            settings.geo_dir / "zcta",
            output / "data" / "geo" / "zcta",
        )

        snapshots: dict[str, dict[str, Any]] = {}
        cell_layout: list[list[Any]] | None = None
        cursor = current_date
        while cursor <= planning_end:
            snapshot = heatmap_data(
                settings, cursor, geo, today=current_date
            )
            snapshots[cursor.isoformat()], cell_layout = _compact_national_snapshot(
                snapshot, cell_layout
            )
            cursor += timedelta(days=1)
        _write_json(
            output / "data" / "national.json",
            {"cellLayout": cell_layout or [], "dates": snapshots},
        )

        exported_intervals = _export_state_rates(settings, geo, output)
        exported_zip_count = _export_zip_index(geo, output)
        database = database_context(settings)
        generated_at = datetime.now(timezone.utc).isoformat()
        context = {
            "hosting": "github-pages",
            "generatedAt": generated_at,
            "today": current_date.isoformat(),
            "travelWindow": {
                "start": current_date.isoformat(),
                "end": planning_end.isoformat(),
            },
            "database": database,
            "map": {
                "available": True,
                "states": geo.state_summaries(),
                "zctaCount": geo.manifest.get("zcta_count"),
            },
        }
        _write_json(output / "data" / "context.json", context)
    finally:
        geo.close()

    files = [path for path in output.rglob("*") if path.is_file()]
    manifest = {
        "output": str(output),
        "generatedAt": context["generatedAt"],
        "travelStart": current_date.isoformat(),
        "travelEnd": planning_end.isoformat(),
        "dateCount": len(snapshots),
        "rateIntervalCount": exported_intervals,
        "searchableZipCount": exported_zip_count,
        "fileCount": len(files),
        "bytes": sum(path.stat().st_size for path in files),
    }
    _write_json(output / "build-manifest.json", manifest)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PACKAGE_ROOT / "site")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    manifest = build_pages(Settings.from_env(data_dir=args.data_dir), args.output)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
