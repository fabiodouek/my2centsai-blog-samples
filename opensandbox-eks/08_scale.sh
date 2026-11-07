#!/usr/bin/env bash
# Scale the node group to zero when you stop for the day, and bring it back.
#
#   ./08_scale.sh down   desired size 0: the two m7i.2xlarge nodes terminate. The EKS control
#                        plane, the NAT gateway, the NLB and the EBS volume keep billing.
#   ./08_scale.sh nodes-only   the first half of `up`: desired size 2 and wait for two Ready nodes, nothing
#                        else (99_teardown.sh uses it: it must not depend on the health checks).
#   ./08_scale.sh up     desired size 2: waits for two Ready nodes, for the pods to come back,
#                        and checks that kata-deploy installed Kata on the new nodes.
#
# What a scale-down does to the cluster:
#  - Pods that ran on the nodes are gone; Deployments recreate them on the new nodes (they wait
#    Pending until `up`). Sandboxes were not seen to be recreated: create new ones after `up`.
#  - The new nodes are new instances. kata-deploy (a DaemonSet) installs Kata on them again;
#    `up` checks the containerd "imports" line, the node label, and runs one pod under
#    kata-qemu.
#  - The server's SQLite file is on an EBS volume in one availability zone, so the server pod
#    can only run on a node in that zone. With two nodes in two zones there is one.
#  - Terraform ignores the node group's desired size, so ./01_cluster.sh keeps the size you set.
#
# Safe to run again: it only changes the desired size when it differs.
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-}"
case "$MODE" in
  down|up|nodes-only) ;;
  *) echo "usage: $0 down|up|nodes-only" >&2; exit 2 ;;
esac

for tool in aws kubectl jq; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
: "${KUBE_CONTEXT:?env.sh has no KUBE_CONTEXT: run ./01_cluster.sh again}"
: "${CLUSTER_NAME:?env.sh has no CLUSTER_NAME: run ./01_cluster.sh again}"
: "${NODEGROUP_NAME:?env.sh has no NODEGROUP_NAME: run ./01_cluster.sh again}"
: "${AWS_REGION:?env.sh has no AWS_REGION: run ./01_cluster.sh again}"
: "${ECR_REGISTRY:?env.sh has no ECR_REGISTRY: run ./01_cluster.sh again}"
: "${ECR_PREFIX:?env.sh has no ECR_PREFIX: run ./01_cluster.sh again}"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }

WANT_NODES=2
DEBUG_IMAGE="public.ecr.aws/amazonlinux/amazonlinux:2023"   # same as 01_cluster.sh
NODE_WAIT_SECONDS=900                # nodes becoming Ready after a scale-up
SCALE_DOWN_WAIT_SECONDS=1800         # nodes disappearing after a scale-down: a recorded run took 16.5 min
trap 'exit 130' INT TERM
cleanup_debug_pods() {
  local pod
  for pod in $debug_pods; do kubectl -n default delete pod "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true; done
}
debug_pods=""
test_pod=""
trap 'cleanup_debug_pods; [[ -z "$test_pod" ]] || kubectl -n default delete pod "$test_pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true' EXIT

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
  timings+="$(printf '  %-42s %9s' "$phase_name" "$(duration "$took")")"$'\n'
  printf '   took %s\n' "$(duration "$took")"
}
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

nodegroup() {  # nodegroup <jmespath query>
  aws eks describe-nodegroup --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
    --nodegroup-name "$NODEGROUP_NAME" --query "$1" --output text
}
ready_nodes() {  # the number of Ready nodes (0 if the API does not answer)
  local json
  json="$(kubectl get nodes -o json 2>/dev/null || true)"
  if [[ -z "$json" ]]; then echo 0; return; fi
  jq '[.items[] | select(any(.status.conditions[]; .type == "Ready" and .status == "True"))] | length' <<<"$json"
}
all_nodes() {
  local json
  json="$(kubectl get nodes -o json 2>/dev/null || true)"
  if [[ -z "$json" ]]; then echo 0; return; fi
  jq '.items | length' <<<"$json"
}

