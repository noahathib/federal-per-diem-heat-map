# Federal Per Diem

`federal-per-diem` is a local, auditable Python data pipeline for answering:

> Given a U.S. ZIP code and travel date, what federal lodging and M&IE rates apply?

It downloads official GSA and Defense Travel Management Office (DTMO) files, maps the ZIP Code Tabulation Areas of Alaska, Hawaii, and the five ZIP-addressable U.S. territories with U.S. Census Bureau relationship files, normalizes every source to one effective-date schema, validates the replacement, and publishes SQLite, CSV, and Excel outputs. Lookups run entirely against SQLite; they do not depend on a government website being available at query time.

## Current build

The checked-out FY2026 build was generated and verified on 2026-08-17:

| Output | Contents |
|---|---|
| `data/processed/federal_per_diem.sqlite` | Canonical indexed database |
| `data/processed/federal_per_diem.csv` | Full long-format export |
| `data/processed/federal_per_diem.xlsx` | Rates, Locations, Sources, Refresh Log, and Validation Summary |

It contains 514,196 effective-date rate records, 42,849 location mappings, all 50 states, the District of Columbia, and the five ZIP-addressable U.S. territories, from seven source files with SHA-256 provenance.

Verified FY2026 examples for 2026-08-17:

| ZIP | Resolution | Lodging | M&IE | Source |
|---|---|---:|---:|---|
| 19103 | Philadelphia, PA | $187.00 | $92.00 | GSA |
| 35004 | Standard CONUS, AL | $110.00 | $68.00 | GSA |
| 99501 | Anchorage, AK | $329.00 | $148.00 | DTMO |
| 96815 | Honolulu, HI | $202.00 | $163.00 | DTMO |
| 00802 | St. Thomas, VI | $414.00 | $150.00 | DTMO |
| 96950 | Saipan, MP | $161.00 | $113.00 | DTMO |

The values above were compared with the raw files retained under `data/raw/FY2026`.

## Python setup

Python 3.11 or newer is required. The project uses only `pandas`, `openpyxl`, and `requests` at runtime; SQLite is from Python's standard library. Tests use `pytest` and `pytest-cov`.

An isolated `.venv` is already configured in this checkout and was verified with Python 3.13.14. Its dependency check reports no broken requirements.

macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip check
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pip check
```

Windows Command Prompt:

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -e ".[dev]"
python -m pip check
```

## Quick start

Query a rate:

```bash
python scripts/query_rate.py --zip 19103 --date 2026-08-17
python scripts/query_rate.py --zip 96815 --date 2026-08-17 --json
python scripts/query_rate.py --zip 35004 --date 2026-08-17 --explain
```

Use the Python API:

```python
from federal_per_diem import get_per_diem

rate = get_per_diem(zip_code="19103", date="2026-08-17")
print(rate.lodging_rate)       # Decimal('187.00')
print(rate.mie_rate)           # Decimal('92.00')
print(rate.first_last_day_mie) # Decimal('69.00')
print(rate.to_dict())
```

Estimate a trip:

```bash
python scripts/estimate_trip.py \
  --zip 19103 \
  --start-date 2026-08-17 \
  --end-date 2026-08-20 \
  --travelers 2
```

```python
from federal_per_diem import estimate_trip

trip = estimate_trip(
    "19103",
    "2026-08-17",
    "2026-08-20",
    travelers=2,
)
print(trip.per_person_total)
print(trip.group_total)
```

Lodging is looked up for each night. M&IE uses 75% on the first and last travel days and the full locality amount on intervening days. Month and fiscal-year boundaries are therefore handled naturally. Mileage is optional and requires an explicit `mileage_rate`; no undated mileage value is hard-coded.

## Map dashboard

A local dashboard selects a ZIP from a map and runs the commands above for you.

Build the map layers once, then start the server:

```bash
python scripts/build_map_data.py
python scripts/dashboard.py --open
```

