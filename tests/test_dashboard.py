from __future__ import annotations

import sys
import time

import pytest

from federal_per_diem.config import PACKAGE_ROOT
from federal_per_diem.dashboard import (
    COMMAND_BUILDERS,
    MANUAL_COMMANDS,
    CommandJob,
    JobRegistry,
    _display,
    build_estimate,
    build_query,
    build_validate,
    run_job,
)
from federal_per_diem.exceptions import InvalidZipCodeError


def tail(argv):
    """Drop the interpreter and script path, leaving the flags."""

    return argv[2:]


# ------------------------------------------------------------------ query

def test_query_builds_the_documented_command():
    argv = build_query({"zip": "19103", "date": "2026-08-17"})
    assert argv[0] == sys.executable
    assert argv[1] == str(PACKAGE_ROOT / "scripts" / "query_rate.py")
    assert tail(argv) == ["--zip", "19103", "--date", "2026-08-17", "--json"]


def test_query_normalizes_zip_plus_four_and_leading_zeros():
    assert "19103" in build_query({"zip": "19103-1234", "date": "2026-08-17"})
    assert "01001" in build_query({"zip": "01001", "date": "2026-08-17"})


def test_query_adds_explain_only_when_requested():
    assert "--explain" in build_query(
        {"zip": "19103", "date": "2026-08-17", "explain": True}
    )
    assert "--explain" not in build_query({"zip": "19103", "date": "2026-08-17"})


@pytest.mark.parametrize(
    "zip_code",
    ["1910", "abcde", "19103; rm -rf /", "--database=/etc/passwd", "", "19103 --json"],
)
def test_query_rejects_anything_that_is_not_a_zip(zip_code):
    with pytest.raises(InvalidZipCodeError):
        build_query({"zip": zip_code, "date": "2026-08-17"})


@pytest.mark.parametrize("value", ["2026-13-01", "tomorrow", "", "2026-08-17; ls"])
def test_query_rejects_malformed_dates(value):
    with pytest.raises(ValueError):
        build_query({"zip": "19103", "date": value})


# --------------------------------------------------------------- estimate

def test_estimate_builds_the_documented_command():
    argv = build_estimate(
        {
            "zip": "19103",
            "startDate": "2026-08-17",
            "endDate": "2026-08-20",
            "travelers": 2,
        }
    )
    assert tail(argv) == [
        "--zip", "19103",
        "--start-date", "2026-08-17",
        "--end-date", "2026-08-20",
        "--travelers", "2",
        "--json",
    ]


def test_estimate_rejects_a_reversed_range():
    with pytest.raises(ValueError, match="precede"):
        build_estimate(
            {"zip": "19103", "startDate": "2026-08-20", "endDate": "2026-08-17"}
        )


@pytest.mark.parametrize("travelers", [0, -1, 1.5, True, "two"])
def test_estimate_rejects_bad_traveler_counts(travelers):
    with pytest.raises(ValueError):
        build_estimate(
            {
                "zip": "19103",
                "startDate": "2026-08-17",
                "endDate": "2026-08-20",
                "travelers": travelers,
            }
        )


def test_estimate_requires_mileage_and_rate_together():
    base = {"zip": "19103", "startDate": "2026-08-17", "endDate": "2026-08-20"}
    with pytest.raises(ValueError, match="together"):
        build_estimate({**base, "mileage": "100"})
    with pytest.raises(ValueError, match="together"):
        build_estimate({**base, "mileageRate": "0.70"})


def test_estimate_passes_validated_mileage_through():
    argv = build_estimate(
        {
            "zip": "19103",
            "startDate": "2026-08-17",
            "endDate": "2026-08-20",
            "mileage": "120",
            "mileageRate": "0.70",
        }
    )
    assert argv[-4:] == ["--mileage", "120", "--mileage-rate", "0.70"]


@pytest.mark.parametrize("value", ["-5", "abc", "1e400", "0.70; ls"])
def test_estimate_rejects_bad_mileage_values(value):
    with pytest.raises(ValueError):
        build_estimate(
            {
                "zip": "19103",
                "startDate": "2026-08-17",
                "endDate": "2026-08-20",
                "mileage": value,
                "mileageRate": "0.70",
            }
        )


