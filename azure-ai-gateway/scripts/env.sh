# Shared settings. Source this file: . scripts/env.sh
export RG="${RG:-rg-aigw-demo}"
export LOCATION="${LOCATION:-eastus2}"
export GATEWAY="${GATEWAY:-aigw-demo-$RANDOM}"   # 1-24 chars, globally unique
export FOUNDRY="${FOUNDRY:-aigw-foundry-$RANDOM}"
export DEPLOYMENT="${DEPLOYMENT:-gpt-5-mini}"
export ALIAS="${ALIAS:-chat}"
export STATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.state"
mkdir -p "$STATE_DIR"
