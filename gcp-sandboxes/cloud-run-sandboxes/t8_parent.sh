#!/usr/bin/env bash
# Test 8c: what happens to a running sandbox when its Cloud Run instance is replaced?
# Starts a detached `sleep 1h` sandbox, deploys a new revision (which replaces the instance), then reads
# the OLD revision's logs for what happened to the sandbox. Run from your laptop.
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

OLD=$(gcloud run services describe "$SERVICE" --format='value(status.latestReadyRevisionName)')
cat > /tmp/t8_orphan.sh <<'EOS'
SB=/usr/local/gcp/bin/sandbox
$SB delete --force orphan >/dev/null 2>&1
$SB run orphan --detach -- /bin/sleep 3600 >/dev/null 2>&1
echo "sandbox processes on this instance: $(ps -eo args | grep -c '^runsc-sandbox')"
EOS
echo "== start a detached 1 h sandbox on revision $OLD"
run_on_instance /tmp/t8_orphan.sh 60 | grep -v '^\[exit'

echo; echo "== deploy a new revision (any change does it)"
gcloud run services update "$SERVICE" --update-env-vars "REPLACED_AT=$(date +%s)" 2>&1 | grep -E 'revision|Done'
NEW=$(gcloud run services describe "$SERVICE" --format='value(status.latestReadyRevisionName)')
echo "old: $OLD   new: $NEW"

sleep 90
echo; echo "== what the OLD revision logged around its shutdown (system log and sandbox log)"
gcloud logging read "resource.labels.revision_name=$OLD AND (logName:\"varlog%2Fsystem\" OR logName:\"sandbox.log\") AND timestamp>=\"$(date -u -v-6M +%FT%TZ 2>/dev/null || date -u -d '-6 min' +%FT%TZ)\"" \
  --order=asc --format='value(timestamp.date("%H:%M:%S"), textPayload)' | cut -c1-200 | tail -12
