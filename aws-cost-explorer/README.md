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
| `COST_TAG_KEYS` | No | _(empty)_ | Comma-separated cost-allocation tag keys to break cost down by (e.g. `Project,Environment`). Each key adds one extra CE call per account per run (~$0.01/call). Tags must be activated in AWS Billing → Cost Allocation Tags first; pre-activation cost data has no tag attribution. |

\* Lambda uses the attached IAM role — no credentials needed.

## Metrics

| Metric | Unit | Dimensions |
|---|---|---|
| `aws.cost.unblended` | USD | `aws.service`, `aws.account.id`, `aws.region`, `cost.date` (+ `aws.tag.<key>` when `COST_TAG_KEYS` is set) |
| `aws.cost.amortized` | USD | same — RI and Savings Plan effective rates applied |

The CE API caps `GroupBy` at 2 dimensions. Each run calls `GetDimensionValues` to list linked accounts, then runs one `GetCostAndUsage` per account filtered by `LINKED_ACCOUNT` and grouped by `SERVICE + REGION`. This preserves all three dimensions in a single series without exceeding the API limit.

When `COST_TAG_KEYS` is set, an additional `GetCostAndUsage` call per account per tag key is issued with `GroupBy = [SERVICE, TAG:<key>]`. Tag rows are emitted as a parallel series (without `aws.region`), so existing region-keyed queries are unaffected. Untagged resources surface as `aws.tag.<key>="untagged"`.

`cost.date` (`YYYY-MM-DD`) encodes the billing date as a label so each day forms a distinct series. This matters when `DAYS_BACK > 1`: without it, all fetched days share identical labels and only the last sample survives per series. At the default `DAYS_BACK=1`, `cost.date` adds one unique label value per run with no cardinality overhead.

## Dashboard

Import `last9-dashboard.json` into Last9 for a pre-built 14-panel cost dashboard covering spend totals, daily trends, top services, and per-account breakdown.

## Security

`OTEL_EXPORTER_OTLP_HEADERS` is stored as a Lambda environment variable. AWS encrypts Lambda env vars at rest with KMS by default, but the value is visible in the console to any principal with `lambda:GetFunctionConfiguration`. If your security policy requires stricter secret management, store the token in [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) or SSM Parameter Store and fetch it at runtime instead.

## Verification

After the Lambda runs, query `aws.cost.unblended` in [Last9 Metrics](https://app.last9.io/metrics) and group by `aws.service`.

## Troubleshooting

### `AccessDeniedException` on `ce:GetCostAndUsage` or `ce:GetDimensionValues`

The Lambda role is missing required Cost Explorer permissions. This typically happens when an existing role pre-dates a release that added new permissions. Re-run `./deploy.sh` — it now reapplies the policy on every run. If you can't redeploy, attach the policy manually:

```bash
aws iam put-role-policy \
  --role-name aws-cost-reporter-role \
  --policy-name cost-explorer-read \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["ce:GetCostAndUsage", "ce:GetDimensionValues"],
      "Resource": "*"
    }]
  }'
```

Wait ~2 minutes for the policy to propagate, then retry the Lambda.

### `ResourceConflictException: An update is in progress`

A previous `update-function-code` or `update-function-configuration` call is still in flight. Wait, then retry:

```bash
aws lambda wait function-updated --function-name aws-cost-reporter --region <region>
./deploy.sh
```

### `ValidationException: Only two values for GroupBy are allowed`

You're running an outdated build. Pull latest main and redeploy — `fetch_costs` now respects the Cost Explorer 2-dim GroupBy cap by looping per linked account.

### `mktemp: mkstemp failed on /tmp/aws-cost-reporter-XXXXXX.zip: File exists`

Outdated `deploy.sh`. Pull latest main — `mktemp -u` is now used so `zip` can create a fresh archive.

### Wrong region for Lambda

By default Lambda is deployed to `us-east-1`. To deploy elsewhere, set `AWS_REGION` before running deploy.sh — do **not** edit `deploy.sh`:

```bash
AWS_REGION=ap-south-1 \
OTEL_EXPORTER_OTLP_ENDPOINT=<endpoint> \
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <token>" \
./deploy.sh
```

The Cost Explorer API itself is global (always called against `us-east-1` internally) — `AWS_REGION` controls only where the Lambda function and EventBridge rule live.

---

> **Need usage type breakdown (`BoxUsage:m5.xlarge`, `DataTransfer-Out-Bytes`, etc.)?**
> Cost Explorer doesn't expose usage-type granularity. For line-item detail,
> switch to the [AWS CUR integration](../aws-cur/) — it adds `aws.usage.type`
> and arbitrary `aws.tag.*` dimensions without prior tag activation.
