#!/usr/bin/env bash
# Build Agent Substrate from source, push it to ECR and install the control
# plane into ate-system with the EKS overlay.
set -euo pipefail
source "$(dirname "$0")/env.sh"

# ate-setup reads every setting from flags or the environment.
export KUBECTL_CONTEXT="$KUBE_CONTEXT"
export KO_DOCKER_REPO="$IMAGE_REPO/substrate"
export KO_DEFAULTPLATFORMS=linux/amd64
export SNAPSHOT_LOCATION
export ATE_SYSTEM_OVERLAY=manifests/ate-install/eks
export ATE_RECORD_DIR="$RECORDS"

step "deploy ate-system"
start=$(date +%s)
# Full output (every ko build and push) goes to the log; the terminal shows
# ate-setup's steps and timings.
LOG="$RECORDS/ate-setup.log"
(
  cd "$SUBSTRATE_SRC"
  go run ./cmd/ate-setup --no-dev-env deploy ate-system \
    --rollout-timeout=5m \
    --credential-provider='{"name":"k8s.io"}' \
    --ateapi-authz-bootstrap-owners="system:serviceaccount:ate-system:ate-client,system:serviceaccount:ax-system:ax-server" \
    --ingress-allowed-spiffe-ids="spiffe://cluster.local/ns/ax-system/sa/ax-server"
) 2>&1 | tee "$LOG" | grep -E --line-buffered '\[step\]|rollout status|recorded this|^Error' || true
grep -q 'recorded this configuration' "$LOG"
echo "deploy ate-system took $(( $(date +%s) - start ))s"

step "ate-system"
k -n ate-system get deploy,ds,sts
k get sandboxconfigs -A
