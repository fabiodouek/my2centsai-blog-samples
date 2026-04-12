#!/usr/bin/env bash
set -euo pipefail

# Register an MCP server via URL-based auto-sync.
# The registry fetches the server descriptor and tool definitions directly from the endpoint.
#
# Usage: ./scripts/register-url-sync.sh <registry-id> [region]

REGISTRY_ID="${1:?Usage: $0 <registry-id> [region]}"
REGION="${2:-us-east-1}"

echo "============================================"
echo "  URL-Based Auto-Sync Registration"
echo "============================================"
echo ""
echo "Registry ID: ${REGISTRY_ID}"
echo "Region:      ${REGION}"
echo ""

echo "Registering AWS Knowledge MCP Server via URL sync..."

RESPONSE=$(aws bedrock-agentcore-control create-registry-record \
  --registry-id "${REGISTRY_ID}" \
  --name "aws-knowledge-mcp" \
  --description "AWS Knowledge MCP Server - provides AWS documentation, code samples, and regional availability via natural language queries" \
  --descriptor-type MCP \
  --synchronization-type URL \
  --synchronization-configuration '{
    "fromUrl": {
      "url": "https://knowledge-mcp.global.api.aws"
    }
  }' \
  --record-version "1.0" \
  --region "${REGION}" \
  --output json)

RECORD_ID=$(echo "${RESPONSE}" | jq -r '.recordArn' | awk -F'/' '{print $NF}')
echo "  Record ID: ${RECORD_ID}"
echo "  Status:    $(echo "${RESPONSE}" | jq -r '.status')"
echo ""

echo "============================================"
echo "  URL-sync record created"
echo "============================================"
echo ""
echo "The registry will fetch the server descriptor and tool definitions"
echo "from https://knowledge-mcp.global.api.aws automatically."
echo ""
echo "Next step: submit for approval with:"
echo "  ./scripts/approval-workflow.sh ${REGISTRY_ID} ${REGION}"
