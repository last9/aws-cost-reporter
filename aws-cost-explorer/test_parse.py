"""
Unit tests for header parsing and timestamp conversion.
Run: python test_parse.py
"""

from __future__ import annotations

import os

os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost")

from main import (
    UNDISCOUNTED_RECORD_TYPES,
    _date_to_ns,
    _parse_headers,
    fetch_undiscounted_costs,
)

PERIOD = {"Start": "2026-08-30", "End": "2026-08-31"}


def test_parse_headers_single() -> None:
    result = _parse_headers("Authorization=Basic abc123")
    assert result == {"Authorization": "Basic abc123"}


def test_parse_headers_multiple() -> None:
    result = _parse_headers("Authorization=Basic abc,X-Tenant=acme")
    assert result == {"Authorization": "Basic abc", "X-Tenant": "acme"}


def test_parse_headers_strips_spaces() -> None:
    result = _parse_headers("Authorization = Basic abc , X-Tenant = acme")
    assert result == {"Authorization": "Basic abc", "X-Tenant": "acme"}


def test_parse_headers_empty_string() -> None:
    assert _parse_headers("") == {}


def test_parse_headers_missing_equals() -> None:
    assert _parse_headers("AuthorizationBasicabc") == {}


def test_parse_headers_value_contains_equals() -> None:
    # Base64 can contain '=' padding — must not be split on second '='
    result = _parse_headers("Authorization=Basic dXNlcjpwYXNz==")
    assert result == {"Authorization": "Basic dXNlcjpwYXNz=="}


def test_date_to_ns_noon_utc() -> None:
    ns = int(_date_to_ns("2026-01-15"))
    # 2026-01-15T12:00:00Z = 1768478400 seconds
    assert ns == 1768478400 * 1_000_000_000


class _StubCE:
    """Records every get_cost_and_usage call's Filter so the test can assert
    RECORD_TYPE is a Filter (not a GroupBy dimension) — a real boto3 call
    with RECORD_TYPE as a third GroupBy dimension raises
    "Only two values for GroupBy are allowed", which this filter-based
    approach must avoid. One fixed ResultsByTime per account, returned in
    call order (fetch_undiscounted_costs makes exactly one call per account
    with no pagination in these tests)."""

    def __init__(self, account_ids: list[str], results_by_time: list[dict]) -> None:
        self.account_ids = account_ids
        self.results_by_time = results_by_time
        self.calls: list[dict] = []

    def get_dimension_values(self, **kwargs):
        return {"DimensionValues": [{"Value": acct} for acct in self.account_ids]}

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        return {"ResultsByTime": self.results_by_time}


def test_fetch_undiscounted_costs_filters_by_record_type() -> None:
    stub = _StubCE(
        ["111111111111"],
        [
            {
                "TimePeriod": {"Start": "2026-08-30"},
                "Groups": [
                    {
                        "Keys": ["Amazon EC2", "us-east-1"],
                        "Metrics": {"UnblendedCost": {"Amount": "12.5"}},
                    }
                ],
            }
        ],
    )
    out = fetch_undiscounted_costs(stub, PERIOD)

    assert out == {("2026-08-30", "Amazon EC2", "111111111111", "us-east-1"): 12.5}
    # Every call must filter RECORD_TYPE to exactly UNDISCOUNTED_RECORD_TYPES,
    # and RECORD_TYPE must be a Filter dimension, never a third GroupBy dim
    # (Cost Explorer caps GroupBy at 2).
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert len(call["GroupBy"]) == 2
    assert all(g["Key"] != "RECORD_TYPE" for g in call["GroupBy"])
    record_type_filter = call["Filter"]["And"][0]["Dimensions"]
    assert record_type_filter["Key"] == "RECORD_TYPE"
    assert record_type_filter["Values"] == UNDISCOUNTED_RECORD_TYPES


def test_fetch_costs_respects_capture_offset_days() -> None:
    """The fetch window must end CAPTURE_OFFSET_DAYS ago, not "today" —
    Cost Explorer's daily estimate keeps revising for ~2-3 days after the day
    passes, and a day can never be corrected once written to Last9, so the
    default (3) must actually shift the window, not just exist as an unused
    env var."""
    import importlib
    import os
    from datetime import datetime, timedelta, timezone

    os.environ["CAPTURE_OFFSET_DAYS"] = "5"
    try:
        import main

        importlib.reload(main)

        period = main._billing_period()
        expected_end = datetime.now(tz=timezone.utc).date() - timedelta(days=5)
        assert period["End"] == str(expected_end), (
            f"expected End={expected_end} (today - CAPTURE_OFFSET_DAYS=5), got {period['End']}"
        )
    finally:
        del os.environ["CAPTURE_OFFSET_DAYS"]
        importlib.reload(main)


