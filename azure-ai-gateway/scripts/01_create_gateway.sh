#!/usr/bin/env bash
# Resource group, then the gateway: --no-wait to start it, wait to time it.
set -euo pipefail
. "$(dirname "$0")/env.sh"
az group create --name "$RG" --location "$LOCATION" -o none
start=$(date +%s)
az aigateway create -g "$RG" -n "$GATEWAY" -l "$LOCATION" --system-assigned true --no-wait
az aigateway wait -g "$RG" -n "$GATEWAY" --created
echo "gateway $GATEWAY created in $(( $(date +%s) - start )) s"
az aigateway show -g "$RG" -n "$GATEWAY" \
  --query '{type:type, location:location, sku:sku.name, state:properties.provisioningState, host:properties.frontend.defaultHostname}' -o yaml
az aigateway show -g "$RG" -n "$GATEWAY" --query properties.frontend.defaultHostname -o tsv > "$STATE_DIR/host"
