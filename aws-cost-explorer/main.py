"""
AWS Cost Explorer → OpenTelemetry Pipeline

Polls the AWS Cost Explorer API and exports cost metrics to Last9.
No CUR setup or S3 bucket required — data is available within minutes.

Metrics exported:
  aws.cost.unblended     (USD) — daily unblended cost per service/account/region
  aws.cost.amortized     (USD) — daily amortized cost (includes RI/SP effective rates)
  aws.cost.undiscounted  (USD) — daily list-price cost, ignoring credits/discounts/SP
                          fees (see KEPT_RECORD_TYPES). For an account under a
                          promotional/committed AWS credit, UnblendedCost/
                          AmortizedCost read near-$0 (Credit == -Usage) even
                          though real infra consumption continues — this
                          metric is the only one that still shows it.

Deployment modes:
  Lambda  — deploy with deploy.sh; EventBridge triggers daily (recommended)
  Docker  — docker compose up (for local testing or non-AWS environments)
"""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone

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
CUSTOM_LABELS_RAW = os.environ.get("CUSTOM_LABELS", "")

# The only Cost Explorer RECORD_TYPE values that carry positive consumption at
# on-demand (list-price) value. Filtering TO these — rather than excluding a
# growing list of discount/credit RECORD_TYPEs by name — means a new discount
# type Cost Explorer adds later is automatically excluded from "undiscounted"
# instead of silently leaking in. Ported from the validated set in
# pde_daily_glassbox_cost_reporter_clone/l9cost/aws_cost.py (KEPT_RECORD_TYPES),
# measured across a 37-day, six-account audit on 2026-08-06.
UNDISCOUNTED_RECORD_TYPES = ["Usage", "SavingsPlanCoveredUsage"]

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


def fetch_costs(ce: object) -> list[dict]:
    """
    Fetch daily costs grouped by SERVICE, REGION, looped per LINKED_ACCOUNT.

    Cost Explorer caps GroupBy at 2 dimensions, so account is applied as an
    outer Filter to preserve the service × account × region combination.
    Returns flat list of {date, service, account_id, region, unblended, amortized}.
    """
    end = datetime.now(tz=timezone.utc).date()
    start = end - timedelta(days=DAYS_BACK)
    period = {"Start": str(start), "End": str(end)}

    accounts_resp = ce.get_dimension_values(
        TimePeriod=period, Dimension="LINKED_ACCOUNT"
    )
    accounts = [v["Value"] for v in accounts_resp.get("DimensionValues", [])] or [""]

    rows: list[dict] = []
    for account_id in accounts:
        next_token: str | None = None
        while True:
            kwargs: dict = {
                "TimePeriod": period,
                "Granularity": "DAILY",
                "Metrics": ["UnblendedCost", "AmortizedCost"],
                "GroupBy": [
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "REGION"},
                ],
            }
            if account_id:
                kwargs["Filter"] = {
                    "Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}
                }
            if next_token:
                kwargs["NextPageToken"] = next_token

            resp = ce.get_cost_and_usage(**kwargs)

            for result in resp.get("ResultsByTime", []):
                day = result["TimePeriod"]["Start"]
                for group in result.get("Groups", []):
                    service, region = group["Keys"]
                    unblended = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    amortized = float(group["Metrics"]["AmortizedCost"]["Amount"])
                    if unblended == 0.0 and amortized == 0.0:
                        continue
                    rows.append(
                        {
                            "date": day,
                            "service": service,
                            "account_id": account_id,
                            "region": region,
                            "unblended": unblended,
                            "amortized": amortized,
                        }
                    )

            next_token = resp.get("NextPageToken")
            if not next_token:
                break

    log.info(
        "Fetched %d cost rows (%s → %s, %d account(s))",
        len(rows),
        start,
        end,
        len(accounts),
    )
    return rows


