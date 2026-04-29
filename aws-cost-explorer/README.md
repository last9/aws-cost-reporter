# AWS Cost Explorer → Last9

Sends AWS cost metrics to Last9 using the Cost Explorer API. No S3 bucket or CUR setup required — data flows within minutes.

## Prerequisites

- AWS account with billing access
- [Last9 OTLP credentials](https://app.last9.io/integrations)

## Deploy with AWS CLI (recommended)

Requires AWS CLI configured locally. Packages `main.py`, creates the IAM role, deploys Lambda, and wires up the EventBridge daily schedule:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=<your_last9_otlp_endpoint> \
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <your-last9-token>" \
./deploy.sh
```

Test after deploy:
```bash
aws lambda invoke --function-name aws-cost-reporter /tmp/out.json && cat /tmp/out.json
```

## Deploy with CloudFormation

Requires an S3 bucket to stage the Lambda zip. `deploy.sh` handles packaging and upload:

```bash
USE_CF=1 \
CF_S3_BUCKET=my-deploy-bucket \
OTEL_EXPORTER_OTLP_ENDPOINT=<your_last9_otlp_endpoint> \
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <your-last9-token>" \
./deploy.sh
```

Or deploy the CloudFormation stack manually from the AWS console:
1. Download `aws-cost-explorer.zip` from the [latest release](../../releases/latest)
2. Upload it to an S3 bucket in your AWS account
3. Upload `cloudformation.yaml` in the CloudFormation console and supply `LambdaS3Bucket`/`LambdaS3Key`

## Run with Docker (local testing)

```bash
cp .env.example .env
# Fill in AWS credentials and OTEL_EXPORTER_OTLP_HEADERS
docker compose up
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Yes | — | Last9 OTLP endpoint |
| `OTEL_EXPORTER_OTLP_HEADERS` | Yes | — | Last9 auth header (`Authorization=Basic <token>`) |
| `AWS_ACCESS_KEY_ID` | No* | — | AWS access key (Docker only) |
| `AWS_SECRET_ACCESS_KEY` | No* | — | AWS secret key (Docker only) |
| `AWS_DEFAULT_REGION` | No | `us-east-1` | AWS region |
| `DAYS_BACK` | No | `1` | Days of history to fetch per run. Set to `30`+ for initial backfill, then drop to `1`. |
| `POLL_INTERVAL_SECONDS` | No | `86400` | Re-poll interval for Docker mode |
| `OTEL_SERVICE_NAME` | No | `aws-cost-reporter` | Service name in Last9 |

\* Lambda uses the attached IAM role — no credentials needed.

## Metrics

| Metric | Unit | Dimensions |
|---|---|---|
| `aws.cost.unblended` | USD | `aws.service`, `aws.account.id`, `aws.region`, `cost.date` |
| `aws.cost.amortized` | USD | same — RI and Savings Plan effective rates applied |

The CE API caps `GroupBy` at 2 dimensions. Each run calls `GetDimensionValues` to list linked accounts, then runs one `GetCostAndUsage` per account filtered by `LINKED_ACCOUNT` and grouped by `SERVICE + REGION`. This preserves all three dimensions in a single series without exceeding the API limit.

`cost.date` (`YYYY-MM-DD`) encodes the billing date as a label so each day forms a distinct series. This matters when `DAYS_BACK > 1`: without it, all fetched days share identical labels and only the last sample survives per series. At the default `DAYS_BACK=1`, `cost.date` adds one unique label value per run with no cardinality overhead.

## Dashboard

Import `last9-dashboard.json` into Last9 for a pre-built 14-panel cost dashboard covering spend totals, daily trends, top services, and per-account breakdown.

## Security

`OTEL_EXPORTER_OTLP_HEADERS` is stored as a Lambda environment variable. AWS encrypts Lambda env vars at rest with KMS by default, but the value is visible in the console to any principal with `lambda:GetFunctionConfiguration`. If your security policy requires stricter secret management, store the token in [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) or SSM Parameter Store and fetch it at runtime instead.

## Verification

After the Lambda runs, query `aws.cost.unblended` in [Last9 Metrics](https://app.last9.io/metrics) and group by `aws.service`.

---

> **Need usage type breakdown (`BoxUsage:m5.xlarge`, `DataTransfer-Out-Bytes`, etc.) or cost allocation tags?**
> Cost Explorer groups by service, account, and region only. For line-item granularity,
> switch to the [AWS CUR integration](../aws-cur/) — it adds `aws.usage.type` and `aws.tag.*` dimensions.