The dashboard listens on `http://127.0.0.1:8765/`. Use `--host`, `--port`, and `--data-dir` to change that.

Selecting a ZIP works two ways:

- **Type it.** Any five-digit ZIP or ZIP+4. The map jumps to it when it has a boundary.
- **Click it.** Click a state to load its ZIP Code Tabulation Areas, then click anywhere inside. The clicked point is resolved by an exact point-in-polygon test.

Selection drives three panels: a rate lookup, a trip estimate, and a database check. Every panel runs a real script as a child process and streams the command line, stdout, stderr, exit code, and duration into a transcript pane, so the dashboard is a view of the pipeline rather than a second implementation of it.

Open `http://127.0.0.1:8765/heatmap` (or use **Rate heat map** in the header) for a separate analytical map. It can switch among lodging, daily M&IE, and first/last-day M&IE for travel dates from today through the same date next year. When an official future fiscal year has not been loaded yet, the map uses the latest loaded fiscal year's equivalent seasonal date as a clearly labeled planning estimate; projected rows are never written to SQLite. The national view colors states by the median unambiguous mapped-ZIP rate; clicking a state loads exact ZIP-area colors. ZIPs that intersect multiple official localities are shown separately rather than assigned a guessed value, and postal ZIPs without a Census ZCTA are omitted because they have no polygon to color. The heat-map endpoint reads SQLite in read-only mode and does not modify published data.

### Mobile GitHub Pages edition

GitHub Pages cannot run the Python/SQLite dashboard server, so the repository also ships a generated static edition. It precomputes the small national date summaries and loads compact rate intervals and ZIP geometry only for the state selected on the map. The result keeps the one-year planning window and official-versus-estimate labels without shipping the database to the browser.

Build or refresh the publishable `site/` directory after changing rates or map data:

```bash
python scripts/build_github_pages.py
```

The Pages workflow deploys `site/` whenever `main` is pushed. The static site uses relative asset URLs, so it works from either an account Pages site or a project Pages subpath.

The saved `Using the GSA Rate Map URL.html` walkthrough is copied into every static build and linked as **Site guide** in the map header.

### The dashboard is read-only

It can query a rate, estimate a trip, and validate the existing database. Those only read. It cannot refresh rates or rebuild map layers, because those replace published data:

```bash
python scripts/refresh_rates.py --fiscal-year 2026
python scripts/build_map_data.py
```

Those stay manual terminal operations. The Database tab lists them with a copy button so they are documented where you need them, not runnable from the page. Enforcement is structural rather than a flag: `dashboard.py` maps an action name to a command builder, and the writing scripts simply have no entry, so `/api/run` rejects them with `400 Unknown action`. Nothing reachable over HTTP can alter what the database holds.

Notes on the map:

- Shading marks which ZIP areas have a published rate in the local database. Clicking one that has none still runs the query and reports what the command said.
- A ZIP that intersects several localities returns the same `AmbiguousRateError` the CLI returns, listing every candidate locality instead of guessing.
- Clicking outside any ZIP area reports the nearest one and its distance rather than silently picking a neighbour.
- The drawn polygons are simplified for speed; click resolution uses the unmodified Census shapefile, so the two cannot disagree.
- No basemap tiles are loaded by default and the page has no third-party requests. An optional street-basemap toggle fetches OpenStreetMap tiles if you want that context.

### Starting it without a terminal

`scripts/start_dashboard.command` runs from a Finder double-click. It binds every
interface, opens a browser, and holds the Mac awake (`caffeinate -is`) until you
press Ctrl-C or close the window. Override the defaults with `PER_DIEM_HOST` and
`PER_DIEM_PORT`.

For a listener that starts at login and comes back if it crashes, install the
launchd agent instead:

```bash
cp scripts/com.federal-per-diem.dashboard.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.federal-per-diem.dashboard.plist
```

