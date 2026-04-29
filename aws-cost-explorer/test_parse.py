"""
Unit tests for header parsing and timestamp conversion.
Run: python test_parse.py
"""

from __future__ import annotations

import os

os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost")

from main import _date_to_ns, _parse_headers  # noqa: E402


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  ✗ {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(failed)
