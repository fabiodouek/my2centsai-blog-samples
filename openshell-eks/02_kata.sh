#!/usr/bin/env bash
# Install Kata Containers with kata-deploy, then start one pod in a Kata VM (gate 3, part a).
# OpenShell's Kubernetes driver can put a sandbox's workload Pod in a VM through a
# RuntimeClass, chosen per sandbox. It only accepts a RuntimeClass labelled
# openshell.ai/sandbox-attachable=true, so this script adds that label to the one it tests.
#
# kata-deploy runs a privileged DaemonSet. On each node it copies the Kata binaries, guest
# kernel and guest image to /opt/kata, writes a containerd drop-in with one runtime per shim
# (/opt/kata/containerd/config.d/kata-deploy.toml), adds it to the imports of
# /etc/containerd/config.toml, restarts containerd and labels the node
# katacontainers.io/kata-runtime=true. Running containers survive the restart. This script
# watches node readiness while that happens and checks the kube-system workloads afterwards.
#
# The test (manifests/kata-test-pod.yaml) runs the same command under Kata and under runc and
# prints the guest kernel next to the node's, and how long each pod took to start.
#
# Safe to run again: the Helm upgrade renders the same manifests, the DaemonSet pods are not
# restarted, and the test pods run again. RUNTIME_CLASS picks the RuntimeClass of the Kata
# test pod: kata-qemu (default) or kata-clh, which is also installed.
set -euo pipefail
cd "$(dirname "$0")"

KATA_VERSION="3.32.0"   # newest Kata Containers 3.x release (2026-06-22); chart and image share it
KATA_CHART="oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy"
KATA_RELEASE="kata-deploy"
KATA_NAMESPACE="kube-system"
RUNTIME_CLASS="${RUNTIME_CLASS:-kata-qemu}"
TEST_MANIFEST="manifests/kata-test-pod.yaml"
TEST_NAMESPACE="default"
DEBUG_IMAGE="public.ecr.aws/amazonlinux/amazonlinux:2023"
INSTALL_TIMEOUT=900     # seconds for kata-deploy to finish on every node (it pulls ~1.6 GB)
GATE_TIMEOUT=300        # seconds for the Kata test pod to reach Running
LOGS="logs"             # full Helm output and failure evidence (gitignored)

for tool in kubectl helm jq; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
context="$(kubectl config current-context 2>/dev/null || true)"
if [[ "$context" != "$CLUSTER_NAME" ]]; then
  echo "kubectl context is '${context}', not '${CLUSTER_NAME}': run ./01_cluster.sh first" >&2
  exit 1
fi
case "$RUNTIME_CLASS" in
  kata-qemu|kata-clh) ;;
  *) echo "RUNTIME_CLASS must be kata-qemu or kata-clh (got '${RUNTIME_CLASS}')" >&2; exit 1 ;;
esac
mkdir -p "$LOGS"

# --- output and timing helpers -------------------------------------------------
script_start=$(date +%s)
timings=""
phase_name=""
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
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
# "name readySince" for every node: the Ready condition's last transition. If it changes
# while kata-deploy runs, the node left Ready at some point, however briefly.
node_ready_since() {
  kubectl get nodes -o json | jq -r '.items[]
    | "\(.metadata.name) \(.status.conditions[] | select(.type == "Ready") | "\(.status)@\(.lastTransitionTime)")"'
}
# "2/2", or "1/2 (NotReady: <node>)"
nodes_ready() {
  kubectl get nodes -o json | jq -r '
    [.items[] | select(any(.status.conditions[]; .type == "Ready" and .status == "True")) | .metadata.name] as $ready
    | [.items[].metadata.name | select(. as $n | $ready | index($n) | not)] as $not
    | "\($ready | length)/\(.items | length)" + (if ($not | length) > 0 then " (NotReady: \($not | join(", ")))" else "" end)'
}
# Container restarts across kube-system, kata-deploy's own pods excluded.
kube_system_restarts() {
  kubectl -n kube-system get pods -o json | jq '[.items[]
    | select(.metadata.labels.name != "kata-deploy")
    | .status.containerStatuses[]?.restartCount] | add // 0'
}
# kata-deploy pod on node $1
kata_deploy_pod() {
  kubectl -n "$KATA_NAMESPACE" get pods -l name=kata-deploy -o json \
    | jq -r --arg node "$1" '.items[] | select(.spec.nodeName == $node) | .metadata.name' | head -n 1
}
iso_to_epoch() { jq -rn --arg t "$1" '$t | fromdateiso8601'; }

