#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "$0")/env.sh"
start=$(date +%s)
az aigateway delete -g "$RG" -n "$GATEWAY" --yes
az aigateway wait -g "$RG" -n "$GATEWAY" --deleted
echo "gateway deleted in $(( $(date +%s) - start )) s"
az group delete --name "$RG" --yes
az cognitiveservices account purge -g "$RG" -n "$FOUNDRY" -l "$FOUNDRY_LOCATION" || true
# The labs mention a soft-delete record for gateways. For mine there was none (ServiceNotFound).
az apim deletedservice purge --service-name "$GATEWAY" --location "$LOCATION" || true
rm -rf "$STATE_DIR"
