#!/usr/bin/env bash
# What the installer's "Turn on Substrate" step runs: `ate-setup deploy ate-system` from a Substrate checkout at the
# pinned tag, with the pre-built images from ghcr.io. The two env lines that matter for AX (not set by the wizard):
#   ATE_API_AUTHZ_BOOTSTRAP_OWNERS  who may administer the API: AX's server runs as ax-system/ax-server
#   ATE_INGRESS_ALLOWED_SPIFFE_IDS  who may call the router: ax-server (wake on request) and atenet-client (what `kubectl ate proxy` presents)
set -euo pipefail
source "$(dirname "$0")/env.sh"
[ -d "$WORK/substrate" ] || git clone -q https://github.com/agent-substrate/substrate.git "$WORK/substrate"
git -C "$WORK/substrate" fetch -q --tags
git -C "$WORK/substrate" checkout -q "$SUBSTRATE_TAG"
cd "$WORK/substrate"
echo "substrate at $(git rev-parse --short=12 HEAD) ($SUBSTRATE_TAG)"
export PROJECT_ID PROJECT_NUMBER SNAPSHOT_LOCATION SNAPSHOT_TRANSFER_GSA_NAME NO_DEV_ENV=1
export CLUSTER_NAME="$CLUSTER" CLUSTER_LOCATION="$ZONE" GCE_REGION="$REGION" VERSION="$SUBSTRATE_TAG"
export ATE_CREDENTIAL_PROVIDER='{"name":"k8s.io"}' ATE_INSTALL_SANDBOX_CLASSES=gvisor
export ATE_INSTALL_CORDON_SYSTEM_INFRA_COMPONENTS=false
export ATE_API_AUTHZ_BOOTSTRAP_OWNERS="system:serviceaccount:ate-system:ate-client,system:serviceaccount:ax-system:ax-server"
export ATE_INGRESS_ALLOWED_SPIFFE_IDS="spiffe://cluster.local/ns/ax-system/sa/ax-server,spiffe://cluster.local/ns/ate-system/sa/atenet-client"
t0=$(date +%s)
go run ./cmd/ate-setup deploy ate-system --rollout-timeout=5m \
  --image-repo ghcr.io/agent-substrate/substrate --image-tag "$SUBSTRATE_TAG" 2>&1 | tee "$WORK/logs/install.log"
echo "ate-setup deploy took $(( $(date +%s) - t0 )) s"
kubectl --context "$CTX" get pods -n ate-system
go build -o "$WORK/bin/kubectl-ate" ./cmd/kubectl-ate
