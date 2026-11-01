#!/usr/bin/env bash
# The fence before OpenShell. OpenShell's Kubernetes driver puts every sandbox workload behind
# a NetworkPolicy with no egress except to its own supervisor, and trusts the CNI to enforce
# it. This script checks what the VPC CNI actually enforces, in namespace fence-test, with
# plain pods and the same kind of policy (manifests/fence-test/):
#
#   1. enforcement: a pod under a deny-all egress policy can't reach the internet or DNS;
#      the same pod without the policy can
#   2. startup window: in the default "standard" mode, how many requests a new pod under the
#      deny-all policy completes before the policy takes effect (10 pods sharing a name
#      prefix, then 10 pods each with its own prefix, as OpenShell sandboxes have)
#   3. node IPs: whether a pod under the deny-all policy can reach a server on its own node's
#      IP and on the other node's IP
#   4. strict mode: the same startup test with NETWORK_POLICY_ENFORCING_MODE=strict, and a
#      fresh pod that no policy selects; then back to standard mode
#
# Needs: ./01_cluster.sh and ./03_images.sh (the workload image). Everything it creates is in
# namespace fence-test, deleted at the end. The VPC CNI add-on is always left in standard mode,
# with the configuration Terraform set, also when the script stops early.
set -euo pipefail
cd "$(dirname "$0")"

[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }

NS="fence-test"
M="manifests/fence-test"
IMAGE="${ECR_REGISTRY}/${ECR_PREFIX}/workload:0.1.6"
WINDOW_PODS=10
# Must stay byte-for-byte what Terraform sets (jsonencode), or the next apply sees a change.
ADDON_STANDARD='{"enableNetworkPolicy":"true"}'
ADDON_STRICT='{"enableNetworkPolicy":"true","env":{"NETWORK_POLICY_ENFORCING_MODE":"strict"}}'

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
verdict() { printf '  >> %s\n' "$*"; }

# --- helpers ----------------------------------------------------------------------
client() {  # name fence node command
  NAME="$1" FENCE="$2" NODE="$3" COMMAND="$4" IMAGE="$IMAGE" \
    envsubst '${NAME} ${FENCE} ${NODE} ${COMMAND} ${IMAGE}' <"$M/client-pod.yaml" | kubectl apply -f - >/dev/null
}
ready() { kubectl -n "$NS" wait --for=condition=Ready "pod/$1" --timeout=180s >/dev/null; }
reach() {  # pod url -> open | blocked
  if kubectl -n "$NS" exec "$1" -- curl -s -m 5 -o /dev/null "$2" 2>/dev/null; then echo open; else echo blocked; fi
}
dns() {  # pod -> resolves | blocked
  if kubectl -n "$NS" exec "$1" -- getent hosts kubernetes.default.svc.cluster.local >/dev/null 2>&1; then echo resolves; else echo blocked; fi
}

addon_mode() {  # standard | strict: update the add-on and wait for aws-node to roll out
  local cfg id status
  if [[ "$1" == strict ]]; then cfg="$ADDON_STRICT"; else cfg="$ADDON_STANDARD"; fi
  if [[ "$(aws eks describe-addon --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" --addon-name vpc-cni \
          --query addon.configurationValues --output text)" == "$cfg" ]]; then
    echo "  vpc-cni already in $1 mode"
    return
  fi
  id="$(aws eks update-addon --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" --addon-name vpc-cni \
    --configuration-values "$cfg" --resolve-conflicts PRESERVE --query update.id --output text)"
  local start=$SECONDS
  while :; do
    status="$(aws eks describe-update --region "$AWS_REGION" --name "$CLUSTER_NAME" --addon-name vpc-cni \
      --update-id "$id" --query update.status --output text)"
    [[ "$status" == Successful ]] && break
    if [[ "$status" == Failed || "$status" == Cancelled ]]; then
      echo "vpc-cni update ${id} ${status}" >&2; return 1
    fi
    sleep 10
  done
  kubectl -n kube-system rollout status daemonset/aws-node --timeout=5m >/dev/null
  echo "  vpc-cni now in $1 mode ($((SECONDS - start)) s, aws-node rolled out)"
}

