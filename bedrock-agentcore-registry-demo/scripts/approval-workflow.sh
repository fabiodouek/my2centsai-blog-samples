#!/usr/bin/env bash
set -euo pipefail

# Submit all DRAFT records for approval, then approve them.
# Usage: ./scripts/approval-workflow.sh <registry-id> [region]

REGISTRY_ID="${1:?Usage: $0 <registry-id> [region]}"
REGION="${2:-us-east-1}"

echo "============================================"
echo "  Approval Workflow"
echo "============================================"
echo ""
echo "Registry ID: ${REGISTRY_ID}"
echo "Region:      ${REGION}"
echo ""

# Get all records in DRAFT status
echo "[1/3] Listing records in DRAFT status..."
RECORDS=$(aws bedrock-agentcore-control list-registry-records \
  --registry-id "${REGISTRY_ID}" \
  --region "${REGION}" \
  --output json)

RECORD_IDS=$(echo "${RECORDS}" | jq -r '.registryRecords[] | select(.status == "DRAFT") | .recordId')

if [ -z "${RECORD_IDS}" ]; then
  echo "  No DRAFT records found. Checking for PENDING_APPROVAL..."
  RECORD_IDS=$(echo "${RECORDS}" | jq -r '.registryRecords[] | select(.status == "PENDING_APPROVAL") | .recordId')
  if [ -z "${RECORD_IDS}" ]; then
    echo "  No records to process. All records may already be approved."
    exit 0
  fi
  echo "  Found records in PENDING_APPROVAL. Skipping to approval step."
  SKIP_SUBMIT=true
else
  SKIP_SUBMIT=false
  RECORD_COUNT=$(echo "${RECORD_IDS}" | wc -l | tr -d ' ')
  echo "  Found ${RECORD_COUNT} records in DRAFT status."
fi
echo ""

# Submit for approval
if [ "${SKIP_SUBMIT}" = false ]; then
  echo "[2/3] Submitting records for approval..."
  for RECORD_ID in ${RECORD_IDS}; do
    RECORD_NAME=$(echo "${RECORDS}" | jq -r ".registryRecords[] | select(.recordId == \"${RECORD_ID}\") | .name")
    echo "  Submitting: ${RECORD_NAME} (${RECORD_ID})..."

    aws bedrock-agentcore-control submit-registry-record-for-approval \
      --registry-id "${REGISTRY_ID}" \
      --record-id "${RECORD_ID}" \
      --region "${REGION}" > /dev/null

    echo "    Status: PENDING_APPROVAL"
  done
  echo ""

  # Brief pause for status propagation
  sleep 2

  # Refresh record list
  RECORDS=$(aws bedrock-agentcore-control list-registry-records \
    --registry-id "${REGISTRY_ID}" \
    --region "${REGION}" \
    --output json)
  RECORD_IDS=$(echo "${RECORDS}" | jq -r '.registryRecords[] | select(.status == "PENDING_APPROVAL") | .recordId')
fi

# Approve records
echo "[3/3] Approving records..."
for RECORD_ID in ${RECORD_IDS}; do
  RECORD_NAME=$(echo "${RECORDS}" | jq -r ".registryRecords[] | select(.recordId == \"${RECORD_ID}\") | .name")
  echo "  Approving: ${RECORD_NAME} (${RECORD_ID})..."

  aws bedrock-agentcore-control update-registry-record-status \
    --registry-id "${REGISTRY_ID}" \
    --record-id "${RECORD_ID}" \
    --status APPROVED \
    --status-reason "Reviewed and approved for demo registry" \
    --region "${REGION}" > /dev/null

  echo "    Status: APPROVED"
done
echo ""

echo "============================================"
echo "  All records approved and discoverable!"
echo "============================================"
echo ""
echo "Next step: test discovery with:"
echo "  ./scripts/discovery.sh ${REGISTRY_ID} ${REGION}"
