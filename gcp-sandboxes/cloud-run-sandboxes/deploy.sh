#!/usr/bin/env bash
# Build the host image with Cloud Build and deploy a PRIVATE Cloud Run service with
# 2 vCPU / 4 GiB, gen2, min = max = 1, CPU always allocated, plus the dummy secret.
# Sandboxes are NOT on yet: t1_enable.sh turns them on.
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh
REPO=m2c-sbx
IMAGE=$REGION-docker.pkg.dev/$PROJECT/$REPO/$SERVICE:1

gcloud artifacts repositories describe $REPO --location "$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create $REPO --repository-format=docker --location "$REGION"

# A dummy secret (never put a real one here)
gcloud secrets describe m2c-dummy >/dev/null 2>&1 ||
  printf 'm2c-secret-canary-4417' | gcloud secrets create m2c-dummy --data-file=-
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding m2c-dummy --quiet \
  --member "serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor >/dev/null

# Cloud Build runs as the default compute service account in new projects; let it build and push
for role in roles/cloudbuild.builds.builder; do
  gcloud projects add-iam-policy-binding "$PROJECT" --quiet --condition=None \
    --member "serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role $role >/dev/null
done

gcloud builds submit host --tag "$IMAGE" --quiet

# Sandboxes are switched on afterwards, in t1_enable.sh (test 1)
gcloud run deploy "$SERVICE" --image "$IMAGE" --no-allow-unauthenticated \
  --cpu 2 --memory 4Gi --execution-environment gen2 \
  --min-instances 1 --max-instances 1 --concurrency 10 --no-cpu-throttling --timeout 1800 \
  --set-env-vars PLAIN=visible \
  --set-secrets DUMMY_SECRET=m2c-dummy:latest,/secrets/dummy=m2c-dummy:latest
