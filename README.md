# AWS Cost Reporter

You're already paying for AWS. You might as well know exactly what you're paying for, broken down by service, account, and region — right inside Last9 where the rest of your observability lives.

This repo ships two integrations. Pick one — **never both**. Running both simultaneously double-counts every dollar.

## aws-cost-explorer — start here

No S3 bucket. No CUR setup. No waiting 24 hours for data.

One CloudFormation stack. One parameter. Done.

```
Upload cloudformation.yaml → fill in your Last9 token → Create stack
```

A Lambda runs in your AWS account every day, calls the Cost Explorer API, and sends `aws.cost.unblended` and `aws.cost.amortized` to Last9. That's it. No servers. No credentials lying around. $0/month to run.

→ [aws-cost-explorer/](./aws-cost-explorer/)

## aws-cur — when you need more

If you want cost broken down by individual EC2 instance, S3 bucket, or custom team/environment tags, you need the Cost and Usage Report. It's more setup (enable CUR, wait 24h for first data, point it at S3), but it gives you line-item granularity that Cost Explorer can't.

→ [aws-cur/](./aws-cur/)

## Metrics

Both integrations send the same core metrics to Last9:

| Metric | What it tells you |
|---|---|
| `aws.cost.unblended` | What AWS actually charged, per service per day |
| `aws.cost.amortized` | Same, but with RI and Savings Plan costs spread across usage (the honest number) |

Query `aws.cost.unblended` in Last9, group by `aws.service`. That's your cost dashboard.
