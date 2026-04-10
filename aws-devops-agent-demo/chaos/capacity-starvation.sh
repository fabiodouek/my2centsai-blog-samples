#!/usr/bin/env bash
# capacity-starvation.sh
#
# Chaos script: switches a DynamoDB table from on-demand to provisioned
# with minimal capacity (1 RCU / 1 WCU), then blasts traffic at the API
# to trigger ProvisionedThroughputExceededException errors.
#
# Usage: ./capacity-starvation.sh <api-url> [requests]
#
# Prerequisites: aws cli, curl

set -euo pipefail

API_URL="${1:?Usage: $0 <api-url> [requests]}"
TABLE_NAME="devops-agent-demo-items"
TOTAL_REQUESTS="${2:-1000}"
CONCURRENT=10

echo "============================================"
echo "  CHAOS: DynamoDB Capacity Starvation"
echo "============================================"
echo ""
echo "Target API:    $API_URL"
echo "Target Table:  $TABLE_NAME"
echo "Requests:      $TOTAL_REQUESTS"
echo ""

# Step 1: Record current billing mode
echo "[1/4] Recording current table configuration..."
CURRENT_MODE=$(aws dynamodb describe-table \
  --table-name "$TABLE_NAME" \
  --query 'Table.BillingModeSummary.BillingMode' \
  --output text 2>/dev/null || echo "UNKNOWN")
echo "  Current billing mode: $CURRENT_MODE"

# Step 2: Switch to provisioned with minimal capacity
echo ""
echo "[2/4] Switching to PROVISIONED mode with 1 RCU / 1 WCU..."
if aws dynamodb update-table \
  --table-name "$TABLE_NAME" \
  --billing-mode PROVISIONED \
  --provisioned-throughput ReadCapacityUnits=1,WriteCapacityUnits=1 \
  --output text --query 'TableDescription.TableStatus' 2>/dev/null; then
  echo "  Waiting for table to become ACTIVE..."
  aws dynamodb wait table-exists --table-name "$TABLE_NAME"
  echo "  Table is ACTIVE with 1 RCU / 1 WCU"
else
  echo "  Table is already provisioned at 1 RCU / 1 WCU. Continuing..."
fi

# Step 3: Blast traffic
echo ""
echo "[3/4] Blasting $TOTAL_REQUESTS requests at the API..."
echo "  This will cause DynamoDB throttling errors."
echo ""

SUCCESS=0
ERRORS=0

for i in $(seq 1 "$TOTAL_REQUESTS"); do
  # Fire concurrent requests in background
  (
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST "$API_URL/items" \
      -H "Content-Type: application/json" \
      -d "{\"name\": \"chaos-item-$i\", \"description\": \"injected by capacity starvation\"}" \
      --max-time 10 2>/dev/null || echo "000")
    echo "$STATUS"
  ) &

  # Limit concurrency
  if (( i % CONCURRENT == 0 )); then
    wait
  fi

  # Progress indicator every 20 requests
  if (( i % 20 == 0 )); then
    echo "  Sent $i / $TOTAL_REQUESTS requests..."
  fi
done

# Wait for remaining requests
wait

echo ""
echo "[4/4] Chaos injection complete."
echo ""
echo "============================================"
echo "  WHAT TO DO NEXT"
echo "============================================"
echo ""
echo "1. Check CloudWatch alarms (they should be firing):"
echo "   aws cloudwatch describe-alarms \\"
echo "     --alarm-name-prefix devops-agent-demo \\"
echo "     --query 'MetricAlarms[].{Name:AlarmName,State:StateValue}' \\"
echo "     --output table"
echo ""
echo "2. Start a DevOps Agent investigation from the console"
echo "   or use the CLI:"
echo "   aws devops-agent start-investigation \\"
echo "     --agent-space-id <your-space-id> \\"
echo "     --description 'API returning 500 errors after DynamoDB throttling'"
echo ""
echo "3. When done, restore the table:"
echo "   ./restore.sh"
echo ""
