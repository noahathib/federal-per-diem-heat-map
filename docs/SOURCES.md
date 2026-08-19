# Official source investigation

Research completed 2026-08-17. Production ingestion is limited to authoritative `.gov` and `.mil` files.

## GSA CONUS

Discovery page:

`https://www.gsa.gov/travel/plan-a-trip/per-diem-rates/per-diem-files`

FY file templates observed:

```text
https://www.gsa.gov/system/files/FY{fiscal_year}_ZipCodeFile.xlsx
https://www.gsa.gov/system/files/FY{fiscal_year}_PerDiemMasterRatesFile.xlsx
```

FY2026 developer workbook schema:

```text
DestinationID, Name, County, LocationDefined, State, Zip, FiscalYear,
Oct, Nov, Dec, Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Meals
```

Important observations:

- It is already ZIP-level and preserves all twelve lodging months plus M&IE.
- `DestinationID == 0` and `Name == Standard Rate` identify explicit standard CONUS mappings.
- ZIP values must be read as strings. Leading-zero ZIPs are present.
- Some ZIPs appear under multiple destination IDs because delivery geography crosses per diem locality boundaries.
- Thirteen FY2026 rows have an empty `State`; the parser permits only unambiguous inference from other rows in that same workbook.
- The master workbook is human-oriented and seasonal-row-oriented. It is downloaded and schema-validated as an independent cross-check; the ZIP developer workbook drives normalized GSA rows.

API documentation:

`https://open.gsa.gov/api/perdiem/`

The documented API has endpoints for city/state/year, state/year, ZIP/year, all CONUS lodging, M&IE breakdown, and ZIP-to-destination-ID mappings. It requires an api.data.gov key and has a default limit of 1,000 requests per hour. The downloadable workbook is preferred for full ingestion because it is one versioned file, needs no key, avoids per-ZIP calls, and retains the same DID/month model.

Fiscal years run October 1 through September 30. GSA normally publishes the coming FY in mid-August.

## DTMO non-foreign OCONUS

Official download form:

`https://www.travel.dod.mil/Travel-Transportation-Rates/Per-Diem/Per-Diem-Rate-Lookup/`

The official form constructs this structured archive URL:

```text
https://www.travel.dod.mil/Portals/119/Documents/Allowances/Per_Diem/
OCONUS/ASCII/OCONUS-ASCII-{calendar_year}.zip
```

Each archive contains monthly snapshots such as:

```text
08-01-26oconus.txt
08-01-26oconusnm.txt
```

The normal `oconus.txt` file is semicolon-delimited. Fields inspected:

```text
state_or_country
locality
season_begin (MM/DD)
season_end (MM/DD)
lodging
local_meal_rate
proportional_meal_rate
local_incidental_rate
footnote_number
footnote_text/reserved
maximum_per_diem
rate_effective_date
```

M&IE is `local_meal_rate + local_incidental_rate`; the maximum column confirms lodging plus those two values. The parser excludes `oconusnm.txt`, rejects malformed field counts or currency, and filters to the seven non-foreign OCONUS areas that have USPS ZIP codes:

```text
ALASKA, HAWAII, AMERICAN SAMOA, GUAM,
NORTHERN MARIANA ISLANDS, PUERTO RICO, VIRGIN ISLANDS (U.S.)
```

The same archive also publishes every foreign country: 240 areas and 1,610
localities in the August 2026 snapshot. Foreign localities are deliberately not
ingested. They have no ZIP code and no Census geography, so they cannot be keyed
into this project's ZIP-addressed model, and the authoritative publisher for
them is the Department of State, not DTMO. GSA's per diem page states the split
directly: DoD sets Alaska, Hawaii, and the territories; State sets foreign
countries.

```text
https://www.gsa.gov/travel/plan-book/per-diem-rates
https://allowances.state.gov/content.asp?content_id=184&menu_id=78
```

DTMO publishes `MIDWAY ISLANDS` and `WAKE ISLAND` rates as well. Neither has a
Census ZCTA, so neither can be reached by a ZIP lookup and neither is ingested.

