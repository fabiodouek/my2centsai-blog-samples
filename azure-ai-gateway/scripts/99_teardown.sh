#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "$0")/env.sh"
GATEWAY=$(cat "$STATE_DIR/gateway"); FOUNDRY=$(cat "$STATE_DIR/foundry")
az aigateway delete -g "$RG" -n "$GATEWAY" --yes
az aigateway wait -g "$RG" -n "$GATEWAY" --deleted
az group delete --name "$RG" --yes
az cognitiveservices account purge -g "$RG" -n "$FOUNDRY" --location "$LOCATION" || true
az apim deletedservice purge --service-name "$GATEWAY" --location "$LOCATION" || true
rm -rf "$STATE_DIR"