ready_before="$(node_ready_since)"
restarts_before="$(kube_system_restarts)"

# --- 1. kata-deploy -------------------------------------------------------------
phase "kata-deploy ${KATA_VERSION}"
# Only two shims are installed: qemu (kata-qemu, the default here) and clh (kata-clh, the
# fallback). No snapshotter is set up: both shims use
# containerd's default overlayfs snapshotter, and the chart's default would add nydus.
prev_rev="$(helm -n "$KATA_NAMESPACE" list --filter "^${KATA_RELEASE}\$" -o json | jq -r '.[0].revision // empty')"
if ! helm upgrade --install "$KATA_RELEASE" "$KATA_CHART" --version "$KATA_VERSION" \
    --namespace "$KATA_NAMESPACE" \
    --set shims.disableAll=true \
    --set shims.qemu.enabled=true \
    --set shims.clh.enabled=true \
    --set 'snapshotter.setup={}' \
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

# Wait for the DaemonSet. A pod turns Ready only when its install has finished (the chart's
# startup probe), so "ready" here means Kata is on that node. One line per change.
last=""
while :; do
  elapsed=$(( $(date +%s) - phase_start ))
  read -r desired updated ready gen observed < <(kubectl -n "$KATA_NAMESPACE" get daemonset kata-deploy -o json \
    | jq -r '.status as $s | [$s.desiredNumberScheduled, $s.updatedNumberScheduled, $s.numberReady,
             .metadata.generation, $s.observedGeneration] | map(. // 0) | join(" ")')
  line="kata-deploy ${ready}/${desired} ready   nodes Ready $(nodes_ready)"
  if [[ "$line" != "$last" ]]; then printf '  %8s  %s\n' "+$(duration "$elapsed")" "$line"; last="$line"; fi
  if (( observed >= gen && desired > 0 && updated == desired && ready == desired )); then break; fi
  if (( elapsed > INSTALL_TIMEOUT )); then
    fail "kata-deploy not ready on every node after $(duration "$INSTALL_TIMEOUT")"
    kubectl -n "$KATA_NAMESPACE" get pods -l name=kata-deploy -o wide
    for pod in $(kubectl -n "$KATA_NAMESPACE" get pods -l name=kata-deploy -o name); do
      echo "--- ${pod} (last 20 lines)"; kubectl -n "$KATA_NAMESPACE" logs "$pod" --tail=20 || true
    done
    echo "uninstall with: helm uninstall ${KATA_RELEASE} -n ${KATA_NAMESPACE}" >&2
    exit 1
  fi
  sleep 5
done

# Per node: the image pull, the install (container start to Ready), and the containerd
# restart: from kata-deploy's "About to restart runtime" to "Runtime restart completed", which
# it logs once containerd is back and the node is Ready again. Only for pods created by this
# run; on a re-run the pods are still the ones from the first run.
nodes="$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}')"
for node in $nodes; do
  pod="$(kata_deploy_pod "$node")"
  pod_json="$(kubectl -n "$KATA_NAMESPACE" get pod "$pod" -o json)"
  created="$(jq -r '.metadata.creationTimestamp' <<<"$pod_json")"
  if (( $(iso_to_epoch "$created") < script_start - 5 )); then
    echo "  ${node}: kata-deploy pod unchanged (running since ${created})"
    continue
  fi
  pull="$(kubectl -n "$KATA_NAMESPACE" get events --field-selector "involvedObject.name=${pod},reason=Pulled" -o json \
    | jq -r '[.items[].message | capture("in (?<d>[0-9][0-9.hms]*) .*Image size: (?<b>[0-9]+) bytes")
             | "\(.d) (\((.b | tonumber) / 1e9 * 10 | round / 10) GB)"] | last // "?"')"
  started="$(jq -r '.status.containerStatuses[0].state.running.startedAt' <<<"$pod_json")"
  ready_at="$(jq -r '.status.conditions[] | select(.type == "Ready") | .lastTransitionTime' <<<"$pod_json")"
  restart="$(kubectl -n "$KATA_NAMESPACE" logs "$pod" --timestamps 2>/dev/null | awk '
    function secs(ts) { split(substr(ts, 12, 15), t, ":"); return t[1] * 3600 + t[2] * 60 + t[3] }
    /About to restart runtime/ && !a { a = $1 }
    /Runtime restart completed/ && !b { b = $1 }
    END { if (a && b) printf "%s, back in %.1fs", substr(a, 12, 8), secs(b) - secs(a); else print "?" }')"
  printf '  %-30s pull %-18s install %-4s containerd restart %s\n' "$node" "$pull" \
    "$(duration $(( $(iso_to_epoch "$ready_at") - $(iso_to_epoch "$started") )))" "$restart"