Locality sets for the seven ingested areas were confirmed stable across all
twelve CY2025 snapshots and all eight published CY2026 snapshots. Only Guam
changed, gaining the `CAMP BLAZ` installation in April 2025.

DTMO uses calendar-year monthly publications, not GSA fiscal-year files. Building FY2026 therefore requires CY2025 for October–December 2025 and CY2026 for January–September 2026. The matching monthly snapshot is selected. When the target month has not yet been published, the latest prior snapshot is applied to that date's published seasonal band.

Some DTMO seasons change mid-month (for example, 04/16). The normalized model stores inclusive `effective_start` and `effective_end`, allowing more than one interval in a month instead of collapsing the rates.

DTMO publishes no ZIP field in this ASCII schema.

## Census ZCTA relationships

Discovery and documentation page:

`https://www.census.gov/geographies/reference-files/2020/geo/relationship-files.html`

Files:

```text
https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/
tab20_zcta520_place20_natl.txt

https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/
tab20_zcta520_county20_natl.txt

https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/
tab20_zcta520_cousub20_natl.txt
```

These are pipe-delimited national relationship files with land/water intersection areas. For each ZCTA in an ingested area, the adapter selects the place, county, and county subdivision with the greatest intersecting area. State FIPS codes `02`, `15`, `60`, `66`, `69`, `72`, and `78` are retained; everything else, CONUS included, is excluded because the GSA workbook prices it.

Observed ZCTA counts: Alaska 245, Hawaii 97, Puerto Rico 132, Guam 7, U.S. Virgin Islands 6, Northern Mariana Islands 3, American Samoa 1.

Resolution policy. Each area is resolved by whichever published Census attribute actually decides the DTMO locality, never by proximity:

- Alaska: exact normalized civil place name, a documented rename/composite alias (for example Utqiagvik → Barrow or Kenai/Soldotna → Kenai-Soldotna), otherwise DTMO `[OTHER]`.
- Hawaii: exact Honolulu/Kapolei/Lihue/Hilo handling first, then official island locality based on county and Maui County subdivision, otherwise DTMO `[OTHER]`.
- Puerto Rico: **municipio**, the Census county equivalent. DTMO's Puerto Rico localities are named for municipios, whereas a Census "place" in Puerto Rico is a *zona urbana* or *comunidad* sitting inside one, so the place is too fine-grained to decide the rate. Thirteen of the seventeen published localities match a municipio name exactly; the other four are installations (see below). 47 of the 132 ZCTAs resolve to a named locality and the remaining 85 take DTMO's published `[OTHER]` row.
- U.S. Virgin Islands: county GEOID. The Census county equivalents *are* the islands DTMO prices, so this is an exact key rather than a name match: `78010` → `ST. CROIX`, `78020` → `ST. JOHN`, `78030` → `ST. THOMAS`.
- Northern Mariana Islands: county GEOID, on the same basis: `69100` → `ROTA`, `69110` → `SAIPAN`, `69120` → `TINIAN`.
- Guam: all seven ZCTAs lie in the single county equivalent `66010`, and DTMO publishes one territory-wide civilian locality, `GUAM (INCL ALL MIL INSTAL)`. `TAMUNING` and the three installations carry an identical rate, so naming the territory-wide locality is both correct and avoids inferring a narrower one from ZIP geography.
- American Samoa: the single ZCTA `96799` spans the whole territory and cannot be narrowed to `PAGO PAGO`, so it resolves to the territory-wide `AMERICAN SAMOA` locality. Both carry an identical rate.
- Military installations are never inferred from a nearby civilian ZCTA. `federal_per_diem/normalizer.py` names the installation-only localities explicitly and refuses to return one, and a test asserts that no resolution table contains one. The practical consequence in Puerto Rico is that Guaynabo ZCTAs take `[OTHER]` rather than the `FT. BUCHANAN [INCL GSA SVC CTR, GUAYNABO]` rate, which applies at the installation and the named GSA service center, not across the municipio.

Two localities keep a bracketed installation in their published name while still being the civil municipality: `FAJARDO [INCL ROOSEVELT RDS NAVSTAT]` and `SAN JUAN & NAV RES STA`. Matching their civil names is exact, not inferred; the bracket records that the installation falls inside the civil locality.