def fetch_undiscounted_costs(ce: object) -> dict[tuple[str, str, str, str], float]:
    """
    Fetch daily list-price cost, filtered to RECORD_TYPE IN
    UNDISCOUNTED_RECORD_TYPES (Usage, SavingsPlanCoveredUsage only) — the
    on-demand value of what was actually consumed, ignoring any Credit,
    Discount, or Savings-Plan-fee line item.

    Cost Explorer caps GroupBy at 2 dims and RECORD_TYPE can't be a Filter
    dimension alongside a GroupBy on the same call without also grouping by
    it, so this is a SEPARATE call from fetch_costs (same SERVICE+REGION
    GroupBy, RECORD_TYPE added as a Filter instead of a GroupBy dimension —
    that's allowed since Filter and GroupBy dimensions are independent).

    Returns {(date, service, account_id, region): undiscounted_usd} — a dict
    keyed to merge directly onto fetch_costs' rows in send_otlp_metrics,
    rather than a second parallel row list that would need its own
    attribute-building logic.
    """
    end = datetime.now(tz=timezone.utc).date()
    start = end - timedelta(days=DAYS_BACK)
    period = {"Start": str(start), "End": str(end)}

    accounts_resp = ce.get_dimension_values(
        TimePeriod=period, Dimension="LINKED_ACCOUNT"
    )
    accounts = [v["Value"] for v in accounts_resp.get("DimensionValues", [])] or [""]

    out: dict[tuple[str, str, str, str], float] = {}
    for account_id in accounts:
        next_token: str | None = None
        while True:
            filters = [{"Dimensions": {"Key": "RECORD_TYPE", "Values": UNDISCOUNTED_RECORD_TYPES}}]
            if account_id:
                filters.append({"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}})
            kwargs: dict = {
                "TimePeriod": period,
                "Granularity": "DAILY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "REGION"},
                ],
                "Filter": {"And": filters} if len(filters) > 1 else filters[0],
            }
            if next_token:
                kwargs["NextPageToken"] = next_token

            resp = ce.get_cost_and_usage(**kwargs)

            for result in resp.get("ResultsByTime", []):
                day = result["TimePeriod"]["Start"]
                for group in result.get("Groups", []):
                    service, region = group["Keys"]
                    amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    if amount == 0.0:
                        continue
                    out[(day, service, account_id, region)] = amount

            next_token = resp.get("NextPageToken")
            if not next_token:
                break

    log.info("Fetched %d undiscounted cost rows (%s → %s, %d account(s))",
              len(out), start, end, len(accounts))
    return out


# ── OTLP export ────────────────────────────────────────────────────────────────


def send_otlp_metrics(
    rows: list[dict],
    undiscounted: dict[tuple[str, str, str, str], float] | None = None,
) -> None:
    if not rows and not undiscounted:
        log.info("No cost rows to export")
        return

    unblended_dps: list[dict] = []
    amortized_dps: list[dict] = []
    undiscounted_dps: list[dict] = []
    custom_labels = _parse_headers(CUSTOM_LABELS_RAW)

    def _attrs(date: str, service: str, account_id: str, region: str) -> list[dict]:
        attrs = [{"key": "aws.service", "value": {"stringValue": service}}]
        for k, v in custom_labels.items():
            attrs.append({"key": k, "value": {"stringValue": v}})
        if account_id:
            attrs.append({"key": "aws.account.id", "value": {"stringValue": account_id}})
        if region:
            attrs.append({"key": "aws.region", "value": {"stringValue": region}})
        attrs.append({"key": "cost.date", "value": {"stringValue": date}})
        return attrs

    for row in rows:
        time_ns = _date_to_ns(row["date"])
        attrs = _attrs(row["date"], row["service"], row["account_id"], row["region"])
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

    # Independent metric, independent loop — deliberately NOT merged onto
    # `rows`: a service can have Usage (and so an undiscounted entry) with
    # zero net UnblendedCost after a credit fully offsets it (or vice versa),
    # so the two row sets aren't guaranteed to line up 1:1.
    for (date, service, account_id, region), amount in (undiscounted or {}).items():
        undiscounted_dps.append(
            {
                "attributes": _attrs(date, service, account_id, region),
                "timeUnixNano": _date_to_ns(date),
                "asDouble": amount,
            }
        )

    metrics = []
    if unblended_dps:
        metrics.append(
            {
                "name": "aws.cost.unblended",
                "unit": "USD",
                "description": "Daily unblended AWS cost by service, account, and region",
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
    if undiscounted_dps:
        metrics.append(
            {
                "name": "aws.cost.undiscounted",
                "unit": "USD",
                "description": (
                    "Daily list-price AWS cost by service, account, and region — "
                    "ignores credits/discounts/Savings-Plan fees, so it stays "
                    "meaningful even when a promotional credit drives "
                    "UnblendedCost/AmortizedCost to ~$0"
                ),
                "gauge": {"dataPoints": undiscounted_dps},
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
        "Exported %d unblended + %d amortized + %d undiscounted data points",
        len(unblended_dps),
        len(amortized_dps),
        len(undiscounted_dps),
    )


# ── Docker mode entry point ────────────────────────────────────────────────────
# Scheduling is handled by the poll loop below (POLL_INTERVAL_SECONDS).
# Lambda mode uses lambda_handler() below — EventBridge handles scheduling there.


def poll(ce: object) -> None:
    rows = fetch_costs(ce)
    undiscounted = fetch_undiscounted_costs(ce)
    send_otlp_metrics(rows, undiscounted)


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
    undiscounted = fetch_undiscounted_costs(ce)
    send_otlp_metrics(rows, undiscounted)
    return {"statusCode": 200, "exported": len(rows), "exported_undiscounted": len(undiscounted)}


if __name__ == "__main__":
    main()