# Sets the desired size, keeping min and max as they are (min is lowered when it would exceed
# the new desired size). Waits for the update to finish.
set_desired() {
  local want="$1" min max desired
  read -r min max desired < <(nodegroup 'nodegroup.scalingConfig.[minSize,maxSize,desiredSize]')
  if (( want > max )); then max="$want"; fi
  if (( min > want )); then min="$want"; fi
  if [[ "$desired" == "$want" ]]; then
    ok "node group ${NODEGROUP_NAME} already has desired size ${want} (min ${min}, max ${max})"
    return
  fi
  echo "  node group ${NODEGROUP_NAME}: desired ${desired} -> ${want} (min ${min}, max ${max})"
  aws eks update-nodegroup-config --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
    --nodegroup-name "$NODEGROUP_NAME" \
    --scaling-config "minSize=${min},maxSize=${max},desiredSize=${want}" >/dev/null
  # The CLI's own waiter: it polls every 30 s and gives up after about 40 minutes.
  aws eks wait nodegroup-active --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" --nodegroup-name "$NODEGROUP_NAME"
  ok "node group update finished: $(nodegroup 'nodegroup.status')"
}

# Runs a command on node $2 in a privileged debug pod (profile sysadmin: the node's root is at
# /host) and prints its output. The pod is deleted afterwards.
node_run() {
  local node="$1" out pod phase="" i=0
  shift
  out="$(kubectl debug -n default "node/${node}" --profile=sysadmin --image="$DEBUG_IMAGE" -- "$@" 2>&1 || true)"
  pod="$(sed -n 's/^Creating debugging pod \([^ ]*\) with container.*/\1/p' <<<"$out")"
  if [[ -z "$pod" ]]; then echo "kubectl debug failed: ${out//$'\n'/ }"; return 0; fi
  debug_pods+=" ${pod}"
  while (( i < 60 )); do
    phase="$(kubectl -n default get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [[ "$phase" == Succeeded || "$phase" == Failed ]]; then break; fi
    sleep 2
    i=$((i + 1))
  done
  kubectl -n default logs "$pod" 2>&1 || true
  kubectl -n default delete pod "$pod" --wait=false >/dev/null 2>&1 || true
}

# ===============================================================================
if [[ "$MODE" == down ]]; then
  phase "scale the node group to 0"
  set_desired 0
  phase_done

  phase "wait for the nodes to go"
  # EKS drains the nodes one after the other; the wait is measured on the clock, not by counting sleeps.
  wait_start=$(date +%s)
  while (( $(all_nodes) > 0 && $(date +%s) - wait_start < SCALE_DOWN_WAIT_SECONDS )); do
    printf '  %s node(s) left\n' "$(all_nodes)"
    sleep 15
  done
  if (( $(all_nodes) == 0 )); then ok "no nodes"; else fail "$(all_nodes) node(s) still registered after ${SCALE_DOWN_WAIT_SECONDS} s"; fi
  phase_done

  printf '\nNodes now:\n'
  kubectl get nodes 2>&1 | sed 's/^/  /'
  printf '\nStill billing while the node group is at 0: the EKS control plane, the NAT gateway, the NLB, the EBS volume\n'
  printf 'and the ECR images. Run ./08_scale.sh up to bring the nodes back, ./99_teardown.sh to remove everything.\n'
if (( $(all_nodes) > 0 )); then printf 'The nodes keep billing until they are gone; this took %s so far.\n' "$(duration $(( $(date +%s) - script_start )))"; fi

