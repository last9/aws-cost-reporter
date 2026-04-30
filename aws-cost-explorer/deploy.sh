#!/usr/bin/env bash
# Deploys the AWS Cost Explorer collector as a Lambda function with a daily
# EventBridge schedule. Requires AWS CLI configured with sufficient permissions.
#
# Usage (direct Lambda API — default):
#   OTEL_EXPORTER_OTLP_ENDPOINT=<endpoint> \
#   OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <token>" \
#   ./deploy.sh
#
# Usage (CloudFormation — requires an S3 bucket to stage the zip):
#   USE_CF=1 \
#   CF_S3_BUCKET=my-deploy-bucket \
#   OTEL_EXPORTER_OTLP_ENDPOINT=<endpoint> \
#   OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <token>" \
#   ./deploy.sh
#
# Optional overrides:
#   FUNCTION_NAME=aws-cost-reporter   (default)
#   AWS_REGION=us-east-1              (default)
#   DAYS_BACK=1                       (default; use 30+ for initial backfill)
#   SCHEDULE=rate(1 day)              (default)
#   OTEL_SERVICE_NAME=aws-cost-reporter

set -euo pipefail

FUNCTION_NAME="${FUNCTION_NAME:-aws-cost-reporter}"
AWS_REGION="${AWS_REGION:-us-east-1}"
DAYS_BACK="${DAYS_BACK:-1}"
SCHEDULE="${SCHEDULE:-rate(1 day)}"
OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-aws-cost-reporter}"
USE_CF="${USE_CF:-0}"

: "${OTEL_EXPORTER_OTLP_ENDPOINT:?OTEL_EXPORTER_OTLP_ENDPOINT is required. Get it from Last9 dashboard → Integrations → OTLP}"
: "${OTEL_EXPORTER_OTLP_HEADERS:?OTEL_EXPORTER_OTLP_HEADERS is required. Set it to: Authorization=Basic <your-last9-token>}"

echo "==> Deploying ${FUNCTION_NAME} to ${AWS_REGION}"

# ── 1. Package Lambda ──────────────────────────────────────────────────────────

echo "--> Packaging Lambda"
BUILD_DIR=$(mktemp -d)
pip install --quiet -r requirements.txt -t "${BUILD_DIR}"
cp main.py "${BUILD_DIR}/"
ZIP_PATH=$(mktemp -u /tmp/aws-cost-reporter-XXXXXX.zip)
(cd "${BUILD_DIR}" && zip -qr "${ZIP_PATH}" .)
rm -rf "${BUILD_DIR}"

# ── 2a. Deploy via CloudFormation ──────────────────────────────────────────────

if [[ "${USE_CF}" == "1" ]]; then
  : "${CF_S3_BUCKET:?CF_S3_BUCKET is required for CloudFormation deploy (S3 bucket to stage the zip)}"
  CF_S3_KEY="${CF_S3_KEY:-aws-cost-reporter.zip}"
  STACK_NAME="${STACK_NAME:-${FUNCTION_NAME}}"

  echo "--> Uploading zip to s3://${CF_S3_BUCKET}/${CF_S3_KEY}"
  aws s3 cp "${ZIP_PATH}" "s3://${CF_S3_BUCKET}/${CF_S3_KEY}" --region "${AWS_REGION}"

  echo "--> Deploying CloudFormation stack: ${STACK_NAME}"
  aws cloudformation deploy \
    --stack-name "${STACK_NAME}" \
    --template-file cloudformation.yaml \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "${AWS_REGION}" \
    --parameter-overrides \
      OtlpEndpoint="${OTEL_EXPORTER_OTLP_ENDPOINT}" \
      OtlpHeaders="${OTEL_EXPORTER_OTLP_HEADERS}" \
      LambdaS3Bucket="${CF_S3_BUCKET}" \
      LambdaS3Key="${CF_S3_KEY}" \
      DaysBack="${DAYS_BACK}" \
      Schedule="${SCHEDULE}" \
      ServiceName="${OTEL_SERVICE_NAME}"

  FUNCTION_ARN=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='FunctionArn'].OutputValue" \
    --output text)

