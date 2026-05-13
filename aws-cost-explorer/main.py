"""
AWS Cost Explorer → OpenTelemetry Pipeline

Polls the AWS Cost Explorer API and exports cost metrics to Last9.
No CUR setup or S3 bucket required — data is available within minutes.

Metrics exported:
  aws.cost.unblended  (USD) — daily unblended cost per service/account/region
  aws.cost.amortized  (USD) — daily amortized cost (includes RI/SP effective rates)

Optional tag breakdown:
  Set COST_TAG_KEYS=Project,Environment to also emit cost grouped by
  cost-allocation tag values. Tags must be activated in AWS Billing first.

Deployment modes:
  Lambda  — deploy with deploy.sh; EventBridge triggers daily (recommended)
  Docker  — docker compose up (for local testing or non-AWS environments)
"""

from __future__ import annotations

import logging
import os
import re
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

COST_TAG_KEYS = [
    k.strip() for k in os.environ.get("COST_TAG_KEYS", "").split(",") if k.strip()
]

# ── Helpers ────────────────────────────────────────────────────────────────────


def _warn_tag_key_collisions(tag_keys: list[str]) -> None:
    """Warn if multiple tag keys sanitize to the same Prom attribute suffix."""
    seen: dict[str, list[str]] = {}
    for key in tag_keys:
        seen.setdefault(_sanitize_tag_key(key), []).append(key)
    for sanitized, originals in seen.items():
        if len(originals) > 1:
            log.warning(
                "Tag key collision: %s all sanitize to aws.tag.%s — "
                "metrics will overwrite each other",
                originals,
                sanitized,
            )


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


def _sanitize_tag_key(key: str) -> str:
    """Map an AWS tag key to a Prom/OTel-safe attribute suffix."""
    return re.sub(r"[^a-z0-9_]", "_", key.lower()).strip("_") or "unknown"


# ── Cost Explorer fetch ────────────────────────────────────────────────────────


def _list_linked_accounts(ce: object, period: dict) -> list[str]:
    """List LINKED_ACCOUNT IDs visible to this caller, with pagination.

    Returns [""] when the dimension is empty or the call fails (single-account
    setups, missing ce:GetDimensionValues permission). Empty string is treated
    downstream as "no account filter".

    Policy choice — availability over per-account correctness: on lookup
    failure, we still emit one cost stream (collapsed across accounts, no
    aws.account.id label) rather than dropping all data. Acceptable for
    single-account setups; misleading for multi-account orgs. Both fallback
    paths emit a loud DEGRADED warning so the regression is visible in logs.
    """
    accounts: list[str] = []
    next_token: str | None = None
    try:
        while True:
            kwargs: dict = {"TimePeriod": period, "Dimension": "LINKED_ACCOUNT"}
            if next_token:
                kwargs["NextPageToken"] = next_token
            resp = ce.get_dimension_values(**kwargs)
            accounts.extend(v["Value"] for v in resp.get("DimensionValues", []))
            next_token = resp.get("NextPageToken")
            if not next_token:
                break
    except Exception as exc:
        log.warning(
            "[DEGRADED] get_dimension_values(LINKED_ACCOUNT) failed: %s. "
            "Falling back to ONE unfiltered cost call. "
            "Consequences: (1) costs collapse across accounts in multi-account "
            "orgs; (2) emitted rows have NO aws.account.id label; "
            "(3) dashboards/alerts that group by aws.account.id will be "
            "incomplete or misleading. "
            "Fix: grant ce:GetDimensionValues to the Lambda role.",
            exc,
        )
        return [""]
    if not accounts:
        log.warning(
            "[DEGRADED] LINKED_ACCOUNT dimension returned zero values. "
            "Falling back to ONE unfiltered cost call. "
            "Expected in single-account setups (safe). In multi-account "
            "orgs this indicates insufficient ce:GetDimensionValues "
            "permission — emitted rows will have NO aws.account.id label "
            "and account-level dashboards will be misleading."
        )
        return [""]
    return accounts


def fetch_costs(ce: object) -> list[dict]:
    """
    Fetch daily costs grouped by SERVICE, REGION, looped per LINKED_ACCOUNT.

    Cost Explorer caps GroupBy at 2 dimensions, so account is applied as an
    outer Filter to preserve the service × account × region combination.
    Returns flat list of {date, service, account_id, region, unblended, amortized}.
    """
    end = date.today()
    start = end - timedelta(days=DAYS_BACK)
    period = {"Start": str(start), "End": str(end)}

    accounts = _list_linked_accounts(ce, period)

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