done
phase_done

# --- 2. RuntimeClasses ----------------------------------------------------------
phase "RuntimeClasses"
columns="NAME:.metadata.name,HANDLER:.handler,CPU OVERHEAD:.overhead.podFixed.cpu"
columns+=",MEMORY OVERHEAD:.overhead.podFixed.memory,NODE SELECTOR:.scheduling.nodeSelector"
columns+=",KATA:.metadata.annotations.katacontainers\.io/kata-version"
kubectl get runtimeclass -l kata-deploy/instance -o custom-columns="$columns"  # Helm owns managed-by
for rc in kata-qemu kata-clh; do
  if kubectl get runtimeclass "$rc" >/dev/null 2>&1; then ok "RuntimeClass ${rc}"; else fail "no RuntimeClass ${rc}"; fi
done
# OpenShell treats a RuntimeClass as an external resource a sandbox may attach only with
# operator approval: this label (resource admission, on by default in the chart).
kubectl label runtimeclass "$RUNTIME_CLASS" openshell.ai/sandbox-attachable=true --overwrite >/dev/null
attach="$(kubectl get runtimeclass "$RUNTIME_CLASS" -o jsonpath='{.metadata.labels.openshell\.ai/sandbox-attachable}')"
if [[ "$attach" == true ]]; then ok "RuntimeClass ${RUNTIME_CLASS} labelled openshell.ai/sandbox-attachable=true"; else fail "RuntimeClass ${RUNTIME_CLASS} not labelled for OpenShell"; fi
labelled="$(kubectl get nodes -l katacontainers.io/kata-runtime=true -o name | wc -l | tr -d ' ')"
node_count="$(wc -w <<<"$nodes" | tr -d ' ')"
if [[ "$labelled" == "$node_count" ]]; then
  ok "${labelled} of ${node_count} nodes labelled katacontainers.io/kata-runtime=true"
else
  fail "${labelled} of ${node_count} nodes labelled katacontainers.io/kata-runtime=true"
fi
phase_done

# --- 3. The gate: one pod in a Kata VM ------------------------------------------
phase "test pods (${RUNTIME_CLASS} and runc)"
kubectl delete -f "$TEST_MANIFEST" --ignore-not-found --wait=true --timeout=2m >/dev/null
sed "s/runtimeClassName: kata-qemu/runtimeClassName: ${RUNTIME_CLASS}/" "$TEST_MANIFEST" | kubectl apply -f - >/dev/null
test_start=$(date +%s)

