#!/usr/bin/env bash
# restore.sh
#
# Restores a DynamoDB table back to on-demand billing mode
# after the capacity starvation chaos experiment.
#
# Usage: ./restore.sh

set -euo pipefail

TABLE_NAME="devops-agent-demo-items"

echo "Restoring $TABLE_NAME to PAY_PER_REQUEST (on-demand) mode..."

aws dynamodb update-table \
  --table-name "$TABLE_NAME" \
  --billing-mode PAY_PER_REQUEST \
  --output text --query 'TableDescription.TableStatus'

echo "Waiting for table to become ACTIVE..."
aws dynamodb wait table-exists --table-name "$TABLE_NAME"

echo "Table restored to on-demand billing."
echo ""
echo "Current table status:"
aws dynamodb describe-table \
  --table-name "$TABLE_NAME" \
  --query 'Table.{Status:TableStatus,BillingMode:BillingModeSummary.BillingMode}' \
  --output table
