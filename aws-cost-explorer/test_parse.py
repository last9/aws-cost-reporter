"""
Unit tests for header parsing and timestamp conversion.
Run: python test_parse.py
"""

from __future__ import annotations

import os

os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost")

from unittest.mock import patch  # noqa: E402

import main  # noqa: E402
from main import (  # noqa: E402
    _date_to_ns,
    _list_linked_accounts,
    _parse_headers,
    _sanitize_tag_key,
    _warn_tag_key_collisions,
    fetch_tag_costs,
    send_otlp_metrics,
)


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


def test_sanitize_tag_key_simple() -> None:
    assert _sanitize_tag_key("Project") == "project"


def test_sanitize_tag_key_punctuation() -> None:
    # AWS allows '+ - = . _ : / @' in tag keys; Prom labels do not
    assert _sanitize_tag_key("cost-center") == "cost_center"
    assert _sanitize_tag_key("aws:Project") == "aws_project"
    assert _sanitize_tag_key("env.tier") == "env_tier"


def test_sanitize_tag_key_collapses_leading_trailing() -> None:
    assert _sanitize_tag_key("__Project__") == "project"


def test_sanitize_tag_key_all_invalid() -> None:
    assert _sanitize_tag_key("@@@") == "unknown"


def test_sanitize_tag_key_collision_pinned() -> None:
    # Pin the lossy mapping: '.' and '-' both collapse to '_'.
    # Documented tradeoff — collision detection lives in
    # _warn_tag_key_collisions.
    assert _sanitize_tag_key("a.b") == _sanitize_tag_key("a-b") == "a_b"


# ── _warn_tag_key_collisions ──────────────────────────────────────────────────


class _LogCapture:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, msg: str, *args: object) -> None:
        self.warnings.append(msg % args if args else msg)


def test_warn_collision_detects_dot_vs_dash() -> None:
    cap = _LogCapture()
    with patch.object(main, "log", cap):
        _warn_tag_key_collisions(["cost.center", "cost-center", "Cost-Center"])
    assert any("cost_center" in w for w in cap.warnings)


def test_warn_collision_silent_when_unique() -> None:
    cap = _LogCapture()
    with patch.object(main, "log", cap):
        _warn_tag_key_collisions(["Project", "Environment"])
    assert cap.warnings == []


# ── _list_linked_accounts ─────────────────────────────────────────────────────


class _FakeCEAccounts:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def get_dimension_values(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


def test_list_linked_accounts_paginates() -> None:
    ce = _FakeCEAccounts(
        [
            {
                "DimensionValues": [{"Value": "111"}, {"Value": "222"}],
                "NextPageToken": "tok",
            },
            {"DimensionValues": [{"Value": "333"}]},
        ]
    )
    period = {"Start": "2026-01-01", "End": "2026-01-02"}
    assert _list_linked_accounts(ce, period) == ["111", "222", "333"]
    assert len(ce.calls) == 2
    assert ce.calls[1]["NextPageToken"] == "tok"


def test_list_linked_accounts_handles_access_denied() -> None:
    class _Boom:
        def get_dimension_values(self, **_: object) -> dict:
            raise RuntimeError("AccessDeniedException")

    assert _list_linked_accounts(_Boom(), {"Start": "x", "End": "y"}) == [""]


def test_list_linked_accounts_empty_falls_back() -> None:
    ce = _FakeCEAccounts([{"DimensionValues": []}])
    assert _list_linked_accounts(ce, {"Start": "x", "End": "y"}) == [""]


# ── fetch_tag_costs ───────────────────────────────────────────────────────────


def _ce_group(service: str, tag_pair: str, unblended: str, amortized: str) -> dict:
    return {
        "Keys": [service, tag_pair],
        "Metrics": {
            "UnblendedCost": {"Amount": unblended, "Unit": "USD"},
            "AmortizedCost": {"Amount": amortized, "Unit": "USD"},
        },
    }


class _FakeCETagCosts:
    """Records get_cost_and_usage calls and returns scripted responses."""

    def __init__(self, response_by_tag: dict[str, list[dict]]) -> None:
        self.response_by_tag = response_by_tag
        self.calls: list[dict] = []

    def get_dimension_values(self, **_: object) -> dict:
        return {"DimensionValues": [{"Value": "111111111111"}]}

    def get_cost_and_usage(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        tag_key = next(g["Key"] for g in kwargs["GroupBy"] if g["Type"] == "TAG")
        if tag_key == "BadKey":
            raise RuntimeError("ValidationException: tag not active")
        groups = self.response_by_tag.get(tag_key, [])
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-01-15", "End": "2026-01-16"},
                    "Groups": groups,
                }
            ]
        }


