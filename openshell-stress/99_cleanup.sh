#!/usr/bin/env bash
# Remove everything the part 6 scripts create, and undo what they could have left changed if
# killed hard (kill -9, a closed laptop): p6- sandboxes, the p6-echo provider, the p6-direct and
# p6-load Pods, a global policy they set, and the Helm values they apply (shared supervisor
# pool, push delivery, 1 GiB workspaces). Each script already does this for itself on exit,
# also on Ctrl-C; this is the safety net. It never touches anything not named p6-.
#
#   ./99_cleanup.sh             everything
#   ./99_cleanup.sh --no-helm   leave the Helm release alone (don't even read it)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="${HERE}/../openshell-eks"
[[ -f "${INSTALL}/env.sh" ]] || { echo "${INSTALL}/env.sh not found: run part 2 (../openshell-eks) first" >&2; exit 1; }
# shellcheck source=/dev/null
source "${INSTALL}/env.sh"
NS="openshell"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
os() { openshell -g osh-eks "$@"; }   # -g before the subcommand
os status >/dev/null 2>&1 || { echo "Gateway not reachable at 127.0.0.1:17680: run ../openshell-eks/09_scale.sh up" >&2; exit 1; }

# 1. Sandboxes (in parallel: each delete takes about 30 s), then wait until none is listed.
names=$(os sandbox list -o json 2>/dev/null | jq -r '.sandboxes[]?.name | select(startswith("p6-"))')
for n in $names; do
  os sandbox provider detach "$n" p6-echo >/dev/null 2>&1
  ( os sandbox delete "$n" >/dev/null 2>&1 && echo "  deleted sandbox ${n}" ) &
done
wait

# 1b. Orphans: Sandbox CRs named default--p6-* that the gateway no longer lists (OpenShell 0.1.6
#     can forget a sandbox it was asked to delete right after it became Ready, leaving its
#     Kubernetes objects and EBS volume). Deleting the CR takes the rest with it.
known=$(os sandbox list -o json 2>/dev/null | jq -r '.sandboxes[]?.name')
for cr in $(kubectl -n "$NS" get sandboxes.agents.x-k8s.io -o name 2>/dev/null | sed 's|.*/||' | grep '^default--p6-'); do
  grep -qx "${cr#default--}" <<<"$known" && continue
  kubectl -n "$NS" delete sandboxes.agents.x-k8s.io "$cr" --wait=true --timeout=180s >/dev/null 2>&1 \
    && echo "  deleted orphaned Sandbox CR ${cr}"
done

# 2. The provider (refused while attached, hence after the sandboxes).
# (provider delete exits 0 even when the provider doesn't exist, so look first)
os provider get p6-echo >/dev/null 2>&1 && os provider delete p6-echo >/dev/null 2>&1 && echo "  deleted provider p6-echo"

# 3. The helper Pods.
kubectl -n default delete pod p6-direct --ignore-not-found --wait=false 2>/dev/null | sed 's/^/  /'
kubectl -n "$NS" delete pod p6-load --ignore-not-found --wait=false 2>/dev/null | sed 's/^/  /'

# 4. A global policy from 03 --scope global (its rules are named p6_echo / p6_marker).
if os policy get --global --full -o json 2>/dev/null | jq -e '[.. | objects | keys[]?] | index("p6_echo")' >/dev/null 2>&1; then
  os policy delete --global --yes >/dev/null 2>&1 && echo "  deleted the p6 global policy"
fi

# 5. Helm values from values/*.yaml. Each script rolls back to the exact earlier revision on exit;
#    here, without that revision, the release's current values are taken, the part 6 keys
#    removed (delivery mode set back to an explicit "poll"), and applied with --reset-values.
#    (--reuse-values with --set key=null does not reliably remove a gatewayConfig key: the
#    rendered gateway.toml can keep it.) The rendered gateway.toml is checked afterwards.
#    Changing it restarts the gateway Pod, so the port-forward is restarted too (same pid file
#    and log as ../openshell-lifecycle). The values file holds the ECR registry, so it is
#    written to a private temp file and never printed.
if [[ "${1:-}" != "--no-helm" ]]; then
  vals=$(helm -n "$NS" get values openshell -o json)
  new=$(jq '
    (if .gatewayConfig["openshell.drivers.kubernetes"].supervisor_topology == "gateway"
       then del(.gatewayConfig["openshell.drivers.kubernetes"].supervisor_topology)
          | del(.supervisor.replicas, .supervisor.affinity) else . end)
    | (if .gatewayConfig["openshell.gateway"].config_delivery_mode == "push"
       then .gatewayConfig["openshell.gateway"].config_delivery_mode = "poll" else . end)
    | (if .server.workspaceDefaultStorageSize == "1Gi" then del(.server.workspaceDefaultStorageSize) else . end)' <<<"$vals")
  if [[ "$(jq -S . <<<"$new")" != "$(jq -S . <<<"$vals")" ]]; then
    echo "  reverting part 6 Helm values (shared supervisor pool, push delivery, 1 GiB workspaces)"
    f=$(mktemp); chmod 600 "$f"; printf '%s\n' "$new" >"$f"
    helm upgrade openshell "${INSTALL}/charts/helm-chart-0.1.6.tgz" -n "$NS" --reset-values -f "$f" \
      --wait --timeout 10m >/dev/null
    rm -f "$f"
    kubectl -n "$NS" rollout status statefulset/openshell --timeout=10m >/dev/null
    toml=$(kubectl -n "$NS" get configmap openshell-config -o jsonpath='{.data.gateway\.toml}')
    if grep -qE 'supervisor_topology = "gateway"|config_delivery_mode = "push"|workspace_default_storage_size = "1Gi"' <<<"$toml"; then
      echo "  WARNING: gateway.toml still has a part 6 setting:" >&2
      grep -E 'supervisor_topology|config_delivery_mode|workspace_default_storage_size' <<<"$toml" >&2
    else
      echo "  gateway.toml: no part 6 settings left"
    fi
    pidf="${INSTALL}/.port-forward.pid"
    [[ -f "$pidf" ]] && kill "$(cat "$pidf")" 2>/dev/null
    nohup kubectl -n "$NS" port-forward svc/openshell 17680:8080 >"${INSTALL}/logs/port-forward.log" 2>&1 &
    echo $! >"$pidf"
    disown
    for _ in $(seq 1 30); do os status >/dev/null 2>&1 && break; sleep 1; done
  fi
fi

echo "p6 sandboxes left: $(os sandbox list -o json 2>/dev/null | jq '[.sandboxes[]?.name | select(startswith("p6-"))] | length')"
echo "p6 workspace PVCs left: $(kubectl -n "$NS" get pvc -o name 2>/dev/null | grep -c 'workspace-default--p6-')"