# ── 2b. Deploy via Lambda API (default) ────────────────────────────────────────

else
  ROLE_NAME="${FUNCTION_NAME}-role"
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

  NEW_ROLE=
  if ! aws iam get-role --role-name "${ROLE_NAME}" &>/dev/null; then
    echo "--> Creating IAM role ${ROLE_NAME}"
    aws iam create-role \
      --role-name "${ROLE_NAME}" \
      --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
          "Effect": "Allow",
          "Principal": {"Service": "lambda.amazonaws.com"},
          "Action": "sts:AssumeRole"
        }]
      }' >/dev/null
    NEW_ROLE=1
  else
    echo "--> IAM role ${ROLE_NAME} already exists — reapplying policy"
  fi

  # Reapply on every run so existing roles pick up new permissions.
  aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

  aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name cost-explorer-read \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["ce:GetCostAndUsage", "ce:GetDimensionValues"],
        "Resource": "*"
      }]
    }'

  if [[ -n "${NEW_ROLE}" ]]; then
    echo "--> Waiting for IAM role to propagate…"
    sleep 10
  fi

  ENV_VARS="Variables={OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT},OTEL_EXPORTER_OTLP_HEADERS=${OTEL_EXPORTER_OTLP_HEADERS},OTEL_SERVICE_NAME=${OTEL_SERVICE_NAME},DAYS_BACK=${DAYS_BACK}}"

  if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" &>/dev/null; then
    echo "--> Updating Lambda function"
    aws lambda update-function-code \
      --function-name "${FUNCTION_NAME}" \
      --zip-file "fileb://${ZIP_PATH}" \
      --region "${AWS_REGION}" >/dev/null
    # update-function-code is async — wait for it before mutating config,
    # otherwise update-function-configuration hits ResourceConflictException.
    aws lambda wait function-updated \
      --function-name "${FUNCTION_NAME}" \
      --region "${AWS_REGION}"
    aws lambda update-function-configuration \
      --function-name "${FUNCTION_NAME}" \
      --environment "${ENV_VARS}" \
      --region "${AWS_REGION}" >/dev/null
  else
    echo "--> Creating Lambda function"
    aws lambda create-function \
      --function-name "${FUNCTION_NAME}" \
      --runtime python3.13 \
      --role "${ROLE_ARN}" \
      --handler main.lambda_handler \
      --zip-file "fileb://${ZIP_PATH}" \
      --timeout 300 \
      --memory-size 256 \
      --environment "${ENV_VARS}" \
      --region "${AWS_REGION}" >/dev/null
  fi

  FUNCTION_ARN=$(aws lambda get-function \
    --function-name "${FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --query Configuration.FunctionArn \
    --output text)

  RULE_NAME="${FUNCTION_NAME}-schedule"

  echo "--> Creating EventBridge rule: ${SCHEDULE}"
  RULE_ARN=$(aws events put-rule \
    --name "${RULE_NAME}" \
    --schedule-expression "${SCHEDULE}" \
    --state ENABLED \
    --region "${AWS_REGION}" \
    --query RuleArn \
    --output text)

  aws lambda add-permission \
    --function-name "${FUNCTION_NAME}" \
    --statement-id "${RULE_NAME}" \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn "${RULE_ARN}" \
    --region "${AWS_REGION}" 2>/dev/null || true

  aws events put-targets \
    --rule "${RULE_NAME}" \
    --targets "Id=1,Arn=${FUNCTION_ARN}" \
    --region "${AWS_REGION}" >/dev/null
fi

rm -f "${ZIP_PATH}"

# ── Done ───────────────────────────────────────────────────────────────────────

echo ""
echo "✓ Deployed ${FUNCTION_NAME}"
echo "  Function : ${FUNCTION_ARN}"
echo "  Schedule : ${SCHEDULE}"
echo ""
echo "Test now:"
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} --region ${AWS_REGION} /tmp/out.json && cat /tmp/out.json"