# ===============================================================================
else
  phase "scale the node group to ${WANT_NODES}"
  set_desired "$WANT_NODES"
  phase_done

  phase "wait for ${WANT_NODES} Ready nodes"
  wait_start=$(date +%s)
  while (( $(ready_nodes) < WANT_NODES && $(date +%s) - wait_start < NODE_WAIT_SECONDS )); do
    printf '  %s of %s nodes Ready\n' "$(ready_nodes)" "$WANT_NODES"
    sleep 15
  done
  if (( $(ready_nodes) >= WANT_NODES )); then ok "${WANT_NODES} nodes Ready"; else fail "$(ready_nodes) of ${WANT_NODES} nodes Ready after ${NODE_WAIT_SECONDS} s"; fi
  kubectl get nodes 2>&1 | sed 's/^/  /'
  phase_done

  if [[ "$MODE" == nodes-only ]]; then
    printf '\n\033[1mTimings\033[0m\n%s' "$timings"
    if (( failures > 0 )); then printf '\n%d check(s) failed (see FAIL above)\n' "$failures"; exit 1; fi
    printf '\nnodes are Ready (the Kata and OpenSandbox checks were skipped: run ./08_scale.sh up for them)\n'
    exit 0
  fi

  phase "kata-deploy on the new nodes"
  # kata-deploy installs Kata on each node and then labels it katacontainers.io/kata-runtime=true.
  waited=0
  labelled() { kubectl get nodes -l katacontainers.io/kata-runtime=true -o name 2>/dev/null | wc -l | tr -d ' '; }
  while (( $(labelled) < WANT_NODES && waited < 600 )); do
    printf '  %s of %s nodes labelled katacontainers.io/kata-runtime=true\n' "$(labelled)" "$WANT_NODES"
    sleep 10
    waited=$((waited + 10))
  done
  if (( $(labelled) >= WANT_NODES )); then ok "kata-deploy labelled ${WANT_NODES} nodes"; else fail "only $(labelled) of ${WANT_NODES} nodes labelled by kata-deploy after 10 minutes"; fi
  if kubectl get runtimeclass kata-qemu >/dev/null 2>&1; then ok "RuntimeClass kata-qemu exists"; else fail "RuntimeClass kata-qemu is missing"; fi
  for node in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
    # shellcheck disable=SC2016
    imports="$(node_run "$node" chroot /host sh -c 'grep -c "kata-deploy.toml" /etc/containerd/config.toml; ls /opt/kata/bin/containerd-shim-kata-v2 2>&1')"
    if grep -q '^[1-9]' <<<"$imports" && grep -q 'containerd-shim-kata-v2$' <<<"$imports" && ! grep -q 'No such file' <<<"$imports"; then
      ok "${node}: containerd imports the kata-deploy drop-in; the kata shim is installed"
    else
      fail "${node}: containerd config or the kata shim missing: ${imports//$'\n'/ }"
    fi
  done
  phase_done

  phase "a pod under kata-qemu"
  test_pod="kata-up-check-$(date +%s)"
  image="${ECR_REGISTRY}/${ECR_PREFIX}/python:3.12-slim"
  kubectl -n default apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${test_pod}
spec:
  runtimeClassName: kata-qemu
  restartPolicy: Never
  containers:
    - name: uname
      image: ${image}
      command: ["uname", "-r"]
