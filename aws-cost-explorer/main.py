"""
AWS Cost Explorer → OpenTelemetry Pipeline

Polls the AWS Cost Explorer API and exports cost metrics to Last9.
No CUR setup or S3 bucket required — data is available within minutes.

Metrics exported:
  aws.cost.unblended  (USD) — daily unblended cost per service/account and per service/region
  aws.cost.amortized  (USD) — daily amortized cost (includes RI/SP effective rates)

Deployment modes:
  Lambda  — deploy with deploy.sh; EventBridge triggers daily (recommended)
  Docker  — docker compose up (for local testing or non-AWS environments)
"""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import date, timedelta, timezone, datetime

import boto3
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "1"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "86400"))

OTLP_ENDPOINT = os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]
OTLP_HEADERS_RAW = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "aws-cost-reporter")

# ── Helpers ────────────────────────────────────────────────────────────────────


def _parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            headers[k.strip()] = v.strip()
    return headers


def _date_to_ns(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1_000_000_000))


# ── Cost Explorer fetch ────────────────────────────────────────────────────────


def _fetch_grouped(
    ce: object, start: date, end: date, group_keys: list[str]
) -> list[dict]:
    """Single paginated CE GetCostAndUsage call. Max 2 group_keys (API limit)."""
    rows: list[dict] = []
    next_token: str | None = None

    while True:
        kwargs: dict = {
            "TimePeriod": {"Start": str(start), "End": str(end)},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost", "AmortizedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": k} for k in group_keys],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token

        resp = ce.get_cost_and_usage(**kwargs)

        for result in resp.get("ResultsByTime", []):
            day = result["TimePeriod"]["Start"]
            for group in result.get("Groups", []):
                values = group["Keys"]
                unblended = float(group["Metrics"]["UnblendedCost"]["Amount"])
                amortized = float(group["Metrics"]["AmortizedCost"]["Amount"])
                if unblended == 0.0 and amortized == 0.0:
                    continue
                rows.append(
                    {
                        "date": day,
                        "keys": dict(zip(group_keys, values)),
                        "unblended": unblended,
                        "amortized": amortized,
                    }
                )

        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    return rows


def fetch_costs(ce: object) -> list[dict]:
    """
    Fetch daily costs via two CE calls (API allows max 2 GroupBy dimensions).
    Call 1: SERVICE + LINKED_ACCOUNT  → rows include aws.service, aws.account.id
    Call 2: SERVICE + REGION          → rows include aws.service, aws.region
    Both sets are exported as separate OTLP datapoints with different label sets.
    """
    end = date.today()
    start = end - timedelta(days=DAYS_BACK)

    by_account = _fetch_grouped(ce, start, end, ["SERVICE", "LINKED_ACCOUNT"])
    by_region = _fetch_grouped(ce, start, end, ["SERVICE", "REGION"])

    rows = []
    for r in by_account:
        rows.append(
            {
                "date": r["date"],
                "service": r["keys"]["SERVICE"],
                "account_id": r["keys"]["LINKED_ACCOUNT"],
                "region": "",
                "unblended": r["unblended"],
                "amortized": r["amortized"],
            }
        )
    for r in by_region:
        rows.append(
            {
                "date": r["date"],
                "service": r["keys"]["SERVICE"],
                "account_id": "",
                "region": r["keys"]["REGION"],
                "unblended": r["unblended"],
                "amortized": r["amortized"],
            }
        )

    log.info(
        "Fetched %d by-account + %d by-region rows (%s → %s)",
        len(by_account),
        len(by_region),
        start,
        end,
    )
    return rows


# ── OTLP export ────────────────────────────────────────────────────────────────


def send_otlp_metrics(rows: list[dict]) -> None:
    if not rows:
        log.info("No cost rows to export")
        return

    unblended_dps: list[dict] = []
    amortized_dps: list[dict] = []

    for row in rows:
        time_ns = _date_to_ns(row["date"])
        attrs = [{"key": "aws.service", "value": {"stringValue": row["service"]}}]
        if row["account_id"]:
            attrs.append(
                {"key": "aws.account.id", "value": {"stringValue": row["account_id"]}}
            )
        if row["region"]:
            attrs.append({"key": "aws.region", "value": {"stringValue": row["region"]}})
        attrs.append({"key": "cost.date", "value": {"stringValue": row["date"]}})
        if row["unblended"] != 0.0:
            unblended_dps.append(
                {
                    "attributes": attrs,
                    "timeUnixNano": time_ns,
                    "asDouble": row["unblended"],
                }
            )
        if row["amortized"] != 0.0:
            amortized_dps.append(
                {
                    "attributes": attrs,
                    "timeUnixNano": time_ns,
                    "asDouble": row["amortized"],
                }
            )

    metrics = []
    if unblended_dps:
        metrics.append(
            {
                "name": "aws.cost.unblended",
                "unit": "USD",
                "description": "Daily unblended AWS cost by service/account and service/region",
                "gauge": {"dataPoints": unblended_dps},
            }
        )
    if amortized_dps:
        metrics.append(
            {
                "name": "aws.cost.amortized",
                "unit": "USD",
                "description": "Daily amortized AWS cost including RI and Savings Plan effective rates",
                "gauge": {"dataPoints": amortized_dps},
            }
        )

    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": OTEL_SERVICE_NAME},
                        },
                        {
                            "key": "telemetry.sdk.language",
                            "value": {"stringValue": "python"},
                        },
                        {"key": "cloud.provider", "value": {"stringValue": "aws"}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "aws.cost_explorer", "version": "1.0.0"},
                        "metrics": metrics,
                    }
                ],
            }
        ]
    }

    hdrs = {**_parse_headers(OTLP_HEADERS_RAW), "Content-Type": "application/json"}
    resp = requests.post(
        f"{OTLP_ENDPOINT.rstrip('/')}/v1/metrics",
        json=payload,
        headers=hdrs,
        timeout=30,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"OTLP export failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    log.info(
        "Exported %d unblended + %d amortized data points",
        len(unblended_dps),
        len(amortized_dps),
    )


# ── Docker mode entry point ────────────────────────────────────────────────────
# Scheduling is handled by the poll loop below (POLL_INTERVAL_SECONDS).
# Lambda mode uses lambda_handler() below — EventBridge handles scheduling there.


def poll(ce: object) -> None:
    rows = fetch_costs(ce)
    send_otlp_metrics(rows)


def main() -> None:
    log.info("AWS Cost Explorer collector starting (Docker mode)")
    log.info("Days back      : %d", DAYS_BACK)
    log.info("Poll interval  : %ds", POLL_INTERVAL_SECONDS)
    log.info("OTLP endpoint  : %s", OTLP_ENDPOINT)

    ce = boto3.client(
        "ce", region_name="us-east-1"
    )  # Cost Explorer is a global service

    def _shutdown(sig, _frame):
        log.info("Shutting down…")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        poll(ce)
        log.info("Sleeping %ds…", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


# ── Lambda mode entry point ────────────────────────────────────────────────────
# EventBridge triggers this on schedule. No poll loop — Lambda process exits after each run.


def lambda_handler(event: dict, context: object) -> dict:
    ce = boto3.client("ce", region_name="us-east-1")
    rows = fetch_costs(ce)
    send_otlp_metrics(rows)
    return {"statusCode": 200, "exported": len(rows)}


if __name__ == "__main__":
    main()
