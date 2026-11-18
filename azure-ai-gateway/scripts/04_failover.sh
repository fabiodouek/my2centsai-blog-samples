#!/usr/bin/env bash
# Puts a dead custom provider at priority 0 under the alias, then calls it 20 times.
set -euo pipefail
. "$(dirname "$0")/env.sh"
GATEWAY=$(cat "$STATE_DIR/gateway")
# Dead backend: a port nothing listens on, via a custom OpenAI-compatible provider.
az aigateway model-provider create -g "$RG" --gateway "$GATEWAY" --name dead --kind Custom \
  --endpoint https://127.0.0.1:9 --api-key-header Authorization --api-key "Bearer x"
az aigateway model create -g "$RG" --gateway "$GATEWAY" --provider dead --name dead-model \
  --model-name dead-model --supported-endpoints /v1/chat/completions
DEAD_ID=$(az aigateway model list -g "$RG" --gateway "$GATEWAY" --query "[?name=='dead-model'].id | [0]" -o tsv)
MODEL_ID=$(cat "$STATE_DIR/model_id")
az aigateway model-alias set -g "$RG" --gateway "$GATEWAY" --name "$ALIAS" \
  --backend-model resource-id="$DEAD_ID" priority=0 weight=100 \
  --backend-model resource-id="$MODEL_ID" priority=1 weight=100
python3 "$(dirname "$0")/03_first_call.py" --repeat 20 --quiet