EOF
  # --for=jsonpath cannot see Failed, so poll for either phase.
  pod_phase=""
  wait_start=$(date +%s)
  while (( $(date +%s) - wait_start < 240 )); do
    pod_phase="$(kubectl -n default get pod "$test_pod" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [[ "$pod_phase" == Succeeded || "$pod_phase" == Failed ]]; then break; fi
    sleep 3
  done
  if [[ "$pod_phase" == Succeeded ]]; then
    guest="$(kubectl -n default logs "$test_pod")"
    node="$(kubectl -n default get pod "$test_pod" -o jsonpath='{.spec.nodeName}')"
    host="$(kubectl get node "$node" -o jsonpath='{.status.nodeInfo.kernelVersion}')"
    if [[ -n "$guest" && "$guest" != "$host" ]]; then
      ok "kata-qemu pod ran in its own VM on ${node}: guest kernel ${guest}, node kernel ${host}"
    else
      fail "kata-qemu pod reports kernel ${guest:-none}, the same as node ${node} (${host}): it did not run in a VM"
    fi
  else
    why="$(kubectl -n default get pod "$test_pod" -o jsonpath='{.status.phase} {.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)"
    fail "kata-qemu pod ${pod_phase:-had no status}, not Succeeded, within 4 minutes (${why:-no status}); kubectl -n default describe pod ${test_pod}"
  fi
  kubectl -n default delete pod "$test_pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  test_pod=""
  phase_done

  phase "OpenSandbox pods"
  # Deployments recreate their pods on the new nodes; wait for every pod in the namespace.
  for d in $(kubectl -n opensandbox-system get deploy -o jsonpath='{.items[*].metadata.name}'); do
    if kubectl -n opensandbox-system rollout status "deployment/${d}" --timeout=300s >/dev/null 2>&1; then
      ok "${d} available"
    else
      fail "${d} not available after 5 minutes"
    fi
  done
  kubectl -n opensandbox-system get pods 2>&1 | sed 's/^/  /'
  not_running="$(kubectl -n opensandbox-system get pods --no-headers 2>/dev/null | awk '$3 != "Running" && $3 != "Completed" {print $1 " " $3}')"
  if [[ -z "$not_running" ]]; then ok "every pod in opensandbox-system is Running"; else fail "pods not Running: ${not_running//$'\n'/, }"; fi
  phase_done

  phase "server volume and gateway load balancer"
  # The server's EBS volume lives in one availability zone: the server pod must land on a node there.
  pv="$(kubectl -n opensandbox-system get pvc opensandbox-server-data -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"
  vol=""
  if [[ -n "$pv" ]]; then vol="$(kubectl get pv "$pv" -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null || true)"; fi
  if [[ -n "$vol" ]]; then
    if ! vol_info="$(aws ec2 describe-volumes --region "$AWS_REGION" --volume-ids "$vol" --query 'Volumes[0].[State,AvailabilityZone]' --output text 2>&1)"; then
      vol_info="could not describe the volume: ${vol_info//$'\n'/ }"
    fi
    server_node="$(kubectl -n opensandbox-system get pods -l app.kubernetes.io/name=opensandbox-server -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)"
    server_zone=""
    if [[ -n "$server_node" ]]; then server_zone="$(kubectl get node "$server_node" -o jsonpath='{.metadata.labels.topology\.kubernetes\.io/zone}')"; fi
    if [[ "$vol_info" == in-use* ]]; then ok "the server's EBS volume is attached (${vol_info//$'\t'/ }); the server pod runs in ${server_zone:-?}"; else fail "the server's EBS volume is not attached: ${vol_info//$'\t'/ }"; fi
  fi
  # Target health of the NLB in front of the gateway. Targets of the old nodes' pods drain for a few minutes.
  nlb_host="$(kubectl -n opensandbox-system get svc opensandbox-gateway-nlb -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  tg_arn=""
  if [[ -n "$nlb_host" ]]; then
    nlb_arn="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --query "LoadBalancers[?DNSName=='${nlb_host}'].LoadBalancerArn | [0]" --output text 2>/dev/null || true)"
    tg_arn=""
    if [[ -n "$nlb_arn" && "$nlb_arn" != None ]]; then
      tg_arn="$(aws elbv2 describe-target-groups --region "$AWS_REGION" --load-balancer-arn "$nlb_arn" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
    fi
  fi
  if [[ -n "$nlb_host" && ( -z "$tg_arn" || "$tg_arn" == None ) ]]; then
    fail "could not find the NLB or its target group for ${nlb_host}"
  elif [[ -n "$nlb_host" ]]; then
    want_gw="$(kubectl -n opensandbox-system get deploy opensandbox-ingress-gateway -o jsonpath='{.spec.replicas}')"
    healthy_gw() {
      [[ "$(aws elbv2 describe-target-health --region "$AWS_REGION" --target-group-arn "$tg_arn" \
        --query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" --output text)" == "$want_gw" ]]
    }
    waited=0
    until healthy_gw || (( waited >= 300 )); do sleep 10; waited=$((waited + 10)); done
    states="$(aws elbv2 describe-target-health --region "$AWS_REGION" --target-group-arn "$tg_arn" --query 'TargetHealthDescriptions[].TargetHealth.State' --output text)"
    if healthy_gw; then ok "NLB: ${want_gw} gateway targets healthy (all states: ${states//$'\t'/ })"; else fail "NLB: fewer than ${want_gw} healthy gateway targets after 5 minutes (states: ${states//$'\t'/ })"; fi
  else
    warn "no gateway NLB Service: skipped the target health check"
  fi
  phase_done
fi

printf '\n\033[1mTimings\033[0m\n%s' "$timings"
printf '  %-42s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then printf '\n%d check(s) failed (see FAIL above)\n' "$failures"; exit 1; fi
printf '\nall checks passed\n'