Name folding. Census publishes Spanish and Hawaiian names with diacritics and the Hawaiian okina, while DTMO publishes plain ASCII. `utils.fold_name` decomposes to NFKD, drops combining marks, deletes intra-word apostrophes and okina, and reduces the rest to single-spaced uppercase, so `Bayamón` folds to `BAYAMON` and `Līhuʻe` to `LIHUE`.

Snapshot interaction. The set of localities a ZCTA may resolve to is read from the newest snapshot in the downloaded archives, while the rate for a given day is read from the snapshot published for that day's month. For FY2026 those are the October, November, and December 2025 snapshots and the January through August 2026 snapshots, with September 2026 falling back to August because it is not yet published. Every locality the resolver selects was confirmed present in every one of those snapshots, and every locality's published seasons were confirmed to cover all 365 days exactly once, with no gap and no overlap, in all twenty snapshots across both archives. If a future publication removes a locality mid-year, an area with a catch-all silently falls back to `[OTHER]` and an area without one raises, failing the refresh rather than emitting a wrong rate.

Areas without a catch-all. Alaska, Hawaii, and Puerto Rico publish an `[OTHER]` row, so an unmatched ZCTA there has a published fallback. Guam, the U.S. Virgin Islands, American Samoa, and the Northern Mariana Islands publish none. Every one of their 17 ZCTAs resolves exactly today; if that ever stops being true, the normalizer raises and the refresh fails with the offending ZIPs listed, retaining the previous known-good outputs. The archive's global `ALL PLACES NOT LISTED` row is **not** used as a substitute, because applying it to a specific U.S. territory ZIP would be a guess rather than a published mapping.

ZCTAs are Census statistical approximations of ZIP delivery areas. They are the most appropriate freely downloadable federal geographic source inspected, but do not include every unique or PO-box-only USPS ZIP.

## Census cartographic boundaries (dashboard map)

Discovery page:

`https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html`

Files:

```text
https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip
https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip
https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_500k.zip
https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_cousub_500k.zip
https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_place_500k.zip
```

ZCTAs are published only in the 2020 vintage. The later vintages of the
cartographic boundary series (2021 onward) omit ZCTAs, urban areas, PUMAs, and
voting districts, and the Census discovery page directs users back to the 2020
tab for them. GENZ2020 is therefore the current ZCTA boundary release, while
the administrative layers use the current 2025 annual release.

Inspected structure, confirmed against the downloaded files:

```text
cb_2020_us_zcta520_500k: 33,791 polygon records, 38,288 rings, 5,954,485
                         vertices; fields ZCTA5CE20, AFFGEOID20, GEOID20,
                         NAME20, LSAD20, ALAND20, AWATER20
cb_2025_us_state_500k:   56 records; fields STATEFP, STATENS, GEOIDFQ, GEOID,
                         STUSPS, NAME, LSAD, ALAND, AWATER
cb_2025_us_county_500k:  3,235 records; includes GEOID, NAME, NAMELSAD, STUSPS,
                         STATE_NAME, and LSAD
cb_2025_us_cousub_500k:  36,360 records; includes county GEOID parts,
                         NAMELSAD, NAMELSADCO, and LSAD
cb_2025_us_place_500k:   32,629 records; includes GEOID, NAMELSAD, STUSPS,
                         STATE_NAME, and LSAD
```

The 33,791 ZCTA records match, exactly, the 33,791 distinct
`GEOID_ZCTA5_20` values in the ZCTA-to-county relationship file already used by
the rate pipeline. That agreement between two independently published Census
products is the cross-check that the boundary file was parsed correctly.

All five files are ESRI shapefiles. `federal_per_diem/shapefile_reader.py` reads the
polygon subset directly, following the ESRI Shapefile Technical Description
(ESRI White Paper, July 1998) for the `.shp`/`.shx` layout and the dBASE III
table format for the `.dbf`. Every structural field is validated on read: file
code 9994, version 1000, shape type 5, the declared file length against the
actual byte count, per-record numbering, the dBASE header terminator position,
and the field widths against the declared record length. No third-party GIS
dependency is introduced.