switched=false
finish() {
  local rc=$?
  if [[ "$switched" == true ]]; then
    echo "putting the VPC CNI back in standard mode" >&2
    addon_mode standard || echo "COULD NOT RESTORE: set vpc-cni configuration to ${ADDON_STANDARD}" >&2
  fi
  kubectl delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  exit "$rc"
}
trap finish EXIT

# window <prefix> <fence>: start WINDOW_PODS pods whose first job is 40 requests 100 ms apart,
# and report how many succeeded before the first one failed.
window() {
  local prefix="$1" fence="$2" i node leaked=0 pods_leaking=0 max=0 out n
  local cmd='for i in $(seq 1 40); do if curl -s -m 1 -o /dev/null http://checkip.amazonaws.com; then echo ok; else echo no; fi; sleep 0.1; done; echo done; sleep infinity'
  for ((i = 1; i <= WINDOW_PODS; i++)); do
    node="${NODES[$(( i % 2 ))]}"
    client "${prefix}-${i}" "$fence" "$node" "$cmd"
  done
  for ((i = 1; i <= WINDOW_PODS; i++)); do
    for _ in $(seq 1 90); do
      out="$(kubectl -n "$NS" logs "${prefix}-${i}" 2>/dev/null || true)"
      grep -q '^done$' <<<"$out" && break
      sleep 2
    done
    n="$(awk '/^no$/ {exit} /^ok$/ {c++} END {print c+0}' <<<"$out")"
    if (( n > 0 )); then pods_leaking=$((pods_leaking + 1)); leaked=$((leaked + n)); fi
    if (( n > max )); then max=$n; fi
    printf '  %-16s %2d request(s) out before the policy applied\n' "${prefix}-${i}" "$n"
  done
  verdict "${pods_leaking} of ${WINDOW_PODS} pods got requests out before the deny-all policy applied (${leaked} in all, at most ${max} per pod)"
}

