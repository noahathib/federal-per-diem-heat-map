"""Application-specific exception hierarchy."""


class PerDiemError(Exception):
    """Base exception for all package errors."""


class InvalidZipCodeError(PerDiemError, ValueError):
    """Raised when a ZIP or ZIP+4 value cannot be normalized."""


class RateNotFoundError(PerDiemError, LookupError):
    """Raised when the local database has no applicable rate."""


class AmbiguousRateError(PerDiemError, LookupError):
    """Raised when one ZIP intersects multiple published rate localities."""


class DataValidationError(PerDiemError):
    """Raised when incoming or processed data fail validation."""


class SourceDownloadError(PerDiemError):
    """Raised when an authoritative source cannot be downloaded safely."""


class SchemaChangeError(DataValidationError):
    """Raised when a source no longer matches its documented schema."""
