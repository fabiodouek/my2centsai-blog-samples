#!/usr/bin/env bash
# Test 9: deploy the minimal ADK agent (adk/sbx_agent) with a Cloud Run sandbox for code execution.
# Uses the Gemini API with an API key (the Vertex route returned 403 for gemini-3.8-flash in us-central1).
# Needs: pip install google-adk (2.14.1 here), and a Gemini API key in Secret Manager as m2c-adk-key.
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh
adk deploy cloud_run --project "$PROJECT" --region "$REGION" --service_name adk-sbx \
  --with_cloud_run_sandbox adk/sbx_agent \
  -- --no-allow-unauthenticated --ssh --set-secrets GOOGLE_API_KEY=m2c-adk-key:latest --memory 2Gi --cpu 2 --max-instances 1
# adk deploy sets Vertex AI as the model route; switch it to the Gemini API key
gcloud beta run services update adk-sbx --update-env-vars GOOGLE_GENAI_USE_ENTERPRISE=0
