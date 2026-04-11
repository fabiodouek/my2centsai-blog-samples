#!/usr/bin/env bash
set -euo pipefail

# Delete all records and the registry.
# Usage: ./scripts/cleanup.sh <registry-id> [region]

REGISTRY_ID="${1:?Usage: $0 <registry-id> [region]}"
REGION="${2:-us-east-1}"

echo "============================================"
echo "  Cleaning Up Agent Registry Demo"
echo "============================================"
echo ""
echo "Registry ID: ${REGISTRY_ID}"
echo "Region:      ${REGION}"
echo ""

# List all records
echo "[1/2] Deleting all registry records..."
RECORDS=$(aws bedrock-agentcore-control list-registry-records \
  --registry-id "${REGISTRY_ID}" \
  --region "${REGION}" \
  --output json 2>/dev/null || echo '{"registryRecords":[]}')

RECORD_IDS=$(echo "${RECORDS}" | jq -r '.registryRecords[].recordId // empty')

if [ -n "${RECORD_IDS}" ]; then
  for RECORD_ID in ${RECORD_IDS}; do
    RECORD_NAME=$(echo "${RECORDS}" | jq -r ".registryRecords[] | select(.recordId == \"${RECORD_ID}\") | .name")
    echo "  Deleting: ${RECORD_NAME} (${RECORD_ID})..."

    aws bedrock-agentcore-control delete-registry-record \
      --registry-id "${REGISTRY_ID}" \
      --record-id "${RECORD_ID}" \
      --region "${REGION}" > /dev/null 2>&1 || echo "    Warning: could not delete ${RECORD_ID}"
  done
else
  echo "  No records found."
fi
echo ""

# Delete the registry
echo "[2/2] Deleting registry ${REGISTRY_ID}..."
aws bedrock-agentcore-control delete-registry \
  --registry-id "${REGISTRY_ID}" \
  --region "${REGION}" > /dev/null 2>&1

echo "  Registry deleted."
echo ""

echo "============================================"
echo "  Cleanup complete"
echo "============================================"
