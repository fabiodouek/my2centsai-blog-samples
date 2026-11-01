#!/usr/bin/env bash
# Installs the OpenShell CLI and local gateway pinned to 0.1.6 on an Apple Silicon Mac,
# with telemetry off, and builds the workload image the other scripts use.
set -euo pipefail
cd "$(dirname "$0")"

VERSION="0.1.6"
WORKLOAD_IMAGE="openshell-workload:${VERSION}"
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateway.env"

step() { printf '\n== %s\n' "$*"; }

gateway_healthy() {
  command -v openshell >/dev/null 2>&1 \
    && openshell --version 2>/dev/null | grep -q " ${VERSION}" \
    && openshell status >/dev/null 2>&1
}

step "Telemetry off (the Homebrew service reads ${ENV_FILE} before it starts the gateway)"
mkdir -p "$(dirname "$ENV_FILE")"
if ! grep -qx 'OPENSHELL_TELEMETRY_ENABLED=false' "$ENV_FILE" 2>/dev/null; then
  echo 'OPENSHELL_TELEMETRY_ENABLED=false' >>"$ENV_FILE"
fi
cat "$ENV_FILE"

step "Gateway config: Docker supervisor in bridge mode (see gateway.toml)"
CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateway.toml"
restart_needed=false
if [ -f "$CONFIG_FILE" ] && ! cmp -s gateway.toml "$CONFIG_FILE"; then
  echo "${CONFIG_FILE} exists and differs from ./gateway.toml; leaving it alone." >&2
  echo "Add supervisor_network_mode = \"bridge\" under [openshell.drivers.docker] yourself." >&2
elif [ ! -f "$CONFIG_FILE" ]; then
  cp gateway.toml "$CONFIG_FILE"
  restart_needed=true
fi
grep -n 'supervisor_network_mode' "$CONFIG_FILE" || true

step "Pinning traps"
latest=$(curl -fsSI https://github.com/NVIDIA/OpenShell/releases/latest \
  | awk 'tolower($1) == "location:" {print $2}' | tr -d '\r' | awk -F/ '{print $NF}')
echo "install.sh without OPENSHELL_VERSION installs releases/latest, today: ${latest}"
token=$(curl -fsS "https://ghcr.io/token?scope=repository:nvidia/openshell/gateway:pull" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
for tag in "v${VERSION}" "${VERSION}"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    -H 'Accept: application/vnd.oci.image.index.v1+json' \
    "https://ghcr.io/v2/nvidia/openshell/gateway/manifests/${tag}")
  echo "ghcr.io/nvidia/openshell/gateway:${tag} -> HTTP ${code}"
done

step "Install OpenShell ${VERSION}"
if gateway_healthy; then
  echo "gateway already running ($(openshell --version))"
else
  if command -v openshell >/dev/null 2>&1 && ! openshell --version | grep -q " ${VERSION}"; then
    echo "found $(openshell --version): 0.0.x installs can't be upgraded in place." >&2
    echo "Delete their sandboxes and uninstall first (docs: Upgrade to 0.1.0), then re-run." >&2
    exit 1
  fi
  curl -LsSf "https://raw.githubusercontent.com/NVIDIA/OpenShell/v${VERSION}/install.sh" \
    | OPENSHELL_VERSION="v${VERSION}" sh
fi

if [ "$restart_needed" = true ] && command -v brew >/dev/null 2>&1 && brew services list | grep -q '^openshell .*started'; then
  echo "restarting the gateway to load ${CONFIG_FILE}"
  brew services restart openshell >/dev/null
fi

step "Wait for the gateway"
for _ in $(seq 1 60); do
  openshell status >/dev/null 2>&1 && break
  sleep 2
done
openshell --version
openshell status

step "Telemetry check (environment of the running gateway process)"
pid=$(pgrep -f 'bin/openshell-gateway' | head -1 || true)
if [ -n "$pid" ] && ps eww -p "$pid" | tr ' ' '\n' | grep -qx 'OPENSHELL_TELEMETRY_ENABLED=false'; then
  echo "telemetry: disabled (pid ${pid})"
else
  echo "telemetry: NOT confirmed disabled; check ${ENV_FILE} and run: brew services restart openshell" >&2
  exit 1
fi

step "Workload image ${WORKLOAD_IMAGE}"
docker build -q -t "$WORKLOAD_IMAGE" workload/
docker image inspect "$WORKLOAD_IMAGE" --format '{{.Id}} {{.Os}}/{{.Architecture}}'
