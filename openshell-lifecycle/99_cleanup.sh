#!/usr/bin/env bash
# Remove everything this directory's tests create, and undo anything they could have left
# changed if interrupted: cordoned nodes, user namespaces switched on.
source "$(dirname "$0")/lib.sh"
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
for n in rp-never rp-onfail rp-always life oneshot persist sup iso userns; do
  os sandbox delete "$n" >/dev/null 2>&1 && echo "  deleted ${n}" &
done
wait
for node in $(kubectl get nodes -o json | jq -r '.items[] | select(.spec.unschedulable == true) | .metadata.name'); do
  kubectl uncordon "$node" >/dev/null && echo "  uncordoned ${node}"
done
if [[ "$(helm -n "$NS" get values openshell -o json | jq -r '.server.enableUserNamespaces // false')" == true ]]; then
  helm upgrade openshell "${INSTALL}/charts/helm-chart-0.1.6.tgz" -n "$NS" --reuse-values \
    --set server.enableUserNamespaces=false --wait --timeout 10m >/dev/null
  kubectl -n "$NS" rollout restart statefulset/openshell >/dev/null
  restart_pf && echo "  server.enableUserNamespaces back to false"
fi
echo "sandboxes left: $(os sandbox list -o json 2>/dev/null | jq '.sandboxes | length')"
