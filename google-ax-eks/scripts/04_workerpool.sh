#!/usr/bin/env bash
# Create the gVisor WorkerPool AX schedules tasks on. Installing the Substrate
# control plane does not create one, and AX's quickstart doesn't either
# (google/ax#437), so the first task fails without this step.
set -euo pipefail
source "$(dirname "$0")/env.sh"

WORKER_REPLICAS="${WORKER_REPLICAS:-2}"

k create namespace ax-system --dry-run=client -o yaml | k apply -f -

step "WorkerPool ax-workers ($WORKER_REPLICAS gVisor workers)"
# The worker image is built from Substrate source by ko and pushed to ECR.
cat <<EOF \
  | (cd "$SUBSTRATE_SRC" && KO_DOCKER_REPO="$IMAGE_REPO/substrate" \
       ./hack/run-tool.sh ko resolve --platform=linux/amd64 -f -) \
  | k apply -f -
apiVersion: ate.dev/v1alpha1
kind: WorkerPool
metadata:
  name: ax-workers
  namespace: ax-system
spec:
  replicas: ${WORKER_REPLICAS}
  workerImage: ko://go.ate.dev/substrate/cmd/ateworker-gvisor
  sandboxClasses:
  - name: gvisor
    configRef:
      name: gvisor-default
EOF

for _ in $(seq 1 60); do
  k -n ax-system get deployment ax-workers >/dev/null 2>&1 && break
  sleep 2
done
k -n ax-system rollout status deployment/ax-workers --timeout=5m
k get workerpools -A
