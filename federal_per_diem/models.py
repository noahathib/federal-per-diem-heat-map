"""Typed records shared across ingestion, storage, lookup, and validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal


Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Description of an authoritative file to retrieve."""

    key: str
    agency: str
    dataset_name: str
    fiscal_year: int
    url: str
    filename: str
    expected_extensions: tuple[str, ...]
    expected_content_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Immutable provenance for one raw source file."""

    agency: str
    dataset_name: str
    fiscal_year: int
    source_url: str
    downloaded_at: datetime
    filename: str
    sha256: str
    file_size: int
    record_count: int = 0
    parser_version: str = "1.0"
    validation_status: str = "pending"
    local_path: Path | None = None


@dataclass(frozen=True, slots=True)
class NormalizedRate:
    """Canonical long-format rate interval for a ZIP code."""

    zip_code: str
    state: str
    city: str | None
    county: str | None
    primary_destination: str | None
    locality: str
    destination_id: str
    fiscal_year: int
    month: int
    effective_start: date
    effective_end: date
    lodging_rate: Decimal
    mie_rate: Decimal
    first_last_day_mie: Decimal
    source_agency: str
    source_file: str
    source_url: str
    source_retrieved_at: datetime
    source_sha256: str
    is_standard: bool = False


@dataclass(frozen=True, slots=True)
class PerDiemRate:
    """Explainable result returned by a ZIP/date lookup."""

    zip_code: str
    state: str
    city: str | None
    county: str | None
    locality: str
    primary_destination: str | None
    destination_id: str
    fiscal_year: int
    travel_date: date
    lodging_rate: Decimal
    mie_rate: Decimal
    first_last_day_mie: Decimal
    source_agency: str
    source_file: str
    source_url: str
    source_retrieved_at: datetime
    is_standard: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""

        result = asdict(self)
        for key, value in tuple(result.items()):
            if isinstance(value, Decimal):
                result[key] = f"{value:.2f}"
            elif isinstance(value, (date, datetime)):
                result[key] = value.isoformat()
        return result


@dataclass(frozen=True, slots=True)
class TripEstimate:
    """Per-person and group allowances for a date range."""

    zip_code: str
    start_date: date
    end_date: date
    travelers: int
    travel_days: int
    lodging_nights: int
    lodging_allowance: Decimal
    full_mie_days: int
    first_day_mie: Decimal
    last_day_mie: Decimal
    full_day_mie: Decimal
    total_mie: Decimal
    mileage_allowance: Decimal
    per_person_total: Decimal
    group_total: Decimal
    nightly_lodging: tuple[tuple[date, Decimal], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""

        result = asdict(self)
        result["nightly_lodging"] = [
            {"date": day.isoformat(), "rate": f"{rate:.2f}"}
            for day, rate in self.nightly_lodging
        ]
        for key, value in tuple(result.items()):
            if isinstance(value, Decimal):
                result[key] = f"{value:.2f}"
            elif isinstance(value, date):
                result[key] = value.isoformat()
        return result


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One actionable validation finding."""

    severity: Severity
    code: str
    message: str


@dataclass(slots=True)
class ValidationReport:
    """Aggregate validation result with errors and non-blocking warnings."""

    issues: list[ValidationIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    def add(self, severity: Severity, code: str, message: str) -> None:
        self.issues.append(ValidationIssue(severity, code, message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.is_valid,
            "metrics": self.metrics,
            "issues": [asdict(issue) for issue in self.issues],
        }
