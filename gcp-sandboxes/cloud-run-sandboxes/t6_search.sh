#!/usr/bin/env bash
# Search Cloud Logging for the canary from t6_logging.sh (run from your laptop).
source "$(dirname "$0")/lib.sh"
echo '$ gcloud logging read "m2c-canary-7731" --freshness=15m'
gcloud logging read '"m2c-canary-7731"' --freshness=15m --order=asc \
  --format='value(logName.basename(), timestamp.date("%H:%M:%S"), textPayload)' | sed 's/%2F/\//g' | cut -c1-170