# "Running", the reason a container is waiting (ContainerCreating, ...), or the pod phase
pod_state() {
  kubectl -n "$TEST_NAMESPACE" get pod "$1" -o json 2>/dev/null | jq -r '
    (.status.containerStatuses[0].state // {}) as $s
    | if $s.running then "Running" elif $s.waiting then $s.waiting.reason
      elif $s.terminated then "Terminated(\($s.terminated.reason))" else .status.phase end'
}
last=""
while :; do
  elapsed=$(( $(date +%s) - test_start ))
  kata_state="$(pod_state kata-test)"
  runc_state="$(pod_state runc-test)"
  line="kata-test ${kata_state}   runc-test ${runc_state}"
  if [[ "$line" != "$last" ]]; then printf '  %8s  %s\n' "+$(duration "$elapsed")" "$line"; last="$line"; fi
  if [[ "$kata_state" == Running && "$runc_state" == Running ]]; then break; fi
  if [[ "$kata_state" == Terminated* ]] || (( elapsed > GATE_TIMEOUT )); then break; fi
  sleep 2
done

if [[ "$kata_state" != Running ]]; then
  fail "kata-test (${RUNTIME_CLASS}) did not reach Running within $(duration "$GATE_TIMEOUT"): ${kata_state}"
  node="$(kubectl -n "$TEST_NAMESPACE" get pod kata-test -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)"
  [[ -n "$node" ]] || node="${nodes%% *}"
  echo "--- events for kata-test"
  kubectl -n "$TEST_NAMESPACE" get events --field-selector involvedObject.name=kata-test \
    -o custom-columns='LAST SEEN:.lastTimestamp,TYPE:.type,REASON:.reason,MESSAGE:.message' \
    | tee "$LOGS/kata-test-events.log"
  echo "--- kata-deploy on ${node} (last 30 lines; all of it in ${LOGS}/kata-deploy-${node}.log)"
  kubectl -n "$KATA_NAMESPACE" logs "$(kata_deploy_pod "$node")" >"$LOGS/kata-deploy-${node}.log" 2>&1 || true
  tail -n 30 "$LOGS/kata-deploy-${node}.log"
  echo "--- containerd on ${node}, last 15 minutes (last 40 lines; all of it in ${LOGS}/containerd-${node}.log)"
  kubectl debug -n "$TEST_NAMESPACE" "node/${node}" --quiet --profile=sysadmin --image="$DEBUG_IMAGE" -- \
    chroot /host journalctl -u containerd --since=-15min --no-pager >/dev/null 2>&1 || true
  dbg="$(kubectl -n "$TEST_NAMESPACE" get pods -o json | jq -r --arg node "$node" '.items[]
    | select(.metadata.name | startswith("node-debugger-")) | select(.spec.nodeName == $node)
    | .metadata.name' | tail -n 1)"
  if [[ -n "$dbg" ]] && kubectl -n "$TEST_NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded \
      "pod/$dbg" --timeout=2m >/dev/null 2>&1; then
    kubectl -n "$TEST_NAMESPACE" logs "$dbg" >"$LOGS/containerd-${node}.log" 2>&1
    tail -n 40 "$LOGS/containerd-${node}.log"
  fi
  [[ -z "$dbg" ]] || kubectl -n "$TEST_NAMESPACE" delete pod "$dbg" --wait=false >/dev/null
  kubectl delete -f "$TEST_MANIFEST" --ignore-not-found --wait=false >/dev/null
  echo "the Kata gate failed with ${RUNTIME_CLASS}. kata-deploy stays installed;" \
       "helm uninstall ${KATA_RELEASE} -n ${KATA_NAMESPACE} removes it" >&2
  exit 1
fi
if [[ "$runc_state" != Running ]]; then
  fail "runc-test did not reach Running within $(duration "$GATE_TIMEOUT"): ${runc_state}"
  kubectl delete -f "$TEST_MANIFEST" --ignore-not-found --wait=false >/dev/null
  exit 1
fi

# Both pods print two lines and then wait. With Kata the log goes through the VM's agent, so
# give it a moment to arrive.
probe() {  # pod -> "kernel hypervisor-count"
  local out=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    out="$(kubectl -n "$TEST_NAMESPACE" logs "$1" 2>/dev/null || true)"
    grep -q '^hypervisor: ' <<<"$out" && break
    sleep 1
  done
  printf '%s %s\n' "$(sed -n 's/^kernel: //p' <<<"$out")" "$(sed -n 's/^hypervisor: //p' <<<"$out")"
}
# Seconds from the pod's creation to its sandbox being ready (for Kata: the VM booted and its
# agent answering) and to the container running. API timestamps have 1 s resolution.
start_times() {  # pod -> "sandbox running"
  kubectl -n "$TEST_NAMESPACE" get pod "$1" -o json | jq -r '
    (.metadata.creationTimestamp | fromdateiso8601) as $c
    | ((.status.conditions[] | select(.type == "PodReadyToStartContainers")
        | .lastTransitionTime | fromdateiso8601) - $c) as $sandbox
    | ((.status.containerStatuses[0].state.running.startedAt | fromdateiso8601) - $c) as $running
    | "\($sandbox) \($running)"'
}
cpus() { if [[ "$1" == 1 ]]; then echo "on 1 CPU"; else echo "on $1 CPUs"; fi; }
read -r kata_kernel kata_hv < <(probe kata-test)
read -r runc_kernel runc_hv < <(probe runc-test)
read -r kata_sandbox kata_run < <(start_times kata-test)
read -r runc_sandbox runc_run < <(start_times runc-test)
kata_node="$(kubectl -n "$TEST_NAMESPACE" get pod kata-test -o jsonpath='{.spec.nodeName}')"
runc_node="$(kubectl -n "$TEST_NAMESPACE" get pod runc-test -o jsonpath='{.spec.nodeName}')"
node_kernel="$(kubectl get node "$kata_node" -o jsonpath='{.status.nodeInfo.kernelVersion}')"
node_cpus="$(kubectl get node "$kata_node" -o jsonpath='{.status.capacity.cpu}')"

echo
row='  %-10s %-29s %-32s %-16s %s\n'
# shellcheck disable=SC2059  # the format is $row
printf "$row" "RUNTIME" "NODE" "KERNEL (uname -r)" "HYPERVISOR FLAG" "CREATED TO RUNNING"
# shellcheck disable=SC2059
printf "$row" "$RUNTIME_CLASS" "$kata_node" "$kata_kernel" "$(cpus "$kata_hv")" \
  "$(duration "$kata_run") (sandbox ready at $(duration "$kata_sandbox"))"
# shellcheck disable=SC2059
printf "$row" "runc" "$runc_node" "$runc_kernel" "$(cpus "$runc_hv")" \
  "$(duration "$runc_run") (sandbox ready at $(duration "$runc_sandbox"))"
echo "  The node is itself an EC2 VM with ${node_cpus} vCPUs, so runc sees the hypervisor flag on all of"
echo "  them; the Kata VM has its own vCPUs. Times come from the API timestamps (1 s resolution)."
echo

ok "kata-test Running on ${kata_node} after $(duration "$kata_run") (limit $(duration "$GATE_TIMEOUT"))"
if [[ -n "$kata_kernel" && "$kata_kernel" != "$node_kernel" ]]; then
  ok "guest kernel ${kata_kernel} is not the node's kernel (${node_kernel})"
else
  fail "guest kernel '${kata_kernel}' vs node kernel '${node_kernel}': expected them to differ"
fi
if [[ "$runc_kernel" == "$node_kernel" ]]; then
  ok "runc-test sees the node's kernel, as expected"
else
  fail "runc-test kernel '${runc_kernel}' is not the node's kernel '${node_kernel}'"
fi
kubectl delete -f "$TEST_MANIFEST" --wait=true --timeout=2m >/dev/null
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

read -r lbc_ready lbc_want < <(kubectl -n kube-system get deployment aws-load-balancer-controller -o json \
  | jq -r '"\(.status.readyReplicas // 0) \(.spec.replicas)"')
lbc="aws-load-balancer-controller ${lbc_ready}/${lbc_want} ready"
if (( lbc_want > 0 && lbc_ready == lbc_want )); then ok "$lbc"; else fail "$lbc"; fi
pia="$(kubectl -n kube-system get daemonset eks-pod-identity-agent -o json \
  | jq -r '"\(.status.numberReady)/\(.status.desiredNumberScheduled)"')"
pia="eks-pod-identity-agent ${pia} ready"
if [[ "$pia" == *" ${node_count}/${node_count} "* ]]; then ok "$pia"; else fail "$pia"; fi

not_ready="$( { kubectl -n kube-system get deployments -o json | jq -r '.items[]
                  | select((.status.readyReplicas // 0) < .spec.replicas) | "deployment/\(.metadata.name)"'
                kubectl -n kube-system get daemonsets -o json | jq -r '.items[]
                  | select(.status.numberReady < .status.desiredNumberScheduled) | "daemonset/\(.metadata.name)"'; } )"
workloads="$(( $(kubectl -n kube-system get deployments -o name | wc -l) \
              + $(kubectl -n kube-system get daemonsets -o name | wc -l) ))"
if [[ -z "$not_ready" ]]; then
  ok "all ${workloads} kube-system Deployments and DaemonSets ready"
else
  fail "kube-system not ready: ${not_ready//$'\n'/, }"
fi
restarts_after="$(kube_system_restarts)"
if [[ "$restarts_after" == "$restarts_before" ]]; then
  ok "kube-system container restarts: ${restarts_before} before, ${restarts_after} after"
else
  fail "kube-system container restarts: ${restarts_before} before, ${restarts_after} after"
fi
phase_done

# --- Summary --------------------------------------------------------------------
printf '\n\033[1m== Timings\033[0m\n%s' "$timings"
printf '  %-30s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "Kata gate passed: RuntimeClass ${RUNTIME_CLASS} runs pods in their own VM on these nodes"