It logs to `~/Library/Logs/federal-per-diem-dashboard.log`. Sleeping the Mac does
not stop either one: macOS suspends the process and it resumes on wake, still
listening. Only a logout, a restart, or `launchctl unload` ends it.

### Serving it to other machines

Binding anywhere other than loopback puts the dashboard on the network, so it
then requires a password on every route. Set one once:

```bash
.venv/bin/federal-per-diem-dashboard --set-password
```

The password is stored as a salted PBKDF2-SHA256 digest in
`~/.config/federal-per-diem/dashboard-auth.json`, mode `0600`. The password
itself is never written to the repository, and never logged. Then:

```bash
.venv/bin/federal-per-diem-dashboard --host 0.0.0.0
```

That listens on every interface and prints the address other machines should
open, so you do not have to look the host's IP up:

```
Dashboard:  http://127.0.0.1:8765/
Network:    http://10.9.4.85:8765/
            Password required. Any user name works.
            HTTP sends that password unencrypted, so treat it
            as protection from strangers, not from snooping.
```

Visitors get their browser's standard sign-in box. Any user name is accepted, so
only the password has to be shared. Requests arriving over loopback are not
challenged: the password exists to stop strangers on the network, not the person
sitting at the machine.

A few properties worth knowing before handing the address out:

- Every route is behind the password, including the static assets and all of
  `/api`, so an unauthenticated caller cannot reach the map layers or start a
  subprocess through `/api/run`.
- A wrong password costs the caller half a second, and ten wrong passwords lock
  that address out for a minute. Wrong attempts are logged with their source.
- HTTP Basic over plain HTTP is base64, not encryption. Anyone able to watch the
  wire between the two machines can read the password. On an untrusted network
  prefer an SSH tunnel, which needs no dashboard password at all because it
  arrives over loopback:

  ```bash
  ssh -N -L 8765:127.0.0.1:8765 you@this-host
  ```

- Serving the network with no password at all requires saying so explicitly with
  `--no-password`. Without a stored credential the server refuses to start rather
  than come up unprotected.

The address changes when the host moves between networks or its lease expires,
so read it from the startup output rather than saving it. macOS may ask once to
allow incoming connections for the Python interpreter; the port stays closed to
other machines until that is allowed.

Because the dashboard is read-only, a visitor who signs in can look up rates but cannot refresh, rebuild, or otherwise change anything. Request values never reach a shell and never become file paths: each action re-validates its inputs through the package's own parsers and emits a fixed argument vector. Requests do consume this machine's CPU, so bind to a network you trust.

## Refreshing rates

Run a normal protected refresh:

```bash
python scripts/refresh_rates.py --fiscal-year 2026
```

Useful modes:

```bash
python scripts/refresh_rates.py --fiscal-year 2026 --validate-only
python scripts/refresh_rates.py --fiscal-year 2026 --force
python scripts/refresh_rates.py --fiscal-year 2026 --gsa-only
python scripts/refresh_rates.py --fiscal-year 2026 --dod-only
python scripts/refresh_rates.py --fiscal-year 2026 --verbose
```

`--gsa-only` and `--dod-only` are adapter diagnostics: they validate that source independently and never replace the complete 50-state production database. `--force` rechecks remote files. If a forced download differs, the prior raw file is preserved unchanged under a timestamped `previous-...` name and the validated new payload becomes the canonical cached filename.

A successful refresh replaces only the requested fiscal year and retains other validated fiscal years already in SQLite. To support a real trip that crosses September 30, load both years (in either order):

```bash
python scripts/refresh_rates.py --fiscal-year 2025
python scripts/refresh_rates.py --fiscal-year 2026
```

DTMO's site may reject automated clients at its edge even when the official archive works in a browser. In that case, download the annual `OCONUS-ASCII-YYYY.zip` from DTMO's official rate-download form and place it in `data/raw/FY<fy>/`; the next non-forced refresh validates and uses the cache. URLs are configurable through environment variables described below.