# --- setup --------------------------------------------------------------------------
read -r -a NODES <<<"$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}')"
(( ${#NODES[@]} == 2 )) || { echo "expected 2 nodes, found ${#NODES[@]}" >&2; exit 1; }
IP0="$(kubectl get node "${NODES[0]}" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')"
IP1="$(kubectl get node "${NODES[1]}" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')"

say "setup: namespace ${NS}, deny-all-egress policy, listeners on both nodes"
kubectl delete namespace "$NS" --ignore-not-found --wait=true --timeout=3m >/dev/null
kubectl apply -f "$M/namespace.yaml" -f "$M/deny-all-egress.yaml" >/dev/null
for i in 0 1; do
  NODE="${NODES[$i]}" SUFFIX="$i" IMAGE="$IMAGE" envsubst '${NODE} ${SUFFIX} ${IMAGE}' <"$M/node-listener.yaml" | kubectl apply -f - >/dev/null
done
client open-client open "${NODES[0]}" "sleep infinity"
client deny-client deny "${NODES[0]}" "sleep infinity"
for p in listener-0 listener-1 open-client deny-client; do ready "$p"; done
sleep 5
echo "  nodes: ${NODES[0]} (${IP0}), ${NODES[1]} (${IP1})"

# --- 1. enforcement -------------------------------------------------------------------
say "1. enforcement: deny-all egress vs no policy"
printf '  %-12s internet %-8s cluster DNS %s\n' "open-client" "$(reach open-client http://checkip.amazonaws.com)" "$(dns open-client)"
printf '  %-12s internet %-8s cluster DNS %s\n' "deny-client" "$(reach deny-client http://checkip.amazonaws.com)" "$(dns deny-client)"
if [[ "$(reach deny-client http://checkip.amazonaws.com)" == blocked && "$(reach open-client http://checkip.amazonaws.com)" == open ]]; then
  verdict "the VPC CNI enforces NetworkPolicy egress"
else
  verdict "NOT ENFORCED: the deny-all pod reached the internet, or the open pod didn't"
fi

# --- 2. startup window, standard mode -------------------------------------------------
say "2. startup window, standard mode (${WINDOW_PODS} new pods under deny-all)"
window win-std deny

# The VPC CNI's network policy agent shares its eBPF maps between pods whose names match up to
# the last "-": win-std-2 to win-std-10 reuse what win-std-1 got. OpenShell names every
# workload Pod after its sandbox, so each sandbox is a "first" pod. Same test, a new prefix
# for every pod:
say "2b. startup window, standard mode, every pod with its own name prefix"
window_unique() {
  local i leaked=0 pods_leaking=0 out n
  local cmd='for i in $(seq 1 40); do if curl -s -m 1 -o /dev/null http://checkip.amazonaws.com; then echo ok; else echo no; fi; sleep 0.1; done; echo done; sleep infinity'
  for ((i = 1; i <= WINDOW_PODS; i++)); do client "u${i}x-probe" deny "${NODES[$(( i % 2 ))]}" "$cmd"; done
  for ((i = 1; i <= WINDOW_PODS; i++)); do
    for _ in $(seq 1 90); do
      out="$(kubectl -n "$NS" logs "u${i}x-probe" 2>/dev/null || true)"
      grep -q '^done$' <<<"$out" && break
      sleep 2
    done
    n="$(awk '/^no$/ {exit} /^ok$/ {c++} END {print c+0}' <<<"$out")"
    if (( n > 0 )); then pods_leaking=$((pods_leaking + 1)); leaked=$((leaked + n)); fi
    printf '  %-16s %2d request(s) out before the policy applied\n' "u${i}x-probe" "$n"
  done
  verdict "${pods_leaking} of ${WINDOW_PODS} pods with unique name prefixes got requests out (${leaked} in all)"
}
window_unique

# --- 3. node IPs ----------------------------------------------------------------------
say "3. node IPs from a pod under deny-all (listener on port 18080 of each node)"
own="$(reach deny-client "http://${IP0}:18080/")"
other="$(reach deny-client "http://${IP1}:18080/")"
control="$(reach open-client "http://${IP1}:18080/")"
printf '  deny-client -> own node   %-15s %s\n' "${IP0}:18080" "$own"
printf '  deny-client -> other node %-15s %s\n' "${IP1}:18080" "$other"
printf '  open-client -> other node %-15s %s (control)\n' "${IP1}:18080" "$control"
if [[ "$own" == open && "$other" == blocked ]]; then
  verdict "a fenced pod can still reach its own node's IP on any port; other nodes are blocked"
else
  verdict "own node ${own}, other node ${other}"
fi

# --- 4. strict mode ---------------------------------------------------------------------
say "4. strict mode"
switched=true
addon_mode strict
window win-strict deny
client nopolicy open "${NODES[1]}" "sleep infinity"
ready nopolicy
printf '  %-12s (no policy selects it) internet %s\n' "nopolicy" "$(reach nopolicy http://checkip.amazonaws.com)"
verdict "in strict mode a new pod no policy selects starts with all traffic denied"
addon_mode standard
switched=false
not_ready="$(kubectl -n kube-system get pods -o json | jq -r '[.items[]
  | select(.status.phase != "Succeeded")
  | select(any(.status.conditions[]?; .type == "Ready" and .status != "True")) | .metadata.name] | join(", ")')"
if [[ -z "$not_ready" ]]; then
  verdict "back in standard mode; every kube-system pod Ready"
else
  verdict "back in standard mode; kube-system pods not Ready: ${not_ready}"
fi
echo
echo "deleting namespace ${NS}"
