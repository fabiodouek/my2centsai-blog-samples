#!/usr/bin/env bash
# Test 3: losing a Pod. First the trusted half (the supervisor Pod), then the untrusted half
# (the workload Pod). OpenShell lets only the same supervisor process resume a sandbox, so a lost
# supervisor can't be replaced in place.
source "$(dirname "$0")/lib.sh"
NAME="sup"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
pods() {
  kubectl -n "$NS" get pods -l "openshell.ai/boundary-pair=$(sandbox_id "$NAME")" --no-headers \
    -o custom-columns='POD:.metadata.name,ROLE:.metadata.labels.openshell\.ai/boundary-role,STATUS:.status.phase' 2>/dev/null \
    | sed 's/^/  /' | grep . || echo "  (no pods)"
}
recover() {  # start a stopped (or failed) sandbox again and read the workspace marker
  local t
  t=$(now)
  [[ "$(sandbox_state "$NAME" | awk '{print $1}')" == Stopped ]] || { os sandbox stop "$NAME" >/dev/null 2>&1; watch_sandbox "$NAME" 60 '^Stopped$' >/dev/null; }
  os sandbox start "$NAME" >/dev/null 2>&1
  watch_sandbox "$NAME" 180 '^Ready$' >/dev/null
  echo "  started again: $(sandbox_state "$NAME" | awk '{print $1}') after $(since "$t") s, generation $(sandbox_state "$NAME" | awk '{print $5}')"
  echo "  /sandbox/marker: $(in_sb "$NAME" cat /sandbox/marker 2>/dev/null || echo unreadable)"
}

os sandbox create --name "$NAME" --detach --no-tty >/dev/null 2>&1
in_sb "$NAME" sh -c 'echo written-before > /sandbox/marker'
pods

id="$(sandbox_id "$NAME")"
sup="$(kubectl -n "$NS" get pods -l "openshell.ai/boundary-pair=${id},openshell.ai/boundary-role=supervisor" -o jsonpath='{.items[0].metadata.name}')"
say "kubectl delete pod ${sup}   (the supervisor)"
kubectl -n "$NS" delete pod "$sup" --wait=false >/dev/null
watch_sandbox "$NAME" 150 '^(Stopped|Error)$' &
w=$!
sleep 8; printf '  exec while the supervisor is gone: '; in_sb "$NAME" true >/dev/null 2>&1 && echo ok || echo refused
wait "$w"
pods
say "openshell sandbox start ${NAME}"
recover

say "kubectl delete pod default--${NAME}   (the workload)"
kubectl -n "$NS" delete pod "default--${NAME}" --wait=false >/dev/null
watch_sandbox "$NAME" 120 '^(Stopped|Error)$'
pods
if [[ "$(sandbox_state "$NAME" | awk '{print $1}')" != Ready ]]; then
  say "openshell sandbox stop ${NAME} && openshell sandbox start ${NAME}"
  recover
fi