def test_validate_takes_no_arguments():
    assert tail(build_validate({})) == []


# --------------------------------------------------------------- display

def test_display_is_copyable_and_project_relative():
    argv = build_query({"zip": "19103", "date": "2026-08-17"})
    assert _display(argv) == (
        "python scripts/query_rate.py --zip 19103 --date 2026-08-17 --json"
    )


def test_display_quotes_arguments_that_need_it():
    assert "'a b'" in _display([sys.executable, "script.py", "a b"])


def test_only_read_only_actions_are_reachable():
    """Refresh and map rebuilds replace published data and stay terminal-only."""

    assert set(COMMAND_BUILDERS) == {"query", "estimate", "validate"}


@pytest.mark.parametrize("action", ["refresh", "build-map", "promote", "download"])
def test_mutating_actions_have_no_builder(action):
    assert action not in COMMAND_BUILDERS


def test_no_reachable_action_invokes_a_writing_script():
    scripts = {
        argv[1].rsplit("/", 1)[-1]
        for argv in (
            build_query({"zip": "19103", "date": "2026-08-17"}),
            build_estimate(
                {"zip": "19103", "startDate": "2026-08-17", "endDate": "2026-08-18"}
            ),
            build_validate({}),
        )
    }
    assert scripts == {"query_rate.py", "estimate_trip.py", "validate_database.py"}
    assert "refresh_rates.py" not in scripts
    assert "build_map_data.py" not in scripts


def test_query_ignores_a_caller_supplied_database_path():
    """A request must not be able to point the CLI at an arbitrary file."""

    argv = build_query(
        {"zip": "19103", "date": "2026-08-17", "database": "/etc/passwd"}
    )
    assert "--database" not in argv
    assert not any("passwd" in argument for argument in argv)


def test_manual_commands_are_advertised_for_the_operator():
    commands = [entry["command"] for entry in MANUAL_COMMANDS]
    assert any("refresh_rates.py" in command for command in commands)
    assert any("build_map_data.py" in command for command in commands)


# ------------------------------------------------------------------- jobs

def make_job(argv, action="query"):
    return CommandJob(id="test", action=action, argv=argv, display=_display(argv))


def test_run_job_captures_stdout_and_exit_status():
    job = make_job([sys.executable, "-c", "print('hello')"])
    run_job(job, timeout=30)
    snapshot = job.snapshot()
    assert snapshot["status"] == "completed"
    assert snapshot["returncode"] == 0
    assert "hello" in snapshot["stdout"]
    assert snapshot["durationMs"] >= 0


def test_run_job_records_a_failure_and_its_stderr():
    job = make_job(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    )
    run_job(job, timeout=30)
    snapshot = job.snapshot()
    assert snapshot["status"] == "failed"
    assert snapshot["returncode"] == 3
    assert "boom" in snapshot["stderr"]


def test_run_job_parses_json_output_when_json_was_requested():
    job = make_job([sys.executable, "-c", "print('{\"a\": 1}')", "--json"])
    run_job(job, timeout=30)
    assert job.parsed == {"a": 1}


def test_run_job_flags_unparsable_json_without_crashing():
    job = make_job([sys.executable, "-c", "print('not json')", "--json"])
    run_job(job, timeout=30)
    assert job.parsed is None
    assert "JSON" in job.error


def test_run_job_kills_a_command_past_its_timeout():
    job = make_job([sys.executable, "-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    run_job(job, timeout=1)
    assert time.monotonic() - started < 20
    assert "timeout" in job.error


def test_run_job_reports_a_command_that_cannot_start():
    job = make_job(["/nonexistent/interpreter", "-c", "pass"])
    run_job(job, timeout=5)
    assert job.status == "failed"
    assert "Could not start" in job.error


def test_job_output_is_bounded():
    job = make_job([sys.executable, "-c", "print('x' * 100)"])
    job.append("stdout", "y" * (4 * 1024 * 1024))
    assert len(job.snapshot()["stdout"]) <= 2 * 1024 * 1024


def test_registry_evicts_the_oldest_jobs():
    registry = JobRegistry(limit=2)
    for index in range(3):
        registry.add(CommandJob(id=str(index), action="query", argv=[], display=""))
    assert registry.get("0") is None
    assert registry.get("2") is not None