def fetch_tag_costs(ce: object, tag_keys: list[str]) -> list[dict]:
    """
    Fetch daily costs grouped by SERVICE × TAG for each configured tag key.

    CE caps GroupBy at 2 dims and accepts only one TAG entry per call, so we
    issue one extra call per (account, tag_key) pair. Tag values arrive as
    "<TagKey>$<TagValue>" — empty value means "untagged". Tags must be
    activated in AWS Billing → Cost Allocation Tags or this returns empty.

    Returns flat list of {date, service, account_id, tag_key, tag_value,
    unblended, amortized}. Region is intentionally absent — these rows are a
    parallel series to the region-grouped rows from fetch_costs.
    """
    if not tag_keys:
        return []

    end = date.today()
    start = end - timedelta(days=DAYS_BACK)
    period = {"Start": str(start), "End": str(end)}

    accounts = _list_linked_accounts(ce, period)

    rows: list[dict] = []
    for account_id in accounts:
        for tag_key in tag_keys:
            try:
                rows.extend(_fetch_tag_costs_one(ce, period, account_id, tag_key))
            except Exception as exc:
                # One bad tag key (inactive, typo, AccessDenied) must not
                # abort the whole run — log and continue with remaining keys.
                log.warning(
                    "fetch_tag_costs(account=%s, tag_key=%s) failed: %s",
                    account_id or "<caller>",
                    tag_key,
                    exc,
                )
                continue

    if not rows:
        log.warning(
            "No tag-cost rows for keys %s — verify these tags are activated "
            "in AWS Billing → Cost Allocation Tags (~24h indexing latency "
            "after activation)",
            tag_keys,
        )
    else:
        log.info(
            "Fetched %d tag-cost rows (%s → %s, %d account(s) × %d tag key(s))",
            len(rows),
            start,
            end,
            len(accounts),
            len(tag_keys),
        )
    return rows


def _fetch_tag_costs_one(
    ce: object, period: dict, account_id: str, tag_key: str
) -> list[dict]:
    """Single (account, tag_key) CE call with pagination. Raises on failure."""
    rows: list[dict] = []
    next_token: str | None = None
    while True:
        kwargs: dict = {
            "TimePeriod": period,
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost", "AmortizedCost"],
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "TAG", "Key": tag_key},
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
                service, tag_pair = group["Keys"]
                # CE returns "<TagKey>$<TagValue>"; split on first '$'
                _, _, tag_value = tag_pair.partition("$")
                tag_value = tag_value or "untagged"
                unblended = float(group["Metrics"]["UnblendedCost"]["Amount"])
                amortized = float(group["Metrics"]["AmortizedCost"]["Amount"])
                if unblended == 0.0 and amortized == 0.0:
                    continue
                rows.append(
                    {
                        "date": day,
                        "service": service,
                        "account_id": account_id,
                        "tag_key": tag_key,
                        "tag_value": tag_value,
                        "unblended": unblended,
                        "amortized": amortized,
                    }
                )

        next_token = resp.get("NextPageToken")
        if not next_token:
            break
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
        if row.get("account_id"):
            attrs.append(
                {"key": "aws.account.id", "value": {"stringValue": row["account_id"]}}
            )
        if row.get("region"):
            attrs.append({"key": "aws.region", "value": {"stringValue": row["region"]}})
        if row.get("tag_key"):
            attrs.append(
                {
                    "key": f"aws.tag.{_sanitize_tag_key(row['tag_key'])}",
                    "value": {"stringValue": row.get("tag_value", "untagged")},
                }
            )
        attrs.append({"key": "cost.date", "value": {"stringValue": row["date"]}})
        unblended = row.get("unblended", 0.0)
        amortized = row.get("amortized", 0.0)
        if unblended != 0.0:
            unblended_dps.append(
                {
                    "attributes": attrs,
                    "timeUnixNano": time_ns,
                    "asDouble": unblended,
                }
            )
        if amortized != 0.0:
            amortized_dps.append(
                {
                    "attributes": attrs,
                    "timeUnixNano": time_ns,
                    "asDouble": amortized,
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
    rows = fetch_costs(ce) + fetch_tag_costs(ce, COST_TAG_KEYS)
    send_otlp_metrics(rows)


def main() -> None:
    log.info("AWS Cost Explorer collector starting (Docker mode)")
    log.info("Days back      : %d", DAYS_BACK)
    log.info("Poll interval  : %ds", POLL_INTERVAL_SECONDS)
    log.info("OTLP endpoint  : %s", OTLP_ENDPOINT)
    _warn_tag_key_collisions(COST_TAG_KEYS)

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
    _warn_tag_key_collisions(COST_TAG_KEYS)
    ce = boto3.client("ce", region_name="us-east-1")
    rows = fetch_costs(ce) + fetch_tag_costs(ce, COST_TAG_KEYS)
    send_otlp_metrics(rows)
    return {"statusCode": 200, "exported": len(rows)}


if __name__ == "__main__":
    main()
