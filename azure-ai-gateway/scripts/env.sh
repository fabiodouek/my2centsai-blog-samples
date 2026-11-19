# Shared settings. Source this file: . scripts/env.sh
export AZURE_CORE_ONLY_SHOW_ERRORS=1
export PYTHONWARNINGS=ignore
export RG="${RG:-rg-aigw-demo}"
export LOCATION="${LOCATION:-eastus2}"                 # the gateway
export FOUNDRY_LOCATION="${FOUNDRY_LOCATION:-swedencentral}"  # the Foundry account (see README)
export STATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.state"
mkdir -p "$STATE_DIR"
# Names are random once, then kept in .state so every script sees the same ones.
[ -f "$STATE_DIR/names" ] || echo "GATEWAY=aigw$RANDOM FOUNDRY=aigwf$RANDOM" > "$STATE_DIR/names"
eval "$(tr ' ' '\n' < "$STATE_DIR/names" | sed 's/^/export /')"
