# Shared by the scripts in this directory. The cluster and gateway come from ../openshell-eks
# (part 2): its env.sh, the kubectl context it created, and the CLI gateway osh-eks.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL="${HERE}/../openshell-eks"
[[ -f "${INSTALL}/env.sh" ]] || { echo "${INSTALL}/env.sh not found: run part 2 (../openshell-eks) first" >&2; exit 1; }
# shellcheck source=/dev/null
source "${INSTALL}/env.sh"
NS="openshell"
LOCAL_PORT="${LOCAL_PORT:-17680}"   # part 2's port-forward; set it as you did for 05_install.sh
ECHO_HOST="echo.echo.svc.cluster.local"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
os() { openshell -g osh-eks "$@"; }   # -g before the subcommand, never after a -- command
os status >/dev/null 2>&1 || { echo "Gateway not reachable at 127.0.0.1:${LOCAL_PORT}: run ../openshell-eks/05_install.sh (or 09_scale.sh up)" >&2; exit 1; }
say() { printf '\n\033[1m$ %s\033[0m\n' "$*"; }
now() { python3 -c 'import time; print(f"{time.time():.1f}")'; }
since() { python3 -c "import time; print(f'{time.time() - $1:.1f}')"; }
nocolor() { sed 's/\x1b\[[0-9;]*m//g'; }
sandbox_id() { os sandbox get "$1" -o json 2>/dev/null | jq -r .id; }
in_sb() { local n="$1"; shift; os sandbox exec "$n" --no-login-shell --no-tty -- "$@"; }
# Create a sandbox and wait until it can run commands. Extra arguments go to sandbox create.
make_sandbox() {
  local n="$1"; shift
  os sandbox delete "$n" >/dev/null 2>&1
  os sandbox create --name "$n" --detach --no-tty "$@" >/dev/null 2>&1 || { echo "sandbox create ${n} failed" >&2; return 1; }
  for _ in $(seq 1 30); do in_sb "$n" true >/dev/null 2>&1 && return 0; sleep 2; done
  echo "sandbox ${n} not usable" >&2; return 1
}
# OCSF lines from `openshell logs`, without the timestamp and source prefix.
ocsf() { os logs "$1" -n "${2:-200}" 2>/dev/null | nocolor | grep '\[OCSF' | sed -E 's/^\[[0-9.]+\] \[[a-z]+\] +\[OCSF *\] \[ocsf\] //'; }
# Import a provider profile from profiles/, or update it if the gateway already has it.
import_profile() {
  os profile import -f "${HERE}/profiles/$1.yaml" >/dev/null 2>&1 || os profile update -f "${HERE}/profiles/$1.yaml" >/dev/null 2>&1 || true
}
# Restart the port-forward to the gateway (after anything that restarts the gateway Pod).
restart_pf() {
  local pidf="${INSTALL}/.port-forward.pid"
  [[ -f "$pidf" ]] && { kill "$(cat "$pidf")" 2>/dev/null; wait "$(cat "$pidf")" 2>/dev/null; }
  kubectl -n "$NS" rollout status statefulset/openshell --timeout=5m >/dev/null
  nohup kubectl -n "$NS" port-forward svc/openshell "${LOCAL_PORT}:8080" >"${INSTALL}/logs/port-forward.log" 2>&1 &
  echo $! >"$pidf"
  disown  # no job-control message when a later run stops it
  for _ in $(seq 1 30); do os status >/dev/null 2>&1 && return 0; sleep 1; done
  echo "gateway not reachable after restarting the port-forward" >&2; return 1
}
# Switch how supervisors receive policy: poll (the default, every 10 s) or push (over the
# supervisor session). A gatewayConfig change rolls the gateway Pod. Revert with "poll": in my
# runs, --set ...=null with --reuse-values left "push" in the rendered gateway.toml.
# The chart wants the TOML table as one dotted key, gatewayConfig."openshell.gateway": a nested
# gatewayConfig.openshell.gateway map fails with "defines "openshell.gateway" both as a nested
# map and a dotted top-level table".
set_delivery() {
  command helm --kube-context "$KUBE_CONTEXT" upgrade openshell "${INSTALL}/charts/helm-chart-0.1.6.tgz" -n "$NS" \
    --reuse-values --set "gatewayConfig.openshell\\.gateway.config_delivery_mode=$1" --wait --timeout 10m >/dev/null && restart_pf
}