## How the pipeline works

1. `downloader.py` discovers configured official URLs, streams to a `.part` file, checks status, size, content signature, and SHA-256, then caches the unmodified payload.
2. `gsa_parser.py` reads the FY ZIP developer workbook. Each ZIP becomes 12 effective monthly rows. `DestinationID == 0` is an explicit standard CONUS record.
3. `dod_parser.py` reads every normal (non-`nm`) monthly file in DTMO's calendar-year ASCII archives. M&IE is the published local meal rate plus local incidental rate.
4. `census_parser.py` selects the largest geographic intersection for each non-CONUS ZCTA with a Census place, county, and county subdivision. Alaska and Hawaii resolve by place name and island; Puerto Rico by municipio; the U.S. Virgin Islands and the Northern Mariana Islands by island county GEOID; Guam and American Samoa to their territory-wide locality. Military installations are never inferred from a civilian ZIP. `docs/SOURCES.md` records the full policy.
5. `normalizer.py` maps those ZCTAs to a DTMO locality or the agency's published `[OTHER]` locality. DTMO mid-month season boundaries remain separate exact-date intervals.
6. `validation.py` checks structure, money values, 75% M&IE, fiscal-year/date coverage, duplicate keys, all 50 states, all seven non-CONUS areas, standard CONUS, source agencies, and changes from the previous database.
7. `database.py` builds a temporary SQLite file, runs `PRAGMA integrity_check`, and generates temporary CSV/Excel exports.
8. Only after every step succeeds does `promote_outputs()` archive prior outputs and replace the production set.

The map layers are built separately by `scripts/build_map_data.py` and are never part of a rate refresh, so a boundary rebuild cannot affect the rate database.

The dependency runs the other way, though. `geo_builder.py` reads the rate database to stamp each ZCTA with `inDatabase` and each state with `ratedZipCount`, so a refresh that changes which ZIPs have rates leaves those coverage flags stale until the map is rebuilt. Geometry is unaffected; only the coverage shading is. Re-run `scripts/build_map_data.py` after any refresh that adds or removes rated ZIPs.

1. `downloader.py` caches the Census state and ZCTA cartographic boundary archives with the same SHA-256 provenance as every other source.
2. `shapefile_reader.py` reads the ESRI polygon and dBASE formats directly, validating each structural field against the published specification.
3. `geo_builder.py` assigns each ZCTA to its largest-area state, writes simplified per-state GeoJSON for drawing, and writes a bounding-box index plus a manifest recording source URLs, hashes, the coordinate reference system, and the simplification tolerance.
4. `geo_lookup.py` resolves a clicked coordinate by narrowing on the index and running an exact point-in-polygon test against the unmodified `.shp`.

No failed parse, validation, database build, or export operation can overwrite the last known-good database.

## Official sources

