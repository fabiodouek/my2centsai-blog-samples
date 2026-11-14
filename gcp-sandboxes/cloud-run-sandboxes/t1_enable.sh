#!/usr/bin/env bash
# Test 1: turning sandboxes on. The console only shows the setting; the flag exists under `gcloud beta` only.
# Run it against a service that was deployed WITHOUT sandboxes (deploy.sh does that).
cd "$(dirname "$0")"
source ./lib.sh
quiet() { grep -v -E 'Service URL|Proxy locally'; }   # keep the project number out of the output
pick()  { grep -E 'sandbox|launch-stage|executionEnvironment|execution-environment'; }

echo '$ gcloud run services update --sandbox-launcher      # the GA command track'
gcloud run services update "$SERVICE" --sandbox-launcher 2>&1 | grep -v '^$' | head -4

echo; echo '$ gcloud run services describe --format export   # before'
gcloud run services describe "$SERVICE" --format export | pick

echo; echo '$ gcloud beta run services update --sandbox-launcher'
gcloud beta run services update "$SERVICE" --sandbox-launcher 2>&1 | quiet

echo; echo '$ gcloud run services describe --format export   # after'
gcloud run services describe "$SERVICE" --format export | pick
