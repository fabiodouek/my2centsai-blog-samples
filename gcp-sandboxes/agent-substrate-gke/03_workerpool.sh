#!/usr/bin/env bash
# The WorkerPool AX tasks run on: 2 gVisor workers, one per node, no capacity declared (sized from node allocatable).
# Same manifest as the EKS run; only the worker image differs (pre-built v1.0.0 from ghcr.io, pinned by digest).
set -euo pipefail
source "$(dirname "$0")/env.sh"
REPLICAS="${WORKER_REPLICAS:-2}"
digest=$(docker buildx imagetools inspect "ghcr.io/agent-substrate/substrate/ateworker-gvisor:$SUBSTRATE_TAG" | awk '/^Digest:/ {print $2; exit}')
k create namespace ax-system --dry-run=client -o yaml | k apply -f -
t0=$(date +%s)
cat <<YAML | k apply -f -
apiVersion: ate.dev/v1
kind: WorkerPool
metadata:
  name: ax-workers
  namespace: ax-system
spec:
  replicas: ${REPLICAS}
  workerImage: ghcr.io/agent-substrate/substrate/ateworker-gvisor@${digest}
  sandboxClasses:
  - name: gvisor
    configRef:
      name: gvisor-default
YAML
for _ in $(seq 1 60); do k -n ax-system get deployment ax-workers >/dev/null 2>&1 && break; sleep 2; done
k -n ax-system rollout status deployment/ax-workers --timeout=5m
echo "WorkerPool ready after $(( $(date +%s) - t0 )) s"
k get workerpools -A