- GSA [Per diem files](https://www.gsa.gov/travel/plan-a-trip/per-diem-rates/per-diem-files): fiscal-year ZIP developer and master rates workbooks.
- GSA [Per diem API documentation](https://open.gsa.gov/api/perdiem/): used to confirm destination IDs, standard CONUS behavior, monthly fields, M&IE, ZIP semantics, and fiscal-year conventions. The production refresh prefers downloadable flat files and does not require an API key.
- DTMO [Per diem rate lookup and downloads](https://www.travel.dod.mil/Travel-Transportation-Rates/Per-Diem/Per-Diem-Rate-Lookup/): calendar-year OCONUS ASCII archives for Alaska, Hawaii, Puerto Rico, Guam, the U.S. Virgin Islands, American Samoa, and the Northern Mariana Islands. DTMO rates may be revised monthly. The same archives carry foreign rates, which are not ingested: foreign localities have no ZIP code, and the Department of State, not DTMO, is their authoritative publisher.
- Census Bureau [2020 geographic relationship files](https://www.census.gov/geographies/reference-files/2020/geo/relationship-files.html): ZCTA-to-place, ZCTA-to-county, and ZCTA-to-county-subdivision relationships.
- Census Bureau [cartographic boundary files](https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html): the 2020 state (1:20,000,000) and ZCTA (1:500,000) shapefiles behind the dashboard map. ZCTAs are published only in the 2020 vintage.

See [`docs/SOURCES.md`](docs/SOURCES.md) for the inspected schemas, source URL templates, and assumptions.

## Public API

```python
get_per_diem(zip_code, date)
get_state_rates(state, fiscal_year)
get_all_rates(fiscal_year)
compare_states(states, fiscal_year)
explain_rate(zip_code, date)
estimate_trip(zip_code, start_date, end_date, ...)
```

Money is returned as `Decimal`. Result dataclasses provide `to_dict()` methods for JSON-friendly serialization.

State comparisons report ZIP count, locality count, minimum/maximum/median lodging, minimum/maximum M&IE, and seasonal-locality count. They do not imply that a state has one rate.

## SQLite schema

- `locations`: ZIP, state, Census/GSA place and county, destination, locality, destination ID, and standard-rate flag.
- `rates`: fiscal year, month, exact inclusive effective interval, lodging, M&IE, first/last-day M&IE, and source member.
- `sources`: agency, dataset, URL, timestamp, filename, SHA-256, size, record count, parser version, and validation status.
- `refresh_history`: refresh timing, status, source and record counts, and the complete validation report.
- `metadata`: schema version.

The dashboard map layers live outside SQLite, under `data/processed/geo`:

- `states.geojson`: one polygon per state with its ZCTA and rated-ZIP counts.
- `zcta/<ST>.geojson`: simplified ZIP areas for one state, flagged by whether the ZIP has a rate.
- `index.npz`: ZIP, state, bounding box, and shapefile record number for all 33,791 ZCTAs.
- `manifest.json`: source URLs, SHA-256 hashes, coordinate reference system, tolerance, and per-state counts.

Uniqueness constraints prevent repeated refreshes from producing duplicates. Indexes cover ZIP, state, fiscal year, month, effective dates, and location.

## Data directories

```text
data/
├── raw/FY2026/       # immutable downloaded government files
├── raw/geo/          # Census cartographic boundary archives
├── processed/        # canonical SQLite plus CSV/XLSX exports
├── processed/geo/    # generated map layers (states, per-state ZCTA, index)
└── archive/          # timestamped last-known-good outputs
```

Override the data root with `FEDERAL_PER_DIEM_DATA_DIR` or `--data-dir`.

Other configuration variables:

- `FEDERAL_PER_DIEM_TIMEOUT`
- `FEDERAL_PER_DIEM_USER_AGENT`
- `FEDERAL_PER_DIEM_GSA_ZIP_URL`
- `FEDERAL_PER_DIEM_GSA_RATES_URL`
- `FEDERAL_PER_DIEM_DOD_ASCII_URL`
- `FEDERAL_PER_DIEM_CENSUS_PLACE_URL`
- `FEDERAL_PER_DIEM_CENSUS_COUNTY_URL`
- `FEDERAL_PER_DIEM_CENSUS_COUSUB_URL`
- `FEDERAL_PER_DIEM_CENSUS_STATE_BOUNDARY_URL`
- `FEDERAL_PER_DIEM_CENSUS_ZCTA_BOUNDARY_URL`
- `FEDERAL_PER_DIEM_MAP_TOLERANCE`
- `FEDERAL_PER_DIEM_MAP_DECIMALS`

URL values may contain the documented `{fiscal_year}` or `{calendar_year}` placeholders. This keeps source locations out of business logic.

`FEDERAL_PER_DIEM_MAP_TOLERANCE` is the Douglas-Peucker tolerance in decimal degrees used when drawing the map, defaulting to `0.001` (about 111 m). It changes only the drawn polygons, never how a click resolves.

## Validation and tests

Validate the production database:

```bash
python scripts/validate_database.py
```

Run the test suite:

```bash
python -m pytest -q
python -m pytest --cov=federal_per_diem --cov-report=term-missing
```

Tests cover fiscal-year boundaries, leading-zero and ZIP+4 normalization, malformed ZIPs, GSA and DTMO parsers, seasonal and mid-month rates, standard CONUS, ambiguous ZIP handling, Alaska/Hawaii, territory locality resolution and its refusal to infer military installations, year-wrapping territory seasons, 75% first/last-day M&IE, month and fiscal-year trip crossings, explicit mileage, SQLite integrity, and retention of a known-good database after failed validation.

Map and dashboard tests build byte-valid synthetic shapefiles rather than mocks, and cover the shapefile and dBASE readers, corrupt-header and truncation rejection, ring simplification, RFC 7946 ring orientation with holes and multipolygons, largest-area ZCTA-to-state assignment, point-in-polygon including points inside holes, nearest-area fallback, and the dashboard command builders. The builder tests assert that malformed ZIPs, dates, traveler counts, mileage values, fiscal years, modes, and state codes are rejected before any argument vector is constructed.

## Limitations and audit notes

- A ZIP code can intersect more than one GSA locality. The FY2026 workbook contains 1,839 such ZIPs. Because ZIP alone is insufficient in that case, `get_per_diem` raises `AmbiguousRateError` and lists the official candidate localities instead of guessing. Use the traveler's exact duty locality to resolve it.
- Census ZCTAs approximate USPS delivery ZIPs and omit some unique or PO-box-only ZIPs. Non-CONUS coverage therefore applies wherever the official 2020 ZCTA source supplies a mapping.
- Guam, the U.S. Virgin Islands, American Samoa, and the Northern Mariana Islands have no polygon in the Census cartographic boundary file, which covers only the 50 states, the District of Columbia, and Puerto Rico. Their 17 ZIPs are fully queryable by typing the ZIP but cannot be reached by clicking the map. Puerto Rico is drawn and clickable.
- Foreign per diem rates are out of scope. They are keyed by country and locality rather than ZIP, so they do not fit this project's ZIP-addressed lookup, and the Department of State publishes them separately.
- DTMO archives are calendar-year, monthly publications; a federal fiscal year needs the previous and current calendar-year archives. For a not-yet-published month, the latest available DTMO snapshot is carried forward and its published seasonal range is applied. Refresh after DTMO's monthly publication to incorporate revisions.
- DTMO installation names are not inferred from surrounding geography. Civilian ZCTAs map to named civil localities, island rates, or the official `[OTHER]` row.
- The dashboard map draws Census ZCTAs, which do not cover every USPS ZIP. Of the 40,768 distinct ZIPs in the FY2026 database, 33,639 have a boundary to click; the rest, including unique ZIPs such as 20500, are reachable only by typing them. Clicking the White House returns the enclosing ZCTA 20006, which is the correct ZCTA answer, not a lookup failure.
- The GSA FY2026 ZIP workbook has 13 blank state cells. The parser fills locality rows only from a unique state for that destination ID and fills standard rows only from a uniquely agreed, most-specific ZIP prefix in the same workbook. Any ambiguous blank fails the refresh.

## Updating a parser

When an agency changes a schema:

1. Keep the last known-good processed files in place.
2. Download the changed source into a separate raw FY directory.
3. Update the relevant adapter's required-column or field checks; do not weaken validation globally.
4. Add a small source-shaped fixture reproducing the change.
5. Run adapter-only validation, then full `--validate-only`.
6. Inspect record counts, coverage warnings, sample ZIPs, source hashes, and the temporary database before running a normal refresh.

Parser versions are stored with source metadata so any generated rate can be traced to the code and raw file that produced it.
