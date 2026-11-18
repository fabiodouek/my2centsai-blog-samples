#!/usr/bin/env bash
# Foundry account + gpt-5-mini, imported as a provider (managed identity, no keys), alias, gateway key.
set -euo pipefail
. "$(dirname "$0")/env.sh"
GATEWAY=$(cat "$STATE_DIR/gateway"); FOUNDRY=$(cat "$STATE_DIR/foundry")
az cognitiveservices account create -g "$RG" -n "$FOUNDRY" --kind AIServices --sku S0 \
  --location "$LOCATION" --custom-domain "$FOUNDRY" -o none
az cognitiveservices account deployment create -g "$RG" -n "$FOUNDRY" --deployment-name "$DEPLOYMENT" \
  --model-name gpt-5-mini --model-version 2025-08-07 --model-format OpenAI \
  --sku-name GlobalStandard --sku-capacity 10 -o none
FOUNDRY_ID=$(az cognitiveservices account show -g "$RG" -n "$FOUNDRY" --query id -o tsv)
az aigateway model-provider import -g "$RG" --gateway "$GATEWAY" --foundry-resource-id "$FOUNDRY_ID"
# The alias maps "chat" to the imported model. Check the flags with: az aigateway model-alias set --help
MODEL_ID=$(az aigateway model list -g "$RG" --gateway "$GATEWAY" --query "[?contains(name,'gpt-5-mini')].id | [0]" -o tsv)
echo "$MODEL_ID" > "$STATE_DIR/model_id"
az aigateway model-alias set -g "$RG" --gateway "$GATEWAY" --name "$ALIAS" \
  --backend-model resource-id="$MODEL_ID" priority=1 weight=100
az aigateway api-key create -g "$RG" --gateway "$GATEWAY" --name demo -o none
az aigateway api-key list-secrets -g "$RG" --gateway "$GATEWAY" --name demo --query primaryKey -o tsv > "$STATE_DIR/key"
chmod 600 "$STATE_DIR/key"
az aigateway show -g "$RG" -n "$GATEWAY" --query properties.frontend.defaultHostname -o tsv > "$STATE_DIR/host"
echo "alias $ALIAS ready; key saved in $STATE_DIR/key (git-ignored, never printed)"
