#!/usr/bin/env bash
# Install Kata Containers with kata-deploy, then start pods in Kata VMs. This is the gate for
# the rest of the series: OpenSandbox's [secure_runtime] type "kata" needs a working Kata
# RuntimeClass, and Kata is the VM runtime that works together with the egress sidecar.
#
# kata-deploy runs a privileged DaemonSet. On each node it copies the Kata binaries, guest
# kernel and guest image to /opt/kata, writes a containerd drop-in with one runtime per shim
# (/opt/kata/containerd/config.d/kata-deploy.toml), adds it to the imports of
# /etc/containerd/config.toml, restarts containerd and labels the node
# katacontainers.io/kata-runtime=true. Running containers survive the restart. This script
# requires every node to be Ready first, watches them while kata-deploy installs, and stops
# if one stays NotReady. Afterwards it compares the workloads in every namespace with their
# state before the run.
#
# The test (manifests/kata-test-pod.yaml) runs the same command under kata-qemu, kata-clh and
# runc. It prints each pod's kernel next to its node's, and how long each pod took to start.
# The gate passes if kata-qemu, or kata-clh as the fallback, runs a pod in its own VM; if only
# one of them does, the other is a warning.
# While the pods run, a privileged debug pod on each node (kubectl debug --profile=sysadmin)
# shows the VMM process behind each Kata pod and the KVM file descriptors it holds. Set
# SHOW_VMM=0 to skip that step.
#
# Safe to run again: the Helm upgrade renders the same manifests, the DaemonSet pods are not
# restarted, and the test pods run again.
set -euo pipefail
cd "$(dirname "$0")"

KATA_VERSION="3.32.0"   # newest Kata Containers 3.x release (2026-06-22); chart and image share it
KATA_CHART="oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy"
KATA_RELEASE="kata-deploy"
KATA_NAMESPACE="kube-system"
KUBECTL_IMAGE_TAG="v1.37.1"  # the chart's kubectl image (its uninstall hook); the default is :latest
TEST_MANIFEST="manifests/kata-test-pod.yaml"
TEST_NAMESPACE="default"
KATA_PODS="kata-test kata-clh-test"   # RuntimeClasses kata-qemu and kata-clh
TEST_PODS="${KATA_PODS} runc-test"
# The debug pods' image. amazonlinux:2023 is a rolling tag; the debug pods only use its sh and
# chroot, and everything else they run comes from the node (/host).
DEBUG_IMAGE="public.ecr.aws/amazonlinux/amazonlinux:2023"
SHOW_VMM="${SHOW_VMM:-1}"
INSTALL_TIMEOUT=900     # seconds for kata-deploy to finish on every node (it pulls ~1.6 GB)
NOTREADY_LIMIT=120      # seconds a node may stay NotReady during the install before we stop
GATE_TIMEOUT=300        # seconds for the test pods to reach Running
LOGS="logs"             # full Helm output and failure evidence (gitignored)

for tool in kubectl helm jq; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
: "${KUBE_CONTEXT:?env.sh has no KUBE_CONTEXT: run ./01_cluster.sh again}"
# Every kubectl and helm call names the context, so switching contexts in another terminal
# can't point this script at a different cluster.
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
kubectl get --raw /readyz >/dev/null 2>&1 \
  || { echo "cannot reach context '${KUBE_CONTEXT}': run ./01_cluster.sh first" >&2; exit 1; }
mkdir -p "$LOGS"
uninstall_hint() {
  echo "kata-deploy stays installed; to remove it: helm --kube-context ${KUBE_CONTEXT} uninstall ${KATA_RELEASE} -n ${KATA_NAMESPACE} --wait" >&2
}

# --- output and timing helpers -------------------------------------------------
script_start=$(date +%s)
timings=""
phase_name=""
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
secs() { if [[ "$1" =~ ^[0-9]+$ ]]; then duration "$1"; else printf '?'; fi; }  # duration, or "?"
phase() {
  phase_name="$1"
  phase_start=$(date +%s)
  printf '\n\033[1m== %s\033[0m\n' "$1"
}
phase_done() {
  local took=$(( $(date +%s) - phase_start ))
  timings+="$(printf '  %-30s %9s' "$phase_name" "$(duration "$took")")"$'\n'
  printf '   took %s\n' "$(duration "$took")"
}
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

