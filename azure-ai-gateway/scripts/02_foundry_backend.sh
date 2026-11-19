#!/usr/bin/env bash
# An Azure OpenAI account with two deployments, imported as one provider (managed identity, no keys),
# an alias "chat", and a gateway API key.
set -euo pipefail
. "$(dirname "$0")/env.sh"
dep() { az cognitiveservices account deployment create -g "$RG" -n "$FOUNDRY" --deployment-name "$1" \
  --model-name "$2" --model-version "$3" --model-format OpenAI --sku-name GlobalStandard --sku-capacity "$4" -o none; }
az cognitiveservices account create -g "$RG" -n "$FOUNDRY" --kind OpenAI --sku S0 \
  -l "$FOUNDRY_LOCATION" --custom-domain "$FOUNDRY" --yes -o none
dep main gpt-4.1-mini 2025-04-14 50   # the real backend
dep alt gpt-4.1-mini 2025-04-14 50   # a second deployment, for the alias retarget test
# (05_failover.py adds a third, with a tiny capacity, for the failover test.)
FID=$(az cognitiveservices account show -g "$RG" -n "$FOUNDRY" --query id -o tsv)
az aigateway model-provider import -g "$RG" --gateway-name "$GATEWAY" --foundry-resource-id "$FID" \
  --query '{provider:providerName, summary:summary}' -o yaml
# Gateway resource IDs of the two models, saved for the other scripts (git-ignored).
python3 "$(dirname "$0")/save_models.py"
MINI=$(python3 -c "import sys; sys.path.insert(0,'$(dirname "$0")'); import common; print(common.model_id('$FOUNDRY','main'))")
az aigateway model-alias set -g "$RG" --gateway-name "$GATEWAY" -n chat --routing-name chat \
  --backend-model resource-id="$MINI" priority=1 weight=100 \
  --query '{alias:properties.aliasName, backends:length(properties.backendModels), url:properties.gatewayEndpoints[0].url}' -o yaml
az aigateway api-key create -g "$RG" --gateway-name "$GATEWAY" -n demo -o none
az aigateway api-key list-secrets -g "$RG" --gateway-name "$GATEWAY" -n demo --query primaryKey -o tsv > "$STATE_DIR/key"
chmod 600 "$STATE_DIR/key"
echo "key saved in .state/key (git-ignored, never printed)"
