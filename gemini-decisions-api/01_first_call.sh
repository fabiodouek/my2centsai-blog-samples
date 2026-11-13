#!/usr/bin/env bash
# The smallest possible Decisions request: one yes/no ("noul") question, over curl.
set -euo pipefail
: "${GEMINI_API_KEY:?Set GEMINI_API_KEY first}"

curl -sS -X POST "https://generativelanguage.googleapis.com/v1beta/decisions" \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-decision-1",
    "input": "The blue mug I ordered on Monday arrived cracked on Wednesday.",
    "questions": {
      "is_damaged": {
        "type": "noul",
        "instructions": "Did the item arrive damaged?"
      }
    }
  }'
echo
