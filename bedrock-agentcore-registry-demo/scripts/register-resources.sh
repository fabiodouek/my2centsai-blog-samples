#!/usr/bin/env bash
set -euo pipefail

# Register three resource types in an Agent Registry: MCP server, A2A agent, and skill.
# Descriptor payloads are stored in scripts/data/ and read at runtime.
#
# Usage: ./scripts/register-resources.sh <registry-id> [region]

REGISTRY_ID="${1:?Usage: $0 <registry-id> [region]}"
REGION="${2:-us-east-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${SCRIPT_DIR}/data"

echo "============================================"
echo "  Registering Resources"
echo "============================================"
echo ""
echo "Registry ID: ${REGISTRY_ID}"
echo "Region:      ${REGION}"
echo ""

# --- MCP Server ---
echo "[1/3] Registering MCP server: weather-forecast-mcp..."

MCP_DESCRIPTORS=$(jq -n \
  --rawfile server "${DATA_DIR}/mcp-server.json" \
  --rawfile tools  "${DATA_DIR}/mcp-tools.json" \
  '{"mcp":{"server":{"schemaVersion":"2025-12-11","inlineContent":$server},"tools":{"protocolVersion":"2024-11-05","inlineContent":$tools}}}')

MCP_RESPONSE=$(aws bedrock-agentcore-control create-registry-record \
  --registry-id "${REGISTRY_ID}" \
  --name "weather-forecast-mcp" \
  --description "MCP server providing weather forecast and historical weather data tools" \
  --descriptor-type MCP \
  --descriptors "${MCP_DESCRIPTORS}" \
  --record-version "1.0" \
  --region "${REGION}" \
  --output json)

MCP_RECORD_ID=$(echo "${MCP_RESPONSE}" | jq -r '.recordArn' | awk -F'/' '{print $NF}')
echo "  Record ID: ${MCP_RECORD_ID}"
echo "  Status:    $(echo "${MCP_RESPONSE}" | jq -r '.status')"
echo ""

# --- A2A Agent ---
echo "[2/3] Registering A2A agent: invoice-processing-agent..."

A2A_DESCRIPTORS=$(jq -n \
  --rawfile card "${DATA_DIR}/a2a-agent-card.json" \
  '{"a2a":{"agentCard":{"schemaVersion":"0.3","inlineContent":$card}}}')

A2A_RESPONSE=$(aws bedrock-agentcore-control create-registry-record \
  --registry-id "${REGISTRY_ID}" \
  --name "invoice-processing-agent" \
  --description "Autonomous agent that processes invoices, extracts line items, and routes for approval" \
  --descriptor-type A2A \
  --descriptors "${A2A_DESCRIPTORS}" \
  --record-version "2.1" \
  --region "${REGION}" \
  --output json)

A2A_RECORD_ID=$(echo "${A2A_RESPONSE}" | jq -r '.recordArn' | awk -F'/' '{print $NF}')
echo "  Record ID: ${A2A_RECORD_ID}"
echo "  Status:    $(echo "${A2A_RESPONSE}" | jq -r '.status')"
echo ""

# --- Agent Skill ---
echo "[3/3] Registering skill: code-review-skill..."

SKILL_DESCRIPTORS=$(jq -n \
  --rawfile skillmd "${DATA_DIR}/skill-code-review.md" \
  '{"agentSkills":{"skillMd":{"inlineContent":$skillmd}}}')

SKILL_RESPONSE=$(aws bedrock-agentcore-control create-registry-record \
  --registry-id "${REGISTRY_ID}" \
  --name "code-review-skill" \
  --description "Agent skill for performing automated code reviews with security and style checks" \
  --descriptor-type AGENT_SKILLS \
  --descriptors "${SKILL_DESCRIPTORS}" \
  --record-version "1.0" \
  --region "${REGION}" \
  --output json)

SKILL_RECORD_ID=$(echo "${SKILL_RESPONSE}" | jq -r '.recordArn' | awk -F'/' '{print $NF}')
echo "  Record ID: ${SKILL_RECORD_ID}"
echo "  Status:    $(echo "${SKILL_RESPONSE}" | jq -r '.status')"
echo ""

echo "============================================"
echo "  All resources registered (DRAFT status)"
echo "============================================"
echo ""
echo "Record IDs:"
echo "  MCP Server: ${MCP_RECORD_ID}"
echo "  A2A Agent:  ${A2A_RECORD_ID}"
echo "  Skill:      ${SKILL_RECORD_ID}"
echo ""
echo "Next step: submit for approval with:"
echo "  ./scripts/approval-workflow.sh ${REGISTRY_ID} ${REGION}"