# --- cluster state helpers -----------------------------------------------------
# The poll loops call these every few seconds for up to 15 minutes, so one failed API call
# prints nothing (empty output) and the loop polls again instead of ending the script.
#
# "name readySince" for every node: the Ready condition's last transition. If it changes
# while kata-deploy runs, the node left Ready at some point, however briefly.
node_ready_since() {
  kubectl get nodes -o json | jq -r '.items[]
    | "\(.metadata.name) \(.status.conditions[] | select(.type == "Ready") | "\(.status)@\(.lastTransitionTime)")"'
}
# "2/2", or "1/2 (NotReady: <node>)"
nodes_ready() {
  kubectl get nodes -o json 2>/dev/null | jq -r '
    [.items[] | select(any(.status.conditions[]; .type == "Ready" and .status == "True")) | .metadata.name] as $ready
    | [.items[].metadata.name | select(. as $n | $ready | index($n) | not)] as $not
    | "\($ready | length)/\(.items | length)" + (if ($not | length) > 0 then " (NotReady: \($not | join(", ")))" else "" end)' \
    2>/dev/null || true
}
all_ready() { [[ "$1" =~ ^([0-9]+)/([0-9]+)$ && "${BASH_REMATCH[1]}" == "${BASH_REMATCH[2]}" && "${BASH_REMATCH[2]}" != 0 ]]; }
# {pod UID: container restarts} for every pod in every namespace, kata-deploy's own excluded
restart_map() {
  kubectl get pods -A -o json | jq -c '[.items[] | select(.metadata.labels.name != "kata-deploy")
    | {key: .metadata.uid, value: {pod: "\(.metadata.namespace)/\(.metadata.name)",
                                   restarts: ([.status.containerStatuses[]?.restartCount] | add // 0)}}]
    | from_entries'
}
# One line per Deployment, DaemonSet and pod in every namespace: "ready|not-ready key label".
# Pods are keyed by UID; objects being deleted are left out.
workload_states() {
  kubectl get deployments,daemonsets,pods -A -o json | jq -r '.items[] | select(.metadata.deletionTimestamp == null)
    | "\(.metadata.namespace)/\(.metadata.name)" as $n
    | if .kind == "Deployment" then
        [(if (.status.readyReplicas // 0) >= (.spec.replicas // 0) then "ready" else "not-ready" end),
         "deployment/\($n)", "deployment/\($n)"]
      elif .kind == "DaemonSet" then
        [(if (.status.numberReady // 0) >= (.status.desiredNumberScheduled // 0) then "ready" else "not-ready" end),
         "daemonset/\($n)", "daemonset/\($n)"]
      else
        [(if .status.phase == "Succeeded" or (.status.phase == "Running" and all(.status.containerStatuses[]?; .ready))
          then "ready" else "not-ready" end), "pod/\(.metadata.uid)", "pod/\($n)(\(.status.phase))"]
      end | join(" ")'
}
kata_deploy_uids() { kubectl -n "$KATA_NAMESPACE" get pods -l name=kata-deploy -o jsonpath='{.items[*].metadata.uid}'; }
iso_to_epoch() { jq -rn --arg t "$1" '$t | fromdateiso8601'; }

# Runs a command on node $2 in a privileged debug pod (kubectl debug --profile=sysadmin: host
# PID namespace, the node's filesystem at /host) and writes the pod's output to file $1. It
# waits up to 2 minutes for the pod to succeed or fail, then deletes it. Not meant for $(...):
# DEBUG_PODS has to reach the cleanup trap if the script is interrupted.
DEBUG_PODS=""
node_debug() {
  local outfile="$1" node="$2" out pod phase="" i=0
  shift 2
  out="$(kubectl debug -n "$TEST_NAMESPACE" "node/${node}" --profile=sysadmin --image="$DEBUG_IMAGE" -- "$@" 2>&1 || true)"
  pod="$(sed -n 's/^Creating debugging pod \([^ ]*\) with container.*/\1/p' <<<"$out")"
  if [[ -z "$pod" ]]; then echo "kubectl debug failed: ${out//$'\n'/ }" >"$outfile"; return 0; fi
  DEBUG_PODS+=" ${pod}"
  while (( i < 60 )); do
    phase="$(kubectl -n "$TEST_NAMESPACE" get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [[ "$phase" == Succeeded || "$phase" == Failed ]]; then break; fi
    sleep 2
    i=$((i + 1))
  done
  kubectl -n "$TEST_NAMESPACE" logs "$pod" >"$outfile" 2>&1 || true
  kubectl -n "$TEST_NAMESPACE" delete pod "$pod" --wait=false >/dev/null 2>&1 || true
}

# --- preflight ------------------------------------------------------------------
# kata-deploy restarts containerd on every node, so start only from a healthy cluster.
nr="$(nodes_ready)"
if ! all_ready "$nr"; then
  echo "not every node is Ready (${nr:-no answer from the API}): fix that before installing kata-deploy" >&2
  exit 1
fi
ready_before="$(node_ready_since)"
restarts_before="$(restart_map)"
states_before="$(workload_states)"
uids_before=" $(kata_deploy_uids) "

# --- 1. kata-deploy -------------------------------------------------------------
phase "kata-deploy ${KATA_VERSION}"
# Only the two shims this series needs are installed: qemu (kata-qemu, what OpenSandbox's
# docs use) and clh (kata-clh, the fallback). No snapshotter is set up: both shims use
# containerd's default overlayfs snapshotter, and the chart's default would add nydus.
prev_rev="$(helm -n "$KATA_NAMESPACE" list --filter "^${KATA_RELEASE}\$" -o json | jq -r '.[0].revision // empty')"
if ! helm upgrade --install "$KATA_RELEASE" "$KATA_CHART" --version "$KATA_VERSION" \
    --namespace "$KATA_NAMESPACE" \
    --set shims.disableAll=true \
    --set shims.qemu.enabled=true \
    --set shims.clh.enabled=true \
    --set 'snapshotter.setup={}' \
    --set kubectlImage.tag="$KUBECTL_IMAGE_TAG" \
    >"$LOGS/helm-kata-deploy.log" 2>&1; then
  tail -n 20 "$LOGS/helm-kata-deploy.log" >&2
  exit 1
fi
rev="$(helm -n "$KATA_NAMESPACE" list --filter "^${KATA_RELEASE}\$" -o json | jq -r '.[0].revision')"
if [[ -z "$prev_rev" ]]; then
  echo "chart ${KATA_VERSION} installed (revision ${rev}); waiting for the DaemonSet on every node"
elif diff <(helm -n "$KATA_NAMESPACE" get manifest "$KATA_RELEASE" --revision "$prev_rev") \
          <(helm -n "$KATA_NAMESPACE" get manifest "$KATA_RELEASE" --revision "$rev") >/dev/null; then
  echo "chart ${KATA_VERSION} upgraded to revision ${rev}: manifests identical to revision ${prev_rev}"
else
  echo "chart ${KATA_VERSION} upgraded to revision ${rev}: manifests changed since revision ${prev_rev}"
fi

install_state() {  # what to look at when the install stalls
  kubectl -n "$KATA_NAMESPACE" get pods -l name=kata-deploy -o wide || true
  for pod in $(kubectl -n "$KATA_NAMESPACE" get pods -l name=kata-deploy -o name 2>/dev/null || true); do
    echo "--- ${pod} (last 20 lines)"; kubectl -n "$KATA_NAMESPACE" logs "$pod" --tail=20 || true
  done
}

# Wait for the DaemonSet. A pod turns Ready only when its install has finished (the chart's
# startup probe), so "ready" here means Kata is on that node. One line per change. A node
# that stays NotReady for NOTREADY_LIMIT seconds stops the script.
last=""
notready_since=0
while :; do
  elapsed=$(( $(date +%s) - phase_start ))
  ds="$(kubectl -n "$KATA_NAMESPACE" get daemonset kata-deploy -o json 2>/dev/null \
    | jq -r '.status as $s | [$s.desiredNumberScheduled, $s.updatedNumberScheduled, $s.numberReady,
             .metadata.generation, $s.observedGeneration] | map(. // 0) | join(" ")' 2>/dev/null || true)"
  nr="$(nodes_ready)"
  if [[ -n "$ds" && -n "$nr" ]]; then
    read -r desired updated ready gen observed <<<"$ds"
    line="kata-deploy ${ready}/${desired} ready   nodes Ready ${nr}"
    if [[ "$line" != "$last" ]]; then printf '  %8s  %s\n' "+$(duration "$elapsed")" "$line"; last="$line"; fi
    if (( observed >= gen && desired > 0 && updated == desired && ready == desired )) && all_ready "$nr"; then break; fi
    if all_ready "$nr"; then
      notready_since=0
    else
      if (( notready_since == 0 )); then notready_since=$(date +%s); fi
      if (( $(date +%s) - notready_since >= NOTREADY_LIMIT )); then
        fail "a node has been NotReady for $(duration "$NOTREADY_LIMIT") during the install: ${nr}"
        install_state
        uninstall_hint
        exit 1
      fi
    fi
  fi
  if (( elapsed > INSTALL_TIMEOUT )); then
    fail "kata-deploy not ready on every node after $(duration "$INSTALL_TIMEOUT")"
    install_state
    uninstall_hint
    exit 1
  fi
  sleep 5
done

# Per node: the image pull, how long the pod took from container start to Ready (kata-deploy's
# install, including its 12 s node-label check and the readiness probe interval), and the
# containerd restart: from kata-deploy's "About to restart runtime" to "Runtime restart
# completed", which it logs once containerd is back and the node is Ready again. Pods that
# were already running before this run (same UID) are reported as unchanged.
nodes="$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}')"
for node in $nodes; do
  pod_json="$(kubectl -n "$KATA_NAMESPACE" get pods -l name=kata-deploy -o json \
    | jq --arg node "$node" 'first(.items[] | select(.spec.nodeName == $node)) // empty')"
  if [[ -z "$pod_json" ]]; then echo "  ${node}: no kata-deploy pod"; continue; fi
  pod="$(jq -r '.metadata.name' <<<"$pod_json")"
  if [[ "$uids_before" == *" $(jq -r '.metadata.uid' <<<"$pod_json") "* ]]; then
    echo "  ${node}: kata-deploy pod unchanged (running since $(jq -r '.metadata.creationTimestamp' <<<"$pod_json"))"
    continue
  fi
  # A pull that takes under 2 s found the layers on the node (imagePullPolicy is Always, so
  # the kubelet still asks the registry for the manifest).
  pull="$(kubectl -n "$KATA_NAMESPACE" get events --field-selector "involvedObject.name=${pod},reason=Pulled" -o json 2>/dev/null \
    | jq -r 'def secs: if test("^[0-9.]+(ms|µs|us|ns)$") then 0 elif test("^[0-9.]+s$") then (.[:-1] | tonumber) else 3600 end;
             [.items[].message
              | if test("already present") then "image already on the node"
                else (capture("in (?<d>[0-9][0-9.hmsµn]*) .*Image size: (?<b>[0-9]+) bytes")
                      | if (.d | secs) < 2 then "image already on the node"
                        else "pulled \((.b | tonumber) / 1e9 * 10 | round / 10) GB in \(.d)" end)
                end] | last // "image pull: ?"' 2>/dev/null || echo "image pull: ?")"
  ready_secs="$(jq -r '((.status.conditions[] | select(.type == "Ready") | .lastTransitionTime | fromdateiso8601)
                        - (.status.containerStatuses[0].state.running.startedAt | fromdateiso8601)) // empty' \
                <<<"$pod_json" 2>/dev/null || true)"
  restart="$(kubectl -n "$KATA_NAMESPACE" logs "$pod" --timestamps 2>/dev/null | awk '
    function secs(ts) { split(substr(ts, 12, 15), t, ":"); return t[1] * 3600 + t[2] * 60 + t[3] }
    /About to restart runtime/ && !a { a = $1 }
    /Runtime restart completed/ && !b { b = $1 }
    END { if (a && b) printf "%s, back in %.1fs", substr(a, 12, 8), secs(b) - secs(a); else print "?" }' || true)"
  printf '  %-30s %-27s ready after %-4s containerd restart %s\n' "$node" "$pull" "$(secs "$ready_secs")" "${restart:-?}"
done
phase_done

# --- 2. RuntimeClasses ----------------------------------------------------------
phase "RuntimeClasses"
rc_row='  %-11s %-11s %-13s %-16s %-37s %s\n'
# shellcheck disable=SC2059  # the format is $rc_row
printf "$rc_row" NAME HANDLER "CPU OVERHEAD" "MEMORY OVERHEAD" "NODE SELECTOR" KATA
# The chart labels its RuntimeClasses kata-deploy/instance (Helm overwrites managed-by).
kubectl get runtimeclass -l kata-deploy/instance -o json | jq -r '.items[]
  | [.metadata.name, .handler, (.overhead.podFixed.cpu // "-"), (.overhead.podFixed.memory // "-"),
     ((.scheduling.nodeSelector // {}) | to_entries | map("\(.key)=\(.value)") | join(",") | if . == "" then "-" else . end),
     (.metadata.annotations["katacontainers.io/kata-version"] // "-")] | @tsv' \
  | while IFS=$'\t' read -r name handler cpu mem selector version; do
      # shellcheck disable=SC2059
      printf "$rc_row" "$name" "$handler" "$cpu" "$mem" "$selector" "$version"
    done
for rc in kata-qemu kata-clh; do
  if kubectl get runtimeclass "$rc" >/dev/null 2>&1; then ok "RuntimeClass ${rc}"; else fail "no RuntimeClass ${rc}"; fi
done
labelled="$(kubectl get nodes -l katacontainers.io/kata-runtime=true -o name | wc -l | tr -d ' ')"
node_count="$(wc -w <<<"$nodes" | tr -d ' ')"
if [[ "$labelled" == "$node_count" ]]; then
  ok "${labelled} of ${node_count} nodes labelled katacontainers.io/kata-runtime=true"
else
  fail "${labelled} of ${node_count} nodes labelled katacontainers.io/kata-runtime=true"
fi
phase_done

# --- 3. The gate: pods in Kata VMs ----------------------------------------------
phase "Kata gate (test pods)"
kubectl delete -f "$TEST_MANIFEST" --ignore-not-found --wait=true --timeout=2m >/dev/null
# From here on, any exit (an error, Ctrl-C) deletes the test pods and debug pods.
cleanup() {
  kubectl delete -f "$TEST_MANIFEST" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  for p in $DEBUG_PODS; do kubectl -n "$TEST_NAMESPACE" delete pod "$p" --ignore-not-found --wait=false >/dev/null 2>&1 || true; done
}
trap cleanup EXIT
trap 'exit 130' INT TERM
kubectl apply -f "$TEST_MANIFEST" >/dev/null
test_start=$(date +%s)

# "Running", the reason a container is waiting (ContainerCreating, ...), the pod phase, or
# "?" when the API call failed
pod_state() {
  local s
  s="$(kubectl -n "$TEST_NAMESPACE" get pod "$1" -o json 2>/dev/null | jq -r '
    (.status.containerStatuses[0].state // {}) as $s
    | if $s.running then "Running" elif $s.waiting then $s.waiting.reason
      elif $s.terminated then "Terminated(\($s.terminated.reason))" else .status.phase end' 2>/dev/null || true)"
  echo "${s:-?}"
}
states=""
state_of() { local kv; for kv in $states; do if [[ "${kv%%=*}" == "$1" ]]; then echo "${kv#*=}"; return; fi; done; echo "?"; }
settled() { [[ "$1" == Running || "$1" == Terminated* || "$1" == Failed || "$1" == Succeeded ]]; }
# Poll until every pod is Running or has ended, or GATE_TIMEOUT passes. Waiting for all of them
# means a pod that ends early never cuts another one's wait short.
last=""
while :; do
  elapsed=$(( $(date +%s) - test_start ))
  states="" line="" waiting=0
  for p in $TEST_PODS; do
    s="$(pod_state "$p")"
    states+=" ${p}=${s}"
    line+="${line:+   }${p} ${s}"
    if ! settled "$s"; then waiting=$((waiting + 1)); fi
  done
  if [[ "$line" != "$last" ]]; then printf '  %8s  %s\n' "+$(duration "$elapsed")" "$line"; last="$line"; fi
  if (( waiting == 0 || elapsed > GATE_TIMEOUT )); then break; fi
  sleep 2
done
not_running() {  # pod -> why it is not Running (empty when it is)
  local s
  s="$(state_of "$1")"
  case "$s" in
    Running) ;;
    Terminated*|Failed|Succeeded) echo "ended before the checks (${s})" ;;
    *) echo "still ${s} after $(duration "$GATE_TIMEOUT"), the limit" ;;
  esac
}

# Every pod prints two lines and then waits. With Kata the log goes through the VM's agent, so
# give it a moment to arrive.
probe() {  # pod -> "kernel hypervisor-count", with "-" for a line that never arrived
  local out="" kernel hv
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    out="$(kubectl -n "$TEST_NAMESPACE" logs "$1" 2>/dev/null || true)"
    if grep -q '^hypervisor: ' <<<"$out"; then break; fi
    sleep 1
  done
  kernel="$(sed -n 's/^kernel: //p' <<<"$out")"
  hv="$(sed -n 's/^hypervisor: //p' <<<"$out")"
  printf '%s %s\n' "${kernel:--}" "${hv:--}"  # "-" also keeps the field positions in $facts
}
reported() { [[ -n "$1" && "$1" != - ]]; }  # a probe() field that actually came back
# One line per pod: pod runtime node container-id sandbox-secs running-secs kernel hv
# node-kernel, "-" or "?" where there is nothing to report (a pod that isn't Running is not
# probed). The seconds run from the pod's creation to its sandbox being ready (for Kata: the VM
# booted and its agent answering) and to the container running. API timestamps have 1 s
# resolution.
facts=""
for p in $TEST_PODS; do
  pod_line="$(kubectl -n "$TEST_NAMESPACE" get pod "$p" -o json 2>/dev/null | jq -r '
    (.metadata.creationTimestamp | fromdateiso8601) as $c
    | [ .metadata.name, (.spec.runtimeClassName // "runc"), (.spec.nodeName // "-"),
        (.status.containerStatuses[0].containerID // "-" | sub("^[a-z]+://"; "")),
        (((.status.conditions[]? | select(.type == "PodReadyToStartContainers" and .status == "True")
           | .lastTransitionTime | fromdateiso8601) - $c) // "?"),
        (((.status.containerStatuses[0].state.running.startedAt // empty | fromdateiso8601) - $c) // "?") ]
    | map(tostring) | join(" ")' 2>/dev/null || true)"
  [[ -n "$pod_line" ]] || pod_line="${p} ? - - ? ?"
  read -r _ _ pnode _ <<<"$pod_line"
  if [[ -z "$(not_running "$p")" ]]; then pod_probe="$(probe "$p")"; else pod_probe="- -"; fi
  node_kernel="-"
  if [[ "$pnode" != - ]]; then
    node_kernel="$(kubectl get node "$pnode" -o jsonpath='{.status.nodeInfo.kernelVersion}' 2>/dev/null || true)"
  fi
  facts+="${pod_line} ${pod_probe} ${node_kernel:--}"$'\n'
done
fact() { awk -v p="$1" -v n="$2" '$1 == p { print $n }' <<<"$facts"; }  # pod field-number

echo
row='  %-10s %-29s %-32s %-16s %s\n'
# shellcheck disable=SC2059  # the format is $row
printf "$row" "RUNTIME" "NODE" "KERNEL (uname -r)" "HYPERVISOR FLAG" "CREATED TO RUNNING"
for p in $TEST_PODS; do
  hv="$(fact "$p" 8)"
  if ! reported "$hv"; then hv="-"; elif [[ "$hv" == 1 ]]; then hv="on 1 CPU"; else hv="on ${hv} CPUs"; fi
  if [[ -z "$(not_running "$p")" ]]; then
    started="$(secs "$(fact "$p" 6)") (sandbox ready at $(secs "$(fact "$p" 5)"))"
  else
    started="not Running: $(state_of "$p")"
  fi
  # shellcheck disable=SC2059
  printf "$row" "$(fact "$p" 2)" "$(fact "$p" 3)" "$(fact "$p" 7)" "$hv" "$started"
done
cpu_node="$(fact runc-test 3)"
[[ "$cpu_node" != - ]] || cpu_node="${nodes%% *}"
echo "  The node is itself an EC2 VM with $(kubectl get node "$cpu_node" -o jsonpath='{.status.capacity.cpu}') vCPUs, so runc sees the hypervisor flag on all of them;"
echo "  each Kata VM has its own vCPUs. Times come from the API timestamps (1 s resolution)."

# Evidence for every pod that did not reach Running: its events, kata-deploy's log on its
# node, and the node's containerd journal.
done_nodes=" "
for p in $TEST_PODS; do
  [[ -n "$(not_running "$p")" ]] || continue
  node="$(fact "$p" 3)"
  [[ "$node" != - ]] || node="${nodes%% *}"
  echo
  echo "--- events for ${p}"
  kubectl -n "$TEST_NAMESPACE" get events --field-selector "involvedObject.name=${p}" \
    -o custom-columns='LAST SEEN:.lastTimestamp,TYPE:.type,REASON:.reason,MESSAGE:.message' \
    | tee "$LOGS/${p}-events.log" || true
  [[ "$done_nodes" != *" ${node} "* ]] || continue
  done_nodes+="${node} "
  kd_pod="$(kubectl -n "$KATA_NAMESPACE" get pods -l name=kata-deploy -o json \
    | jq -r --arg node "$node" 'first(.items[] | select(.spec.nodeName == $node) | .metadata.name) // empty' || true)"
  echo "--- kata-deploy on ${node} (last 30 lines; all of it in ${LOGS}/kata-deploy-${node}.log)"
  if [[ -n "$kd_pod" ]]; then kubectl -n "$KATA_NAMESPACE" logs "$kd_pod" >"$LOGS/kata-deploy-${node}.log" 2>&1 || true; fi
  tail -n 30 "$LOGS/kata-deploy-${node}.log" 2>/dev/null || true
  echo "--- containerd on ${node}, last 15 minutes (last 40 lines; all of it in ${LOGS}/containerd-${node}.log)"
  echo "    (read through a privileged debug pod: kubectl debug node/${node} --profile=sysadmin)"
  node_debug "$LOGS/containerd-${node}.log" "$node" chroot /host journalctl -u containerd --since=-15min --no-pager
  tail -n 40 "$LOGS/containerd-${node}.log"
done

# The VMM behind each running Kata pod, read on its node: the containerd container's sandbox
# ID names the QEMU or Cloud Hypervisor process, and the KVM file descriptors it holds (a
# kvm-vm and one kvm-vcpu per vCPU) show that the guest runs under KVM.
# shellcheck disable=SC2016  # runs on the node
VMM_PROBE='
for arg in "$@"; do
  pod=${arg%%=*}; cid=${arg#*=}
  sid=$(ctr -n k8s.io containers info "$cid" 2>/dev/null \
    | sed -n "s/.*\"io.kubernetes.cri.sandbox-id\": *\"\([0-9a-f]*\)\".*/\1/p" | head -n 1)
  pid=""
  [ -n "$sid" ] && pid=$(pgrep -f "^/opt/kata/bin/(qemu-system-x86_64|cloud-hypervisor) .*$sid" | head -n 1)
  if [ -z "$pid" ]; then echo "vmm $pod none - - - 0 0"; continue; fi
  exe=$(basename "$(readlink "/proc/$pid/exe")")
  rss=$(ps -o rss= -p "$pid" | tr -d " ")
  machine=$(tr "\0" "\n" <"/proc/$pid/cmdline" | sed -n "/^-machine$/{n;p;}")
  fds=$(ls -l "/proc/$pid/fd" 2>/dev/null)
  vm=$(printf "%s\n" "$fds" | grep -c "anon_inode:kvm-vm")
  vcpu=$(printf "%s\n" "$fds" | grep -c "anon_inode:kvm-vcpu:")
  echo "vmm $pod $exe $pid $rss ${machine:--} $vm $vcpu"
done
k=$(/opt/kata/bin/kata-runtime --version 2>/dev/null | sed -n "s/^kata-runtime *: *//p")
q=$(/opt/kata/bin/qemu-system-x86_64 --version 2>/dev/null | sed -n "1s/.*version \([^ ]*\).*/\1/p")
c=$(/opt/kata/bin/cloud-hypervisor --version 2>/dev/null | sed "s/^cloud-hypervisor v*//")
g=$(ls /opt/kata/share/kata-containers 2>/dev/null | sed -n "s/^vmlinux-\([0-9.]*-[0-9]*\)$/\1/p" | head -n 1)
echo "versions ${k:-?} ${q:-?} ${c:-?} ${g:-?}"
'
vmm=""
running_kata=""
for p in $KATA_PODS; do
  if [[ -z "$(not_running "$p")" && "$(fact "$p" 4)" != - ]]; then running_kata+=" ${p}"; fi
done
if [[ "$SHOW_VMM" == 1 && -n "$running_kata" ]]; then
  echo
  echo "  The VM behind each Kata pod, read on its node by a privileged debug pod (kubectl debug --profile=sysadmin):"
  vmm_nodes=" "
  for p in $running_kata; do
    node="$(fact "$p" 3)"
    [[ "$vmm_nodes" != *" ${node} "* ]] || continue
    vmm_nodes+="${node} "
    args=""
    for q in $running_kata; do
      if [[ "$(fact "$q" 3)" == "$node" ]]; then args+=" ${q}=$(fact "$q" 4)"; fi
    done
    # shellcheck disable=SC2086  # one pod=container-id pair per word
    node_debug "$LOGS/vmm-${node}.log" "$node" chroot /host sh -c "$VMM_PROBE" sh $args
    vmm+="$(cat "$LOGS/vmm-${node}.log")"$'\n'
  done
  for p in $running_kata; do
    read -r _ _ exe _ rss machine vm vcpu <<<"$(grep "^vmm ${p} " <<<"$vmm" || true)"
    if [[ -z "${exe:-}" || "$exe" == none ]]; then
      printf '  %-14s no VMM process found on %s\n' "$p" "$(fact "$p" 3)"
      continue
    fi
    if [[ "$machine" != - ]]; then machine="-machine ${machine}   "; else machine=""; fi
    [[ "$rss" =~ ^[0-9]+$ ]] || rss=0
    printf '  %-14s %-19s RSS %4s MiB   %sKVM fds: %s VM, %s vCPU\n' "$p" "$exe" "$(( rss / 1024 ))" "$machine" "$vm" "$vcpu"
  done
  read -r _ v_kata v_qemu v_clh v_kernel <<<"$(grep -m 1 '^versions ' <<<"$vmm" || true)"
  echo "  On the nodes: Kata ${v_kata:-?}, QEMU ${v_qemu:-?}, Cloud Hypervisor ${v_clh:-?}, guest kernel ${v_kernel:-?}"
  grep -v -E '^(vmm|versions) |^$' <<<"$vmm" | sed 's/^/  /' || true   # anything unexpected
fi

# One verdict per Kata RuntimeClass. The gate follows the plan: kata-qemu first, kata-clh as
# the fallback, so it passes if either one passes. A runtime that fails while the gate passes
# is a warning. Each check line under a verdict is marked ok or FAIL.
echo
verdict_of=""
for p in $KATA_PODS; do
  rc="$(fact "$p" 2)" node="$(fact "$p" 3)" kernel="$(fact "$p" 7)" node_kernel="$(fact "$p" 9)"
  checks="" verdict=PASS
  why="$(not_running "$p")"
  if [[ -n "$why" ]]; then
    checks+="FAIL|${p} ${why}"$'\n'
    verdict=FAIL
  else
    checks+="ok|${p} Running on ${node} after $(secs "$(fact "$p" 6)") (limit $(duration "$GATE_TIMEOUT"))"$'\n'
    if ! reported "$kernel"; then
      checks+="FAIL|no kernel line in ${p}'s log"$'\n'; verdict=FAIL
    elif [[ "$kernel" == "$node_kernel" ]]; then
      checks+="FAIL|guest kernel ${kernel} is the node's own kernel"$'\n'; verdict=FAIL
    else
      checks+="ok|guest kernel ${kernel} is not the node's kernel (${node_kernel})"$'\n'
    fi
    if [[ "$SHOW_VMM" == 1 ]]; then
      read -r _ _ exe _ _ machine vm _ <<<"$(grep "^vmm ${p} " <<<"$vmm" || true)"
      if [[ "${vm:-0}" =~ ^[0-9]+$ && "${vm:-0}" -ge 1 && ( "$exe" != qemu-system-x86_64 || "$machine" == *accel=kvm* ) ]]; then
        checks+="ok|${exe} runs the guest under KVM"$'\n'
      else
        checks+="FAIL|no KVM-backed VMM process found (${exe:-none}, ${vm:-0} kvm-vm fds)"$'\n'; verdict=FAIL
      fi
    fi
  fi
  printf '  %-5s %s\n' "$verdict" "$rc"
  while IFS='|' read -r mark text; do
    if [[ -n "$mark" ]]; then printf '          %-4s %s\n' "$mark" "$text"; fi
  done <<<"$checks"
  verdict_of+=" ${rc}=${verdict}"
done
qemu_ok=0 clh_ok=0
[[ "$verdict_of" != *" kata-qemu=PASS"* ]] || qemu_ok=1
[[ "$verdict_of" != *" kata-clh=PASS"* ]] || clh_ok=1
gate_on="" gate_note=""
if (( qemu_ok )); then
  gate_on="kata-qemu"
  if (( clh_ok )); then
    ok "Kata gate passed on kata-qemu; kata-clh passed too"
    gate_note="; kata-clh works too, and both run pods in their own VMs on these nodes"
  else
    warn "kata-clh failed (above); the gate passed on kata-qemu, which part 3 uses"
    gate_note=" (kata-clh failed, see the WARN above)"
  fi
elif (( clh_ok )); then
  gate_on="kata-clh"
  warn "kata-qemu failed (above); the gate passed on the fallback, kata-clh"
  ok "Kata gate passed on kata-clh: set [secure_runtime] k8s_runtime_class = \"kata-clh\""
  gate_note=", the fallback (kata-qemu failed, see the WARN above)"
else
  fail "Kata gate failed: neither kata-qemu nor kata-clh ran a pod in its own VM"
  uninstall_hint
fi

why="$(not_running runc-test)"
if [[ -n "$why" ]]; then
  fail "runc-test ${why}"
elif ! reported "$(fact runc-test 7)"; then
  fail "no kernel line in runc-test's log"
elif [[ "$(fact runc-test 7)" == "$(fact runc-test 9)" ]]; then
  ok "runc-test sees its node's kernel, as expected"
else
  fail "runc-test kernel '$(fact runc-test 7)' is not its node's kernel '$(fact runc-test 9)'"
fi
kubectl delete -f "$TEST_MANIFEST" --wait=true --timeout=2m >/dev/null
trap - EXIT INT TERM
ok "test pods deleted"
phase_done

# --- 4. Cluster health ----------------------------------------------------------
phase "cluster health"
ready_after="$(node_ready_since)"
while read -r node state; do
  before="$(awk -v n="$node" '$1 == n {print $2}' <<<"$ready_before")"
  if [[ "$state" != True@* ]]; then
    fail "${node} is not Ready: ${state}"
  elif [[ -n "$before" && "$state" != "$before" ]]; then
    warn "${node} left Ready during this run and came back (Ready since ${state#True@})"
  else
    ok "${node} stayed Ready throughout (Ready since ${state#True@})"
  fi
done <<<"$ready_after"

lbc="$(kubectl -n kube-system get deployment aws-load-balancer-controller -o json \
  | jq -r '"\(.status.readyReplicas // 0)/\(.spec.replicas)"')"
if [[ "${lbc%/*}" == "${lbc#*/}" && "$lbc" != 0/0 ]]; then ok "aws-load-balancer-controller ${lbc} ready"; else fail "aws-load-balancer-controller ${lbc} ready"; fi
pia="$(kubectl -n kube-system get daemonset eks-pod-identity-agent -o json \
  | jq -r '"\(.status.numberReady)/\(.status.desiredNumberScheduled)"')"
if [[ "$pia" == "${node_count}/${node_count}" ]]; then ok "eks-pod-identity-agent ${pia} ready"; else fail "eks-pod-identity-agent ${pia} ready"; fi

# Every namespace, relative to the start of the run: only a Deployment, DaemonSet or pod that
# was ready before and is not ready now fails the check. Ones that were already not ready, or
# appeared during the run and aren't ready yet, are listed as warnings. kata-deploy itself has
# to be ready on every node.
kd="$(kubectl -n "$KATA_NAMESPACE" get daemonset kata-deploy -o json \
  | jq -r '"\(.status.numberReady)/\(.status.desiredNumberScheduled)"')"
if [[ "$kd" == "${node_count}/${node_count}" ]]; then ok "kata-deploy ${kd} ready"; else fail "kata-deploy ${kd} ready"; fi
states_after="$(workload_states)"
changed() {  # became|already|new -> the not-ready objects in that group, comma-separated
  awk -v want="$1" 'NR == FNR { before[$2] = $1; next }
    $1 != "not-ready" { next }
    { group = ($2 in before) ? (before[$2] == "ready" ? "became" : "already") : "new" }
    group == want { out = out (out == "" ? "" : ", ") $3 }
    END { print out }' <(printf 'sentinel - -\n%s\n' "$states_before") <(printf '%s\n' "$states_after")
}
counts="$(awk '{ split($2, k, "/"); n[k[1]]++ }
  END { printf "%d DaemonSets, %d Deployments, %d Pods", n["daemonset"], n["deployment"], n["pod"] }' <<<"$states_after")"
became="$(changed became)"
if [[ -z "$became" ]]; then
  ok "every namespace (${counts}): nothing that was ready before this run is not ready now"
else
  fail "ready before this run, not ready now: ${became}"
fi
already="$(changed already)"
[[ -z "$already" ]] || warn "already not ready before this run: ${already}"
appeared="$(changed new)"
[[ -z "$appeared" ]] || warn "created during this run and not ready yet: ${appeared}"
restarted="$(jq -rn --argjson a "$restarts_before" --argjson b "$(restart_map)" '
  [$b | to_entries[] | (.value.restarts - ($a[.key].restarts // 0)) as $d | select($d > 0)
   | "\(.value.pod) +\($d)"] | join(", ")')"
if [[ -z "$restarted" ]]; then
  ok "no container restarted in any namespace during this run"
else
  fail "containers restarted during this run: ${restarted}"
fi
phase_done

# --- Summary --------------------------------------------------------------------
printf '\n\033[1m== Timings\033[0m\n%s' "$timings"
printf '  %-30s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "Kata gate passed on ${gate_on}${gate_note}"
