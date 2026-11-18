#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "$0")/env.sh"
echo "$GATEWAY" > "$STATE_DIR/gateway"; echo "$FOUNDRY" > "$STATE_DIR/foundry"
az group create --name "$RG" --location "$LOCATION" -o none
start=$(date +%s)
az aigateway create --resource-group "$RG" --name "$GATEWAY" --location "$LOCATION" \
  --system-assigned true --no-wait
az aigateway wait --resource-group "$RG" --name "$GATEWAY" --created
echo "created in $(( $(date +%s) - start )) s"
az aigateway show --resource-group "$RG" --name "$GATEWAY" \
  --query '{name:name, host:properties.frontend.defaultHostname, state:properties.provisioningState}' -o json
