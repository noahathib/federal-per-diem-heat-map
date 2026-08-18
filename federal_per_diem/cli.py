"""Command-line entry points."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Sequence

from .config import Settings
from .estimator import estimate_trip
from .exceptions import PerDiemError
from .lookup import explain_rate, get_per_diem
from .pipeline import refresh_rates
from .validation import validate_database


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def refresh_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh federal per diem data")
    parser.add_argument("--fiscal-year", type=int, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gsa-only", action="store_true")
    mode.add_argument("--dod-only", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    settings = Settings.from_env(data_dir=args.data_dir)
    try:
        result = refresh_rates(
            args.fiscal_year,
            force=args.force,
            validate_only=args.validate_only,
            gsa_only=args.gsa_only,
            dod_only=args.dod_only,
            settings=settings,
        )
    except (PerDiemError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    print(f"Fiscal year:       {result.fiscal_year}")
    print(f"Records validated: {result.record_count:,}")
    print(f"Sources tracked:   {result.source_count}")
    print(f"Validation:        {'PASS' if result.validation.is_valid else 'FAIL'}")
    print(f"Promoted:          {'yes' if result.promoted else 'no'}")
    if result.database_path:
        print(f"SQLite:            {result.database_path}")
        print(f"CSV:               {result.csv_path}")
        print(f"Excel:             {result.excel_path}")
    for warning in result.validation.warnings:
        print(f"WARNING [{warning.code}] {warning.message}")
    return 0


def query_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query a federal per diem rate")
    parser.add_argument("--zip", dest="zip_code", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args(argv)
    try:
        rate = get_per_diem(args.zip_code, args.date, database_path=args.database)
    except (PerDiemError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        payload = rate.to_dict()
        if args.explain:
            payload["explanation"] = explain_rate(
                args.zip_code, args.date, database_path=args.database
            )
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"ZIP:                 {rate.zip_code}")
        print(f"State:               {rate.state}")
        print(f"Destination:         {rate.locality}")
        print(f"Travel date:         {rate.travel_date.isoformat()}")
        print(f"Fiscal year:         {rate.fiscal_year}")
        print(f"Lodging allowance:   ${rate.lodging_rate:.2f}")
        print(f"M&IE allowance:      ${rate.mie_rate:.2f}")
        print(f"First/last day M&IE: ${rate.first_last_day_mie:.2f}")
        print(f"Source:              {rate.source_agency}")
        if args.explain:
            print()
            print(explain_rate(args.zip_code, args.date, database_path=args.database))
    return 0


def estimate_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estimate a federal per diem trip")
    parser.add_argument("--zip", dest="zip_code", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--travelers", type=int, default=1)
    parser.add_argument("--mileage")
    parser.add_argument("--mileage-rate")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        estimate = estimate_trip(
            args.zip_code,
            args.start_date,
            args.end_date,
            travelers=args.travelers,
            mileage=args.mileage,
            mileage_rate=args.mileage_rate,
            database_path=args.database,
        )
    except (PerDiemError, ValueError) as exc:
        parser.error(str(exc))
    data = estimate.to_dict()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"Travel days:       {estimate.travel_days}")
        print(f"Lodging nights:    {estimate.lodging_nights}")
        print(f"Lodging allowance: ${estimate.lodging_allowance:.2f}")
        print(f"Total M&IE:        ${estimate.total_mie:.2f}")
        print(f"Per-person total:  ${estimate.per_person_total:.2f}")
        print(f"Group total:       ${estimate.group_total:.2f}")
    return 0


def build_map_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build dashboard map layers from Census boundary files"
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument(
        "--state",
        action="append",
        dest="states",
        help="Rebuild only these states; repeatable. Omit to build every state.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    settings = Settings.from_env(data_dir=args.data_dir)
    from .geo_builder import build_map_data

    try:
        result = build_map_data(
            settings=settings,
            database_path=args.database,
            force_download=args.force_download,
            states=args.states,
        )
    except (PerDiemError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    print(f"Map directory:     {result.geo_dir}")
    print(f"ZCTAs in source:   {result.zcta_count:,}")
    print(f"Mapped to a state: {result.mapped_zcta_count:,}")
    print(f"Unmapped ZCTAs:    {result.unmapped_zcta_count:,}")
    print(f"State layers:      {result.state_count}")
    print(f"ZIPs in database:  {result.database_zip_count:,}")
    print(f"Drawn with a rate: {result.covered_zip_count:,}")
    print(f"Source vertices:   {result.source_points:,}")
    print(f"Drawn vertices:    {result.written_points:,}")
    return 0


def dashboard_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the local per diem dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--open", action="store_true", help="Open a browser window")
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="Store the password required by network callers, then exit",
    )
    parser.add_argument(
        "--no-password",
        action="store_true",
        help="Serve the network without a password (not recommended)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    logger = logging.getLogger(__name__)

    if args.set_password:
        return _set_dashboard_password()

    from .auth import PasswordGate, credentials_path, is_loopback, load_credential
    from .dashboard import serve

    # A loopback listener is only reachable from this machine, so it stays open.
    # Any other bind is on the network and must be protected, or refuse to run:
    # coming up unprotected because a file was missing is not an acceptable
    # default when /api/run starts subprocesses.
    gate = None
    if not is_loopback(args.host):
        try:
            credential = load_credential()
        except ValueError as exc:
            logger.error("Cannot read the dashboard password: %s", exc)
            return 1
        if credential is None:
            if not args.no_password:
                logger.error(
                    "Refusing to serve %s without a password. Set one with "
                    "'federal-per-diem-dashboard --set-password' (stored in %s), "
                    "or pass --no-password to serve the network anyway.",
                    args.host,
                    credentials_path(),
                )
                return 1
            logger.warning(
                "Serving %s with no password. Anyone on this network can use it.",
                args.host,
            )
        else:
            gate = PasswordGate(credential)

    try:
        serve(
            host=args.host,
            port=args.port,
            settings=Settings.from_env(data_dir=args.data_dir),
            open_browser=args.open,
            gate=gate,
        )
    except OSError as exc:
        logger.error("Cannot start the dashboard: %s", exc)
        return 1
    return 0


def _set_dashboard_password() -> int:
    """Prompt for the dashboard password and store it as a salted digest."""

    import getpass

    from .auth import save_password

    try:
        password = getpass.getpass("New dashboard password: ")
        confirmation = getpass.getpass("Repeat the password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    if password != confirmation:
        print("Those passwords do not match. Nothing was changed.")
        return 1
    try:
        target = save_password(password)
    except (OSError, ValueError) as exc:
        print(f"Could not save the password: {exc}")
        return 1
    print(f"Saved to {target} (readable only by you).")
    print("Network callers now need this password. Loopback stays open.")
    return 0


def validate_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the local SQLite database")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args(argv)
    path = args.database or Settings.from_env().database_path
    report = validate_database(path)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.is_valid else 1

