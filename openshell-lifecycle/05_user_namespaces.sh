#!/usr/bin/env bash
# Test 5: user namespaces. The chart's server.enableUserNamespaces turns on hostUsers: false for
# sandbox workload Pods (Kubernetes user namespaces). UID 10001 inside the Pod
# then maps to an unprivileged range on the node. The setting is put back at the end.
source "$(dirname "$0")/lib.sh"
NAME="userns"
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
set_userns() {
  helm upgrade openshell "${INSTALL}/charts/helm-chart-0.1.6.tgz" -n "$NS" --reuse-values \
    --set "server.enableUserNamespaces=$1" --wait --timeout 10m >/dev/null
  kubectl -n "$NS" rollout restart statefulset/openshell >/dev/null
  restart_pf
}
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; set_userns false; echo "  server.enableUserNamespaces back to false"; }
trap cleanup EXIT
os sandbox delete "$NAME" >/dev/null 2>&1

say "helm upgrade openshell --reuse-values --set server.enableUserNamespaces=true"
set_userns true
os sandbox create --name "$NAME" --detach --no-tty >/dev/null 2>&1 || true
echo "  sandbox: $(sandbox_state "$NAME" | awk '{print $1}')"
echo "  workload Pod hostUsers: $(kubectl -n "$NS" get pod "default--${NAME}" -o jsonpath='{.spec.hostUsers}' 2>/dev/null || echo '?')"
say "inside the sandbox"
in_sb "$NAME" sh -c 'echo "  id: $(id)"; echo "  /proc/self/uid_map: $(tr -s " " < /proc/self/uid_map)"' 2>&1