def test_fetch_tag_costs_groupby_and_filter() -> None:
    ce = _FakeCETagCosts(
        {
            "Project": [
                _ce_group("AmazonEC2", "Project$alpha", "1.50", "1.40"),
                _ce_group("AmazonS3", "Project$", "0.10", "0.09"),  # untagged
            ]
        }
    )
    rows = fetch_tag_costs(ce, ["Project"])
    assert len(ce.calls) == 1
    call = ce.calls[0]
    assert call["GroupBy"] == [
        {"Type": "DIMENSION", "Key": "SERVICE"},
        {"Type": "TAG", "Key": "Project"},
    ]
    assert call["Filter"] == {
        "Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["111111111111"]}
    }
    assert {r["tag_value"] for r in rows} == {"alpha", "untagged"}
    assert all(r["account_id"] == "111111111111" for r in rows)


def test_fetch_tag_costs_one_bad_key_does_not_kill_run() -> None:
    ce = _FakeCETagCosts(
        {"Project": [_ce_group("AmazonEC2", "Project$alpha", "1.00", "1.00")]}
    )
    rows = fetch_tag_costs(ce, ["BadKey", "Project"])
    # BadKey raises; Project still produces rows.
    assert [r["tag_key"] for r in rows] == ["Project"]


def test_fetch_tag_costs_pagination_concat() -> None:
    class _Paginated:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def get_dimension_values(self, **_: object) -> dict:
            return {"DimensionValues": [{"Value": "111"}]}

        def get_cost_and_usage(self, **kwargs: object) -> dict:
            self.calls.append(kwargs)
            if "NextPageToken" not in kwargs:
                return {
                    "ResultsByTime": [
                        {
                            "TimePeriod": {"Start": "2026-01-15", "End": "2026-01-16"},
                            "Groups": [
                                _ce_group("EC2", "Project$alpha", "1.00", "1.00")
                            ],
                        }
                    ],
                    "NextPageToken": "p2",
                }
            return {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-01-15", "End": "2026-01-16"},
                        "Groups": [_ce_group("S3", "Project$beta", "2.00", "2.00")],
                    }
                ]
            }

    ce = _Paginated()
    rows = fetch_tag_costs(ce, ["Project"])
    assert {r["service"] for r in rows} == {"EC2", "S3"}
    assert len(ce.calls) == 2


# ── send_otlp_metrics tag branch ──────────────────────────────────────────────


def test_send_otlp_metrics_emits_sanitized_tag_attribute() -> None:
    captured: dict = {}

    class _FakeResp:
        status_code = 200
        text = ""

    def _fake_post(url: str, json: dict, headers: dict, timeout: int) -> object:
        captured["payload"] = json
        return _FakeResp()

    rows = [
        {
            "date": "2026-01-15",
            "service": "AmazonEC2",
            "account_id": "111",
            "tag_key": "cost-center",
            "tag_value": "platform",
            "unblended": 1.23,
            "amortized": 1.20,
        }
    ]
    with patch.object(main.requests, "post", _fake_post):
        send_otlp_metrics(rows)

    metrics = captured["payload"]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    dp = metrics[0]["gauge"]["dataPoints"][0]
    keys = {a["key"] for a in dp["attributes"]}
    assert "aws.tag.cost_center" in keys  # dash → underscore
    assert "aws.region" not in keys  # tag rows have no region
    tag_attr = next(a for a in dp["attributes"] if a["key"] == "aws.tag.cost_center")
    assert tag_attr["value"]["stringValue"] == "platform"


def test_send_otlp_metrics_untagged_default() -> None:
    captured: dict = {}

    class _FakeResp:
        status_code = 200
        text = ""

    def _fake_post(url: str, json: dict, **_: object) -> object:
        captured["payload"] = json
        return _FakeResp()

    # Row with tag_key but no tag_value (drift test) — must not KeyError.
    rows = [
        {
            "date": "2026-01-15",
            "service": "AmazonS3",
            "account_id": "111",
            "tag_key": "Project",
            "unblended": 0.50,
            "amortized": 0.50,
        }
    ]
    with patch.object(main.requests, "post", _fake_post):
        send_otlp_metrics(rows)
    dp = captured["payload"]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0][
        "gauge"
    ]["dataPoints"][0]
    tag_attr = next(a for a in dp["attributes"] if a["key"] == "aws.tag.project")
    assert tag_attr["value"]["stringValue"] == "untagged"


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
