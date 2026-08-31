"""
Unit tests for header parsing and timestamp conversion.
Run: python test_parse.py
"""

from __future__ import annotations

import os

os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost")

from main import UNDISCOUNTED_RECORD_TYPES, _date_to_ns, _parse_headers, fetch_undiscounted_costs


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
    stub = _StubCE(["111111111111"], [{
        "TimePeriod": {"Start": "2026-08-30"},
        "Groups": [{"Keys": ["Amazon EC2", "us-east-1"],
                    "Metrics": {"UnblendedCost": {"Amount": "12.5"}}}],
    }])
    out = fetch_undiscounted_costs(stub)

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


def test_fetch_undiscounted_costs_skips_zero_amounts() -> None:
    stub = _StubCE([""], [{
        "TimePeriod": {"Start": "2026-08-30"},
        "Groups": [{"Keys": ["Amazon S3", "us-east-1"],
                    "Metrics": {"UnblendedCost": {"Amount": "0"}}}],
    }])
    out = fetch_undiscounted_costs(stub)
    assert out == {}


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
