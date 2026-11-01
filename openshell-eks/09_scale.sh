#!/usr/bin/env bash
# Scale the node group to zero between sessions, and back.
#
#   ./09_scale.sh down   stop the port-forward; 0 nodes (the control plane, NAT gateway and
#                        any NLB keep billing: about $0.15/h without the nodes)
#   ./09_scale.sh up     2 nodes, wait for them and for the gateway, restart the port-forward
#
# Running sandboxes don't survive a scale-down: their Pods go away with the nodes.
set -euo pipefail
cd "$(dirname "$0")"

[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
LOCAL_PORT="${LOCAL_PORT:-17680}"

scale() {
  aws eks update-nodegroup-config --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
    --nodegroup-name "$NODEGROUP_NAME" --scaling-config "minSize=0,maxSize=2,desiredSize=$1" >/dev/null
  aws eks wait nodegroup-active --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" --nodegroup-name "$NODEGROUP_NAME"
}

case "${1:-}" in
  down)
    if [[ -f .port-forward.pid ]]; then kill "$(cat .port-forward.pid)" 2>/dev/null || true; rm -f .port-forward.pid; fi
    scale 0
    # The managed node group drains each node before terminating it; that can take a while.
    for _ in $(seq 1 120); do
      [[ "$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')" == 0 ]] && break
      sleep 10
    done
    left="$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo "nodes: ${left}"
    if [[ "$left" != 0 ]]; then
      echo "WARNING: ${left} node(s) still draining after 20 minutes; the node group is at 0, so they will go" >&2
    fi
    ;;
  up)
    scale 2
    for _ in $(seq 1 60); do
      [[ "$(kubectl get nodes --no-headers 2>/dev/null | grep -cw Ready)" == 2 ]] && break
      sleep 10
    done
    kubectl get nodes
    kubectl -n openshell rollout status statefulset/openshell --timeout=10m
    if ! { [[ -f .port-forward.pid ]] && kill -0 "$(cat .port-forward.pid)" 2>/dev/null; }; then
      nohup kubectl -n openshell port-forward svc/openshell "${LOCAL_PORT}:8080" >logs/port-forward.log 2>&1 &
      echo $! >.port-forward.pid
      sleep 2
    fi
    openshell status -g osh-eks
    ;;
  *) echo "usage: ./09_scale.sh down|up" >&2; exit 2 ;;
esac
