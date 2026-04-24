# AWS Cost Reporter → Last9

Sends AWS billing data to Last9 as OpenTelemetry metrics. Two integration options:

| | [aws-cost-explorer](./aws-cost-explorer/) | [aws-cur](./aws-cur/) |
|---|---|---|
| **Setup time** | Minutes | 24h+ (CUR generation) |
| **Data source** | Cost Explorer API | S3 parquet (CUR files) |
| **Granularity** | Service / account / region | + resource IDs, cost allocation tags |
| **Deploy** | CloudFormation or `deploy.sh` | Docker |
| **Best for** | Quick start | Detailed analysis, tag-based cost allocation |

## Quick start

### Cost Explorer (recommended for most teams)

```bash
# CloudFormation — upload aws-cost-explorer/cloudformation.yaml to AWS console
# Fill in OtlpHeaders with your Last9 token → Create stack

# Or via CLI:
cd aws-cost-explorer
OTLP_HEADERS="Authorization=Basic <token>" ./deploy.sh
```

### CUR / S3

```bash
cd aws-cur
cp .env.example .env  # set CUR_S3_BUCKET, CUR_REPORT_NAME, OTLP_HEADERS
docker compose up
```

## Metrics

Both integrations emit:

| Metric | Unit | Description |
|---|---|---|
| `aws.cost.unblended` | USD | Daily unblended cost |
| `aws.cost.amortized` | USD | Daily cost with RI/SP effective rates |
| `aws.usage.quantity` | 1 | Daily usage amount (CUR only) |

Query `aws.cost.unblended` in [Last9 Metrics](https://app.last9.io/metrics) and group by `aws.service`.
