#!/usr/bin/env bash
# Test 2: what survives. A file in the workspace (/sandbox, an EBS volume), a file in /tmp and a
# running process, across stop and start. Then the zone-bound volume: the sandbox is started
# again while its node is cordoned and the only other node is in another availability zone.
source "$(dirname "$0")/lib.sh"
NAME="persist"
node=""
cleanup() { [[ -n "$node" ]] && kubectl uncordon "$node" >/dev/null 2>&1; os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
os sandbox delete "$NAME" >/dev/null 2>&1

say "openshell sandbox create --name ${NAME}; write two files, start a process"
os sandbox create --name "$NAME" --detach --no-tty >/dev/null 2>&1
in_sb "$NAME" sh -c 'echo kept > /sandbox/keep.txt; echo lost > /tmp/gone.txt; nohup sleep 3600 >/dev/null 2>&1 & echo "  background sleep pid $!"'

say "openshell sandbox stop ${NAME} && openshell sandbox start ${NAME}"
t=$(now); os sandbox stop "$NAME" >/dev/null 2>&1; echo "  stopped after $(since "$t") s"
t=$(now); os sandbox start "$NAME" >/dev/null 2>&1; watch_sandbox "$NAME" 120 '^Ready$' >/dev/null; echo "  ready again after $(since "$t") s"
in_sb "$NAME" sh -c 'printf "  /sandbox/keep.txt: %s\n" "$(cat /sandbox/keep.txt 2>/dev/null || echo missing)";
  printf "  /tmp/gone.txt:     %s\n" "$(cat /tmp/gone.txt 2>/dev/null || echo missing)";
  printf "  sleep process:     %s\n" "$(pgrep -x sleep >/dev/null && echo running || echo gone)"'

pod="default--${NAME}"
node="$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.spec.nodeName}')"
pvzone="$(kubectl get pv "$(kubectl -n "$NS" get pvc "workspace-${pod}" -o jsonpath='{.spec.volumeName}')" -o json \
  | jq -r '.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[] | select(.key | test("zone")) | .values[0]')"
say "openshell sandbox stop ${NAME}; kubectl cordon ${node}; openshell sandbox start ${NAME}"
echo "  the workspace volume is an EBS volume in ${pvzone}; the nodes:"
kubectl get nodes -o json | jq -r '.items[] | "    \(.metadata.name)  \(.metadata.labels["topology.kubernetes.io/zone"])"'
os sandbox stop "$NAME" >/dev/null 2>&1; watch_sandbox "$NAME" 60 '^Stopped$' >/dev/null
kubectl cordon "$node" >/dev/null
os sandbox start "$NAME" >/dev/null 2>&1 &
start_pid=$!
t=$(now)
for _ in $(seq 1 30); do  # 30 polls, about two minutes with the node cordoned
  printf '\r  +%5ss workload pod: %-8s  openshell: %-12s' "$(since "$t")" \
    "$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || echo none)" "$(sandbox_state "$NAME" | awk '{print $1}')"
  sleep 3
done
echo
kubectl -n "$NS" get events --field-selector "involvedObject.name=${pod},reason=FailedScheduling" \
  --sort-by=.lastTimestamp -o json | jq -r '.items[-1].message // "(no scheduling event)"' | cut -c1-150 | sed 's/^/  /'
echo "  supervisor pod on: $(kubectl -n "$NS" get pods -l "openshell.ai/boundary-pair=$(sandbox_id "$NAME"),openshell.ai/boundary-role=supervisor" -o jsonpath='{.items[0].spec.nodeName}')"
say "kubectl uncordon ${node}"
t=$(now); kubectl uncordon "$node" >/dev/null; node=""
watch_sandbox "$NAME" 240 '^(Ready|Error)$'
wait "$start_pid" 2>/dev/null
for _ in $(seq 1 30); do in_sb "$NAME" true >/dev/null 2>&1 && break; sleep 2; done
echo "  usable $(since "$t") s after uncordon; /sandbox/keep.txt: $(in_sb "$NAME" cat /sandbox/keep.txt 2>/dev/null || echo unreadable)"
