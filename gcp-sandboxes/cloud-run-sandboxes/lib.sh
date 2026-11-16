# Shared settings for the Cloud Run sandboxes tests.
# Usage: source lib.sh. Needs: gcloud (with the beta component) and curl.
: "${PROJECT:=$(gcloud config get-value project 2>/dev/null)}"
: "${REGION:=us-central1}"
: "${SERVICE:=sbx-host}"
export PROJECT REGION SERVICE
export CLOUDSDK_RUN_REGION=$REGION

svc_url() { gcloud run services describe "$SERVICE" --format='value(status.url)'; }

# run_on_instance <script-file> [timeout-seconds]: run a shell script on the Cloud Run
# instance through the private command endpoint. The output streams back as it is produced
# (stdout and stderr together) and ends with "[exit CODE, SECONDS s]".
run_on_instance() {
  local url token body
  url=$(svc_url); token=$(gcloud auth print-identity-token)
  body=$(mktemp); { echo "export SERVICE_URL=$url N=${N:-} CONC_N=${CONC_N:-}"; cat "$1"; } > "$body"   # scripts can use $SERVICE_URL
  curl -sSN -X POST --max-time "${2:-900}" -H "Authorization: Bearer $token" \
    --data-binary @"$body" "$url/run?timeout=${2:-900}"
  rm -f "$body"
}
