"""Parser for DTMO's official semicolon-delimited OCONUS ASCII archives."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .exceptions import DataValidationError, SchemaChangeError
from .models import SourceMetadata
from .utils import money


PARSER_VERSION = "dtmo-ascii-1.1"
MEMBER_PATTERN = re.compile(r"^(\d{2})-(\d{2})-(\d{2})oconus\.txt$", re.IGNORECASE)

# The non-foreign OCONUS areas this project ingests, spelled exactly as DTMO
# publishes them in the first ASCII field. The same archive also carries every
# foreign country, which is out of scope: foreign localities have no ZIP code,
# so they cannot be keyed into this project's ZIP-addressed model.
DEFAULT_AREAS: tuple[str, ...] = (
    "ALASKA",
    "HAWAII",
    "AMERICAN SAMOA",
    "GUAM",
    "NORTHERN MARIANA ISLANDS",
    "PUERTO RICO",
    "VIRGIN ISLANDS (U.S.)",
)


@dataclass(frozen=True, slots=True)
class DODSeasonRate:
    """A seasonal locality rate as published in one monthly DTMO snapshot."""

    state: str
    locality: str
    season_begin: str
    season_end: str
    lodging_rate: Decimal
    local_meal_rate: Decimal
    incidental_rate: Decimal
    footnote: str | None
    maximum_per_diem: Decimal
    rate_effective_date: date
    publication_date: date
    source_file: str
    source_url: str
    source_retrieved_at: datetime
    source_sha256: str

    @property
    def mie_rate(self) -> Decimal:
        return self.local_meal_rate + self.incidental_rate

    def applies_on(self, day: date) -> bool:
        """Return whether the MM/DD seasonal range includes *day*."""

        start_month, start_day = (int(part) for part in self.season_begin.split("/"))
        end_month, end_day = (int(part) for part in self.season_end.split("/"))
        marker = (day.month, day.day)
        start = (start_month, start_day)
        end = (end_month, end_day)
        return start <= marker <= end if start <= end else marker >= start or marker <= end


def parse_dod_file(
    path: Path | str,
    metadata: SourceMetadata,
    *,
    states: tuple[str, ...] = DEFAULT_AREAS,
) -> tuple[list[DODSeasonRate], SourceMetadata]:
    """Parse non-foreign OCONUS rates from a DTMO annual ASCII ZIP archive."""

    source_path = Path(path)
    output: list[DODSeasonRate] = []
    wanted = set(states)
    try:
        archive = zipfile.ZipFile(source_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise DataValidationError(f"Invalid DTMO archive {source_path.name}: {exc}") from exc
    with archive:
        members = [name for name in archive.namelist() if MEMBER_PATTERN.match(Path(name).name)]
        if not members:
            raise SchemaChangeError(
                f"DTMO archive {source_path.name} has no monthly oconus.txt members"
            )
        for member in sorted(members):
            match = MEMBER_PATTERN.match(Path(member).name)
            assert match is not None
            month, day, year2 = (int(part) for part in match.groups())
            publication_date = date(2000 + year2, month, day)
            text_stream = io.TextIOWrapper(
                archive.open(member), encoding="utf-8-sig", errors="replace", newline=""
            )
            with text_stream:
                reader = csv.reader(text_stream, delimiter=";")
                for line_number, fields in enumerate(reader, start=1):
                    if not fields or not fields[0].strip():
                        continue
                    if len(fields) < 12:
                        raise SchemaChangeError(
                            f"DTMO {member}:{line_number} has {len(fields)} fields; expected 12+"
                        )
                    state = fields[0].strip().upper()
                    if state not in wanted:
                        continue
                    try:
                        lodging = money(fields[4])
                        local_meal = money(fields[5])
                        incidentals = money(fields[7])
                        maximum = money(fields[10])
                        assert None not in (lodging, local_meal, incidentals, maximum)
                        output.append(
                            DODSeasonRate(
                                state=state,
                                locality=fields[1].strip().upper(),
                                season_begin=fields[2].strip(),
                                season_end=fields[3].strip(),
                                lodging_rate=lodging,
                                local_meal_rate=local_meal,
                                incidental_rate=incidentals,
                                footnote=fields[8].strip() or None,
                                maximum_per_diem=maximum,
                                rate_effective_date=datetime.strptime(
                                    fields[11].strip(), "%m/%d/%Y"
                                ).date(),
                                publication_date=publication_date,
                                source_file=f"{metadata.filename}:{Path(member).name}",
                                source_url=metadata.source_url,
                                source_retrieved_at=metadata.downloaded_at,
                                source_sha256=metadata.sha256,
                            )
                        )
                    except (ValueError, IndexError) as exc:
                        raise DataValidationError(
                            f"Malformed DTMO row {member}:{line_number}: {exc}"
                        ) from exc
    if not output:
        raise DataValidationError(
            f"DTMO archive contains no rows for {', '.join(sorted(wanted))}"
        )
    return output, replace(
        metadata,
        record_count=len(output),
        parser_version=PARSER_VERSION,
        validation_status="parsed",
    )
