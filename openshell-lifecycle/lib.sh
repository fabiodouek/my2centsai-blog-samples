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
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
os() { openshell -g osh-eks "$@"; }   # -g before the subcommand, never after a -- command
os status >/dev/null 2>&1 || { echo "Gateway not reachable at 127.0.0.1:${LOCAL_PORT}: run ../openshell-eks/05_install.sh (or 09_scale.sh up)" >&2; exit 1; }
say() { printf '\n\033[1m$ %s\033[0m\n' "$*"; }
now() { python3 -c 'import time; print(f"{time.time():.1f}")'; }
since() { python3 -c "import time; print(f'{time.time() - $1:.1f}')"; }
# One line per change: phase, restart count, last exit code, seconds to next restart, generation.
sandbox_state() {
  os sandbox get "$1" -o json 2>/dev/null | jq -r '[.phase, (.restart_count|tostring), (.exit_code // "-" | tostring),
    (if (.next_restart_at_ms // 0) > 0 then "\(((.next_restart_at_ms / 1000) - now) | floor)s" else "-" end),
    (.annotations["internal.openshell.ai/runtime-generation"] // "-" | .[0:8])] | join(" ")'
}
watch_sandbox() {  # name seconds [stop-when-phase-regex]
  local name="$1" secs="$2" stop="${3:-^$}" start last="" cur key
  start=$(now)
  printf '  %6s  %-12s %-8s %-5s %-12s %s\n' "t" "phase" "restarts" "exit" "next restart" "generation"
  while :; do
    cur="$(sandbox_state "$name")"
    [[ -z "$cur" ]] && cur="(gone)"
    key="$(awk '{print $1, $2, $3, $5}' <<<"$cur")"   # ignore the countdown
    if [[ "$key" != "$last" ]]; then
      read -r p r e n g <<<"$cur"
      printf '  %5ss  %-12s %-8s %-5s %-12s %s\n' "$(since "$start")" "$p" "${r:-}" "${e:-}" "${n:-}" "${g:-}"
      last="$key"
    fi
    [[ "${cur%% *}" =~ $stop ]] && break
    python3 -c "import time,sys; sys.exit(0 if time.time() - $start < $secs else 1)" || break
    sleep 1
  done
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
sandbox_id() { os sandbox get "$1" -o json 2>/dev/null | jq -r .id; }
in_sb() { local n="$1"; shift; os sandbox exec "$n" --no-login-shell --no-tty -- "$@"; }