def test_fetch_undiscounted_costs_skips_zero_amounts() -> None:
    stub = _StubCE(
        [""],
        [
            {
                "TimePeriod": {"Start": "2026-08-30"},
                "Groups": [
                    {
                        "Keys": ["Amazon S3", "us-east-1"],
                        "Metrics": {"UnblendedCost": {"Amount": "0"}},
                    }
                ],
            }
        ],
    )
    out = fetch_undiscounted_costs(stub, PERIOD)
    assert out == {}


def test_poll_uses_one_billing_window_across_utc_midnight() -> None:
    """poll must compute the window once: if the clock rolls past UTC
    midnight between fetch_costs and fetch_undiscounted_costs, both metric
    families must still query the same [start, end) — otherwise undiscounted
    describes a different day than unblended/amortized and, at DAYS_BACK=1,
    silently skips a day."""
    from datetime import datetime, timezone
    from unittest.mock import patch

    import main

    ticks = iter(
        [
            datetime(2026, 9, 15, 23, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 9, 16, 0, 0, 1, tzinfo=timezone.utc),
        ]
    )

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks, datetime(2026, 9, 16, 0, 0, 2, tzinfo=timezone.utc))

    periods: list[dict] = []

    class _CE:
        def get_dimension_values(self, **kwargs):
            periods.append(kwargs["TimePeriod"])
            return {"DimensionValues": [{"Value": "111111111111"}]}

        def get_cost_and_usage(self, **kwargs):
            periods.append(kwargs["TimePeriod"])
            return {"ResultsByTime": []}

    with (
        patch.object(main, "datetime", _Clock),
        patch.object(main, "send_otlp_metrics"),
    ):
        main.poll(_CE())

    windows = {(p["Start"], p["End"]) for p in periods}
    assert len(windows) == 1, f"one poll mixed billing windows: {windows}"


def _run_deploy(use_cf: str) -> list[dict]:
    """Runs deploy.sh with aws/pip3/zip stubbed on PATH; returns every aws
    CLI invocation (plus the parsed --environment file, if any)."""
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = root / "aws.jsonl"
        aws = root / "aws"
        aws.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "entry = {'args': args}\n"
            "if '--environment' in args:\n"
            "    v = args[args.index('--environment') + 1]\n"
            "    entry['environment'] = json.loads("
            "Path(v.removeprefix('file://')).read_text())\n"
            "open(os.environ['STUB_AWS_LOG'], 'a').write(json.dumps(entry) + '\\n')\n"
            "if args[:2] == ['sts', 'get-caller-identity']:\n"
            "    print('111111111111')\n"
            "else:\n"
            "    print('arn:aws:lambda:us-east-1:111111111111:function:stub')\n"
        )
        aws.chmod(0o755)
        for cmd in ("pip3", "zip"):
            (root / cmd).write_text("#!/bin/sh\nexit 0\n")
            (root / cmd).chmod(0o755)
        env = dict(
            os.environ,
            PATH=str(root) + os.pathsep + os.environ["PATH"],
            USE_CF=use_cf,
            CF_S3_BUCKET="stub-bucket",
            OTEL_EXPORTER_OTLP_ENDPOINT="https://collector.example.test",
            OTEL_EXPORTER_OTLP_HEADERS="x-test=stub",
            CAPTURE_OFFSET_DAYS="7",
            DAYS_BACK="2",
            SMOKE_TEST="0",
            STUB_AWS_LOG=str(log),
        )
        subprocess.run(
            ["bash", "deploy.sh"],
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
            cwd=Path(__file__).parent,
        )
        return [json.loads(line) for line in log.read_text().splitlines()]


def test_deploy_cloudformation_passes_capture_offset() -> None:
    calls = _run_deploy("1")
    args = next(
        c["args"] for c in calls if c["args"][:2] == ["cloudformation", "deploy"]
    )
    assert "DaysBack=2" in args  # existing override: positive control
    assert "CaptureOffsetDays=7" in args, "CloudFormation drops CAPTURE_OFFSET_DAYS"


def test_deploy_direct_lambda_passes_capture_offset() -> None:
    calls = _run_deploy("0")
    env = next(c["environment"]["Variables"] for c in calls if "environment" in c)
    assert env["CAPTURE_OFFSET_DAYS"] == "7"
    assert env["DAYS_BACK"] == "2"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  ✗ {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(failed)
