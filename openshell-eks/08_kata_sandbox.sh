#!/usr/bin/env bash
# One OpenShell sandbox whose workload Pod should run in a Kata VM (gate 3, part b), chosen per
# sandbox with --driver-config-json (the values set server.drivers.kubernetes.allowDriverConfig).
# The RuntimeClass must carry the openshell.ai/sandbox-attachable=true label (02_kata.sh adds it).
#
# On OpenShell 0.1.6 and these nodes this fails: the Kata VM's agent dies while the workload
# Pod's workspace-init container seeds the workspace volume, and the sandbox never leaves
# ConfigurationPending. The script shows that, ends the attempt after the init container fails
# (instead of waiting out the 300 s provisioning timeout), then removes the label once to show
# the admission refusal, and puts it back.
#
# Needs: 02_kata.sh and 05_install.sh. RUNTIME_CLASS defaults to kata-qemu.
set -uo pipefail
cd "$(dirname "$0")"

RUNTIME_CLASS="${RUNTIME_CLASS:-kata-qemu}"
GW="osh-eks"
NAME="kata1"
NS="openshell"
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
os() { openshell -g "$GW" "$@"; }
say() { printf '\n$ %s\n' "$*"; }

os status >/dev/null 2>&1 || { echo "Gateway not reachable: run ./05_install.sh" >&2; exit 1; }
restore_label() { kubectl label runtimeclass "$RUNTIME_CLASS" openshell.ai/sandbox-attachable=true --overwrite >/dev/null; }
trap restore_label EXIT
os sandbox delete "$NAME" >/dev/null 2>&1 || true

DRIVER_CONFIG="{\"kubernetes\":{\"pod\":{\"runtime_class_name\":\"${RUNTIME_CLASS}\"}}}"
say "openshell sandbox create --name ${NAME} --driver-config-json '${DRIVER_CONFIG}' --detach"
os sandbox create --name "$NAME" --driver-config-json "$DRIVER_CONFIG" --detach --no-tty >logs/08-create.log 2>&1 &
create_pid=$!
start=$SECONDS
state=""
while (( SECONDS - start < 280 )); do
  pod_json="$(kubectl -n "$NS" get pod "default--${NAME}" -o json 2>/dev/null || true)"
  if [[ -n "$pod_json" ]]; then
    state="$(jq -r '[.status.initContainerStatuses[]? | "\(.name)=\(.state | keys[0])\(if .state.terminated then "(exit \(.state.terminated.exitCode))" else "" end)"] | join(" ")' <<<"$pod_json")"
    phase="$(jq -r '.status.phase' <<<"$pod_json")"
    rc="$(jq -r '.spec.runtimeClassName' <<<"$pod_json")"
    printf '  +%3ds  pod %s (runtimeClassName %s)  init: %s\n' $((SECONDS - start)) "$phase" "$rc" "$state"
    if [[ "$state" == *"(exit "[1-9]* ]] || [[ "$phase" == Running ]]; then break; fi
  fi
  sleep 10
done
if [[ "$(kubectl -n "$NS" get pod "default--${NAME}" -o jsonpath='{.status.phase}' 2>/dev/null)" == Running ]]; then
  echo "sandbox kernel: $(os sandbox exec "$NAME" --no-login-shell --no-tty -- uname -r)"
else
  echo "  the workload Pod's init container failed inside the Kata VM; events on its node:"
  node="$(kubectl -n "$NS" get pod "default--${NAME}" -o jsonpath='{.spec.nodeName}' 2>/dev/null)"
  kubectl -n "$NS" get events --field-selector "involvedObject.name=default--${NAME}" -o json 2>/dev/null \
    | jq -r '.items[] | select(.type == "Warning") | "    \(.reason): \(.message[0:120])"' | tail -n 3
  if [[ -n "$node" ]]; then
    # containerd's own log on that node, read through a debug pod.
    kubectl debug -n default "node/${node}" --quiet --profile=sysadmin --image=public.ecr.aws/amazonlinux/amazonlinux:2023 -- \
      chroot /host sh -c 'journalctl -u containerd --since -5min --no-pager | grep -oE "Dead agent|QEMU exited with an error[^\"]*" | sort | uniq -c' \
      >/dev/null 2>&1
    sleep 15
    dbg="$(kubectl -n default get pods -o json | jq -r --arg n "$node" '.items[] | select(.metadata.name | startswith("node-debugger-")) | select(.spec.nodeName == $n) | .metadata.name' | tail -n 1)"
    echo "  containerd on ${node}, last 5 minutes:"
    kubectl -n default logs "$dbg" 2>/dev/null | sed 's/^/   /'
    kubectl -n default delete pod "$dbg" --wait=false >/dev/null 2>&1
  fi
fi
os sandbox delete "$NAME" >/dev/null 2>&1 || true
wait "$create_pid" 2>/dev/null
echo "  sandbox ${NAME}: $(grep -oE 'provisioning (timed out|stream ended)[^.]*|Created sandbox' logs/08-create.log | tail -n 1)"

say "kubectl label runtimeclass ${RUNTIME_CLASS} openshell.ai/sandbox-attachable-   (then create again)"
kubectl label runtimeclass "$RUNTIME_CLASS" openshell.ai/sandbox-attachable- >/dev/null
os sandbox create --name "${NAME}-nolabel" --driver-config-json "$DRIVER_CONFIG" --detach --no-tty 2>&1 \
  | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'Error|×|│' | head -n 4
os sandbox delete "${NAME}-nolabel" >/dev/null 2>&1 || true
restore_label
echo "label restored: $(kubectl get runtimeclass "$RUNTIME_CLASS" -o jsonpath='{.metadata.labels.openshell\.ai/sandbox-attachable}')"
