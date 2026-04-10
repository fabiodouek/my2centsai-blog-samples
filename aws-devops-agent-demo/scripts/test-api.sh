#!/usr/bin/env bash
# test-api.sh
#
# Smoke test for the items API.
#
# Usage: ./test-api.sh <api-url>

set -euo pipefail

API_URL="${1:?Usage: $0 <api-url>}"

echo "Testing API at: $API_URL"
echo ""

# Health check
echo "--- Health Check ---"
curl -s "$API_URL/health" | python3 -m json.tool
echo ""

# Create an item
echo "--- Create Item ---"
ITEM=$(curl -s -X POST "$API_URL/items" \
  -H "Content-Type: application/json" \
  -d '{"name": "test-item", "description": "created by smoke test"}')
echo "$ITEM" | python3 -m json.tool
ITEM_ID=$(echo "$ITEM" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo ""

# List items
echo "--- List Items ---"
curl -s "$API_URL/items" | python3 -m json.tool
echo ""

# Get single item
echo "--- Get Item ---"
curl -s "$API_URL/items/$ITEM_ID" | python3 -m json.tool
echo ""

# Delete item
echo "--- Delete Item ---"
curl -s -X DELETE "$API_URL/items/$ITEM_ID" | python3 -m json.tool
echo ""

echo "All tests passed."
