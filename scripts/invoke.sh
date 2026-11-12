#!/usr/bin/env bash
# Run the report now instead of waiting for 8:00 AM.
# Usage: ./scripts/invoke.sh   (from the project root)
set -euo pipefail

FN=$(terraform -chdir=terraform output -raw lambda_function_name)
REGION=$(terraform -chdir=terraform output -raw region)

echo "Invoking ${FN} in ${REGION}..."
aws lambda invoke --region "$REGION" --function-name "$FN" \
  --cli-binary-format raw-in-base64-out --payload '{}' /tmp/finops-response.json >/dev/null
echo "Response:"
cat /tmp/finops-response.json; echo
echo
echo "Last log lines:"
aws logs tail "/aws/lambda/${FN}" --region "$REGION" --since 5m | tail -20
echo
echo "Check your inbox for the report."