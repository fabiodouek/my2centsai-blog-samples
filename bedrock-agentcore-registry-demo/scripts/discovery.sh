#!/usr/bin/env bash
set -euo pipefail

# Test Agent Registry search: keyword, semantic, and filtered.
# Usage: ./scripts/discovery.sh <registry-id> [region]

REGISTRY_ID="${1:?Usage: $0 <registry-id> [region]}"
REGION="${2:-us-east-1}"

# Build the registry ARN from the ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:registry/${REGISTRY_ID}"

echo "============================================"
echo "  Testing Registry Discovery"
echo "============================================"
echo ""
echo "Registry ID:  ${REGISTRY_ID}"
echo "Registry ARN: ${REGISTRY_ARN}"
echo "Region:       ${REGION}"
echo ""

# --- Test 1: Keyword search ---
echo "--- Test 1: Keyword Search ---"
echo "Query: \"weather\""
echo ""

aws bedrock-agentcore search-registry-records \
  --search-query "weather" \
  --registry-ids "${REGISTRY_ARN}" \
  --max-results 10 \
  --region "${REGION}" \
  --output json | jq '.registryRecords[] | {name, descriptorType, description: .description[:80]}'

echo ""

# --- Test 2: Semantic search ---
echo "--- Test 2: Semantic Search ---"
echo "Query: \"I need something that handles billing and payments\""
echo "(No record uses the words 'billing' or 'payments' directly)"
echo ""

aws bedrock-agentcore search-registry-records \
  --search-query "I need something that handles billing and payments" \
  --registry-ids "${REGISTRY_ARN}" \
  --max-results 10 \
  --region "${REGION}" \
  --output json | jq '.registryRecords[] | {name, descriptorType, description: .description[:80]}'

echo ""

# --- Test 3: Natural language query ---
echo "--- Test 3: Natural Language Query ---"
echo "Query: \"find tools that help with code quality and security\""
echo ""

aws bedrock-agentcore search-registry-records \
  --search-query "find tools that help with code quality and security" \
  --registry-ids "${REGISTRY_ARN}" \
  --max-results 10 \
  --region "${REGION}" \
  --output json | jq '.registryRecords[] | {name, descriptorType, description: .description[:80]}'

echo ""

# --- Test 4: Filtered search (MCP servers only) ---
echo "--- Test 4: Filtered Search (MCP servers only) ---"
echo "Query: \"forecast\" with filter: descriptorType = MCP"
echo ""

aws bedrock-agentcore search-registry-records \
  --search-query "forecast" \
  --registry-ids "${REGISTRY_ARN}" \
  --max-results 10 \
  --filters '{"descriptorType": {"$eq": "MCP"}}' \
  --region "${REGION}" \
  --output json | jq '.registryRecords[] | {name, descriptorType, description: .description[:80]}'

echo ""

# --- Test 5: Filtered search (skills only) ---
echo "--- Test 5: Filtered Search (skills only) ---"
echo "Query: \"security review\" with filter: descriptorType = AGENT_SKILLS"
echo ""

aws bedrock-agentcore search-registry-records \
  --search-query "security review" \
  --registry-ids "${REGISTRY_ARN}" \
  --max-results 10 \
  --filters '{"descriptorType": {"$eq": "AGENT_SKILLS"}}' \
  --region "${REGION}" \
  --output json | jq '.registryRecords[] | {name, descriptorType, description: .description[:80]}'

echo ""

echo "============================================"
echo "  Discovery tests complete"
echo "============================================"
echo ""
