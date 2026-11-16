#!/usr/bin/env bash
# Delete everything the Cloud Run sandboxes tests created. Safe to re-run.
cd "$(dirname "$0")"
source ./lib.sh
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SA="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

for s in sbx-host adk-sbx sbx-internal; do gcloud run services delete "$s" --region "$REGION" --quiet 2>&1 | tail -1; done
for r in m2c-sbx cloud-run-source-deploy; do gcloud artifacts repositories delete "$r" --location "$REGION" --quiet 2>&1 | tail -1; done
for b in "gs://${PROJECT}_cloudbuild" "gs://run-sources-${PROJECT}-${REGION}"; do gcloud storage rm -r "$b" 2>&1 | tail -1; done
for sec in m2c-dummy m2c-adk-key; do gcloud secrets delete "$sec" --quiet 2>&1 | tail -1; done
KEY=$(gcloud services api-keys list --filter='displayName=m2c-adk-test' --format='value(name)')
# a key used in the last 7 days is protected; this is our own temporary key, so override the check
[ -n "$KEY" ] && curl -s -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "x-goog-user-project: $PROJECT" "https://apikeys.googleapis.com/v2/$KEY?checkExistingUsage=SKIP" >/dev/null
for role in roles/cloudbuild.builds.builder roles/aiplatform.user; do
  gcloud projects remove-iam-policy-binding "$PROJECT" --member "$SA" --role "$role" --condition=None --quiet >/dev/null 2>&1
done
echo "--- left over (should all be empty):"
gcloud run services list --format='value(name)'
gcloud artifacts repositories list --location "$REGION" --format='value(name)'
gcloud secrets list --format='value(name)'