The `.prj` files declare:

```text
GCS_North_American_1983 (NAD83), GRS 1980 spheroid, degrees
```

Coordinates are already geographic longitude/latitude, so no re-projection is
performed. NAD83 and WGS84 differ by roughly one to two metres in the
contiguous states; that offset is far below the resolution of a 1:500,000
cartographic boundary file and is not corrected.

The build generates these products under `data/processed/geo`:

- `states.geojson` and simplified state-split ZCTA, county, and municipal
  GeoJSON, using Douglas-Peucker at 0.001
  degrees (about 111 m). A 1:500,000 file is already generalized at roughly
  250 m, so this removes redundant vertices rather than introducing a new
  survey-grade boundary claim. The 2026-08-19 build writes 64,706 municipal
  features after removing coextensive place/county-subdivision duplicates.
- `localities/<ST>.geojson`, containing 319 county polygon parts for GSA
  destinations whose published `LocationDefined` text exactly matches one or
  more complete counties.
- `index.npz`, a ZIP-to-record table with bounding boxes.
- `manifest.json`, with source URLs, archive hashes, download timestamps,
  vintages, feature counts, coordinate precision, simplification tolerance,
  and the locality normalization policy.

The local dashboard's `/api/locate` endpoint still resolves a point against the
unmodified ZCTA shapefile through `index.npz`. The static geography explorer
has no server, so it builds a small spatial grid for the selected state's
simplified layers and runs point-in-polygon only against candidate features in
that grid. It never loads or scans national municipal geometry in the browser.

ZCTAs are national and do not nest inside states. A ZCTA is grouped under the
state holding its largest intersecting area, using the same
largest-part policy the rate pipeline already applies to Census parts. This
affects only which map layer draws the ZCTA.

The 2025 state file has 56 entities, including American Samoa, Guam, the
Northern Mariana Islands, and the U.S. Virgin Islands. Its FIPS-to-USPS table
now assigns all 33,791 ZCTAs to a state or territory, including the seventeen
territory ZCTAs that the former 52-entity 2020 state file left ungrouped.

Municipal normalization. The cartographic DBFs publish `NAMELSAD` and `LSAD`
but not the fuller TIGER/Line `CLASSFP`/`FUNCSTAT` fields. The explorer
therefore preserves the Census area description in the name—`Falls Township`,
`Hatboro Borough`, `Levittown CDP`, or a statistical subdivision such as a
CCD—plus `sourceType` (`place` or `county_subdivision`). It does not label every
county subdivision as a functioning township. When a place and county
subdivision are coextensive with the same name and land/water area, the county
subdivision representation is retained once; genuinely overlapping systems
remain separate and are disclosed by point or ZIP selection.

GSA locality boundaries. `LocationDefined` is authoritative text, not a ready
polygon. A destination receives a boundary only when its definition tokens
exactly equal the complete Census counties named in the workbook. Multi-county
areas retain all county parts under one destination ID. Definitions containing
an exclusion, a city limit, an installation, or other unmatched prose are
omitted. For example, Bucks receives a complete-county outline while
`Dauphin County excluding Hershey` does not. ZCTAs with multiple destination
IDs remain ambiguous and are never dissolved into a fabricated locality.

ZCTAs approximate USPS delivery areas and omit unique or PO-box-only ZIPs. ZIP
20500, the White House, has no ZCTA: clicking that location returns the
enclosing ZCTA 20006, while 20500 itself remains queryable by typing it.

## First and last travel day

DTMO's per diem policy page states that departure and return days receive 75 percent of the applicable M&IE. GSA's M&IE API also publishes `FirstLastDay`. Normalized records calculate `round(mie * 0.75, 2)` with `Decimal`; a source-published value can be preserved if a future adapter supplies one.

## Configurability

All templates and base files live in `federal_per_diem/config.py` and can be overridden with environment variables. Parsers receive local paths and source metadata; no parser contains download behavior or a current-year URL.
